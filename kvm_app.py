"""Desktop window that turns this Mac's keyboard and trackpad into Windows input.

Click the window to start controlling Windows through the CH340 + CH9329
cable. Press Control + Option + Command together (or switch away from the
window) to give control back to the Mac.

Run with:  uv run kvm_app.py
"""

import argparse
import os
import sys
import threading

import AppKit
import Foundation
import objc
import Quartz
from PyObjCTools import AppHelper

import kvm_bridge as kb
import kvm_panel as kp
import kvm_core as kc
import windows_keyboard as wk

BUTTON_BITS = {0: 1, 1: 2, 2: 4}  # NSEvent buttonNumber -> HID left/right/middle
EVENT_MASK = 0
for _type in (
    AppKit.NSEventTypeKeyDown, AppKit.NSEventTypeKeyUp, AppKit.NSEventTypeFlagsChanged,
    AppKit.NSEventTypeMouseMoved, AppKit.NSEventTypeScrollWheel,
    AppKit.NSEventTypeLeftMouseDown, AppKit.NSEventTypeLeftMouseUp, AppKit.NSEventTypeLeftMouseDragged,
    AppKit.NSEventTypeRightMouseDown, AppKit.NSEventTypeRightMouseUp, AppKit.NSEventTypeRightMouseDragged,
    AppKit.NSEventTypeOtherMouseDown, AppKit.NSEventTypeOtherMouseUp, AppKit.NSEventTypeOtherMouseDragged,
):
    EVENT_MASK |= 1 << _type
APP_NAME = "Mac2Win KVM"
WINDOW_WIDTH, WINDOW_MIN_WIDTH = 880, 800
ICON_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "icon.png")
KEY_EVENTS = {AppKit.NSEventTypeKeyDown, AppKit.NSEventTypeKeyUp, AppKit.NSEventTypeFlagsChanged}
MOUSE_DOWN = {AppKit.NSEventTypeLeftMouseDown, AppKit.NSEventTypeRightMouseDown, AppKit.NSEventTypeOtherMouseDown}
MOUSE_UP = {AppKit.NSEventTypeLeftMouseUp, AppKit.NSEventTypeRightMouseUp, AppKit.NSEventTypeOtherMouseUp}
MOUSE_MOVE = {AppKit.NSEventTypeMouseMoved, AppKit.NSEventTypeLeftMouseDragged,
              AppKit.NSEventTypeRightMouseDragged, AppKit.NSEventTypeOtherMouseDragged}

MAPPING_NOTE = (
    "Paste: ⌘V types the Mac clipboard on Windows (also while controlling; Ctrl+V is not sent). "
    "US-layout ASCII only; Esc stops.\n\n"
    "Control → Ctrl   Option → Alt   ⌘+key → Ctrl+key   ⌘ alone → Start   Delete → Backspace\n\n"
    "Not forwarded (macOS keeps them): Cmd+Tab, Cmd+Space, Mission Control, "
    "Cmd+Opt+Esc, media keys (hold fn for F1–F12)."
)
FOOTER_NOTE = "⌃ Control → Ctrl    ⌥ Option → Alt    ⌘+key → Ctrl+key    ⌘ alone → Start    ⌫ → Backspace"
LEVEL_COLORS = {"ok": "systemGreenColor", "warn": "systemOrangeColor", "error": "systemRedColor",
                "idle": "secondaryLabelColor"}
CARD_RADIUS = 12
GLOW = 8  # room around the capture card for its glow


def color(name):
    return getattr(AppKit.NSColor, name)()


def symbol(name, size=13, weight=AppKit.NSFontWeightRegular):
    """SF Symbol image, or None (callers then fall back to text)."""
    image = AppKit.NSImage.imageWithSystemSymbolName_accessibilityDescription_(name, None)
    if image is None:
        return None
    config = AppKit.NSImageSymbolConfiguration.configurationWithPointSize_weight_(size, weight)
    return image.imageWithSymbolConfiguration_(config)


def label(text, size=13, weight=AppKit.NSFontWeightRegular, text_color="labelColor", wrap=False):
    field = (AppKit.NSTextField.wrappingLabelWithString_(text) if wrap
             else AppKit.NSTextField.labelWithString_(text))
    field.setFont_(AppKit.NSFont.systemFontOfSize_weight_(size, weight))
    field.setTextColor_(color(text_color))
    return field


def stack(views, vertical=False, spacing=8, alignment=None, distribution=None):
    view = AppKit.NSStackView.stackViewWithViews_(views)
    view.setOrientation_(AppKit.NSUserInterfaceLayoutOrientationVertical if vertical
                         else AppKit.NSUserInterfaceLayoutOrientationHorizontal)
    view.setSpacing_(spacing)
    if alignment is not None:
        view.setAlignment_(alignment)
    if distribution is not None:
        view.setDistribution_(distribution)
    return view


def pin(view, parent, inset):
    """Pin ``view`` to ``parent`` with (top, left, bottom, right) insets."""
    view.setTranslatesAutoresizingMaskIntoConstraints_(False)
    top, left, bottom, right = inset
    for constraint in (view.topAnchor().constraintEqualToAnchor_constant_(parent.topAnchor(), top),
                       view.leadingAnchor().constraintEqualToAnchor_constant_(parent.leadingAnchor(), left),
                       parent.bottomAnchor().constraintEqualToAnchor_constant_(view.bottomAnchor(), bottom),
                       parent.trailingAnchor().constraintEqualToAnchor_constant_(view.trailingAnchor(), right)):
        constraint.setActive_(True)


def match_width(view, other):
    view.widthAnchor().constraintEqualToAnchor_(other.widthAnchor()).setActive_(True)


class CardView(AppKit.NSView):
    """Rounded, lightly filled container that follows light and dark mode."""

    def drawRect_(self, rect):
        path = AppKit.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            AppKit.NSInsetRect(self.bounds(), 0.5, 0.5), CARD_RADIUS, CARD_RADIUS)
        AppKit.NSColor.controlBackgroundColor().colorWithAlphaComponent_(0.6).setFill()
        path.fill()
        AppKit.NSColor.separatorColor().setStroke()
        path.stroke()


class ChipView(AppKit.NSView):
    """Status pill: tinted capsule with a symbol (or dot) and short text."""

    def drawRect_(self, rect):
        bounds = AppKit.NSInsetRect(self.bounds(), 0.5, 0.5)
        radius = bounds.size.height / 2
        path = AppKit.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(bounds, radius, radius)
        tint = color(LEVEL_COLORS[getattr(self, "level", "idle")])
        tint.colorWithAlphaComponent_(0.14).setFill()
        path.fill()
        tint.colorWithAlphaComponent_(0.35).setStroke()
        path.stroke()

    def isFlipped(self):
        return True


def make_chip(level, symbol_name, text, tip):
    chip = ChipView.alloc().initWithFrame_(((0, 0), (10, 10)))
    chip.level = level
    tint = color(LEVEL_COLORS[level])
    image = symbol(symbol_name, 11, AppKit.NSFontWeightSemibold)
    if image is not None:
        icon = AppKit.NSImageView.imageViewWithImage_(image)
        icon.setContentTintColor_(tint)
    else:
        icon = label("●", 10, text_color=LEVEL_COLORS[level])
    text_field = label(text, 11, AppKit.NSFontWeightMedium)
    inner = stack([icon, text_field], spacing=4)
    chip.addSubview_(inner)
    pin(inner, chip, (3, 9, 3, 10))
    chip.setToolTip_(tip)
    return chip


class CaptureView(AppKit.NSView):
    """Large click target that shows whether Windows is being controlled."""

    def initWithFrame_(self, frame):
        self = objc.super(CaptureView, self).initWithFrame_(frame)
        if self is None:
            return None
        self.state = "connecting"
        self.icon = AppKit.NSImageView.alloc().init()
        self.title = label("", 22, AppKit.NSFontWeightSemibold)
        self.subtitle = label("", 13, text_color="secondaryLabelColor", wrap=True)
        for field in (self.title, self.subtitle):
            field.setAlignment_(AppKit.NSTextAlignmentCenter)
        self.subtitle.setPreferredMaxLayoutWidth_(520)
        content = stack([self.icon, self.title, self.subtitle], vertical=True, spacing=8,
                        alignment=AppKit.NSLayoutAttributeCenterX)
        content.setCustomSpacing_afterView_(14, self.icon)
        self.addSubview_(content)
        content.setTranslatesAutoresizingMaskIntoConstraints_(False)
        content.centerXAnchor().constraintEqualToAnchor_(self.centerXAnchor()).setActive_(True)
        content.centerYAnchor().constraintEqualToAnchor_(self.centerYAnchor()).setActive_(True)
        content.widthAnchor().constraintLessThanOrEqualToAnchor_constant_(self.widthAnchor(), -48).setActive_(True)
        return self

    def drawRect_(self, rect):
        bounds = AppKit.NSInsetRect(self.bounds(), GLOW, GLOW)
        path = AppKit.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(bounds, 16, 16)
        accent = AppKit.NSColor.controlAccentColor()
        if self.state in ("capturing", "typing"):
            accent.colorWithAlphaComponent_(0.07).setFill()
            path.fill()
            AppKit.NSGraphicsContext.saveGraphicsState()
            glow = AppKit.NSShadow.alloc().init()
            glow.setShadowColor_(accent)
            glow.setShadowBlurRadius_(GLOW)
            glow.setShadowOffset_((0, 0))
            glow.set()
            accent.setStroke()
            path.setLineWidth_(2.5)
            path.stroke()
            path.stroke()
            AppKit.NSGraphicsContext.restoreGraphicsState()
            return
        AppKit.NSColor.controlBackgroundColor().colorWithAlphaComponent_(0.6).setFill()
        path.fill()
        if self.state == "ready":
            accent.colorWithAlphaComponent_(0.7).setStroke()
            path.setLineWidth_(1.5)
            path.setLineDash_count_phase_([7, 5], 2, 0)
        else:
            AppKit.NSColor.separatorColor().setStroke()
        path.stroke()

    def resetCursorRects(self):
        if self.state in ("ready", "offline"):
            self.addCursorRect_cursor_(self.bounds(), AppKit.NSCursor.pointingHandCursor())

    @objc.python_method
    def set_state(self, state, symbol_name, title, subtitle):
        self.state = state
        tint = {"capturing": "controlAccentColor", "typing": "controlAccentColor",
                "ready": "controlAccentColor", "offline": "systemRedColor"}.get(state, "secondaryLabelColor")
        image = symbol(symbol_name, 40, AppKit.NSFontWeightLight)
        self.icon.setImage_(image)
        self.icon.setHidden_(image is None)
        self.icon.setContentTintColor_(color(tint))
        self.title.setStringValue_(title)
        self.subtitle.setStringValue_(subtitle)
        self.subtitle.setHidden_(not subtitle)
        self.window() and self.window().invalidateCursorRectsForView_(self)
        self.setNeedsDisplay_(True)


class Controller:
    """Owns the cable, the window, and capture state. Runs on the main thread."""

    def __init__(self, args):
        self.args = args
        self.port = self.keyboard = self.sender = self.outbox = None
        self.connecting = False
        self.capturing = False
        self.typing = False
        self.typing_label = ""
        self.typing_finish = None
        self.bridge = None
        self.panel = kp.Panel()
        self.popover = None
        self.path, self.rate, self.firmware = None, 0, 0
        self.target = self.leds = 0
        self.typing_total = 0
        self.cable_text = "Connecting…"
        self.cable_error = None
        self.keys = kc.KeyboardState()
        self.buttons = 0
        self.move_x = kc.Accumulator(args.mouse_scale)
        self.move_y = kc.Accumulator(args.mouse_scale)
        self.scroll = kc.Accumulator()
        self.monitor = None
        self.delegate = Delegate.alloc().init()
        self.delegate.controller = self

    # ----- window -----

    def build_window(self):
        style = (AppKit.NSWindowStyleMaskTitled | AppKit.NSWindowStyleMaskClosable
                 | AppKit.NSWindowStyleMaskMiniaturizable | AppKit.NSWindowStyleMaskResizable
                 | AppKit.NSWindowStyleMaskFullSizeContentView)
        self.window = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            ((200, 200), (WINDOW_WIDTH, 700)), style, AppKit.NSBackingStoreBuffered, False)
        self.window.setTitle_(APP_NAME)
        self.window.setTitlebarAppearsTransparent_(True)
        self.window.setTitleVisibility_(AppKit.NSWindowTitleHidden)
        self.window.setDelegate_(self.delegate)
        self.window.setAcceptsMouseMovedEvents_(True)
        self.window.setReleasedWhenClosed_(False)
        self.window.setFrameAutosaveName_("Mac2WinMainWindow")

        background = AppKit.NSVisualEffectView.alloc().init()
        background.setMaterial_(AppKit.NSVisualEffectMaterialUnderWindowBackground)
        background.setBlendingMode_(AppKit.NSVisualEffectBlendingModeBehindWindow)
        background.setState_(AppKit.NSVisualEffectStateFollowsWindowActiveState)
        self.window.setContentView_(background)

        self.capture_view = CaptureView.alloc().initWithFrame_(((0, 0), (100, 100)))
        self.capture_view.setToolTip_("Click to send this Mac's keyboard and trackpad to Windows")
        self.capture_view.setContentHuggingPriority_forOrientation_(1, AppKit.NSLayoutConstraintOrientationVertical)
        self.capture_view.heightAnchor().constraintGreaterThanOrEqualToConstant_(200).setActive_(True)

        rows = [self.build_header(), self.capture_view, self.build_paste_row(), self.build_panel(),
                self.build_footer()]
        main = stack(rows, vertical=True, spacing=16, alignment=AppKit.NSLayoutAttributeCenterX)
        main.setCustomSpacing_afterView_(16 - GLOW, rows[0])
        main.setCustomSpacing_afterView_(16 - GLOW, self.capture_view)
        background.addSubview_(main)
        # The capture card spans GLOW points past the 20 pt margins so its glow has room.
        pin(main, background, (40, 20 - GLOW, 16, 20 - GLOW))
        for row in rows:
            if row is self.capture_view:
                match_width(row, main)
            else:
                row.widthAnchor().constraintEqualToAnchor_constant_(main.widthAnchor(), -2 * GLOW).setActive_(True)

        self.refresh()
        background.layoutSubtreeIfNeeded()
        fitting = main.fittingSize()
        minimum = (WINDOW_MIN_WIDTH, fitting.height + 56)
        self.window.setContentMinSize_(minimum)
        self.window.setContentSize_((WINDOW_WIDTH, max(fitting.height + 56, 720)))
        self.window.center()

    def build_header(self):
        views = []
        icon = AppKit.NSImage.alloc().initWithContentsOfFile_(ICON_PATH)
        if icon is not None:
            image_view = AppKit.NSImageView.imageViewWithImage_(icon)
            image_view.setImageScaling_(AppKit.NSImageScaleProportionallyUpOrDown)
            for anchor in (image_view.widthAnchor(), image_view.heightAnchor()):
                anchor.constraintEqualToConstant_(44).setActive_(True)
            views.append(image_view)
        titles = stack([label(APP_NAME, 17, AppKit.NSFontWeightBold),
                        label("Mac keyboard & trackpad → Windows over the CH9329 cable", 12,
                              text_color="secondaryLabelColor")],
                       vertical=True, spacing=2, alignment=AppKit.NSLayoutAttributeLeading)
        views.append(titles)
        spacer = AppKit.NSView.alloc().init()
        spacer.setContentHuggingPriority_forOrientation_(1, AppKit.NSLayoutConstraintOrientationHorizontal)
        views.append(spacer)
        self.chips = stack([], spacing=6)
        views.append(self.chips)
        header = stack(views, spacing=12, alignment=AppKit.NSLayoutAttributeCenterY)
        titles.setContentCompressionResistancePriority_forOrientation_(
            250, AppKit.NSLayoutConstraintOrientationHorizontal)
        return header

    def control_button(self, title, action, symbol_name=None, tooltip=None, target=None):
        button = AppKit.NSButton.buttonWithTitle_target_action_(title, target or self.delegate, action)
        image = symbol(symbol_name) if symbol_name else None
        if image is not None:
            button.setImage_(image)
            button.setImagePosition_(AppKit.NSImageLeading)
        # Never take keyboard focus, so Space/Return in the window don't click it.
        button.setRefusesFirstResponder_(True)
        if tooltip:
            button.setToolTip_(tooltip)
        return button

    def build_paste_row(self):
        self.paste_button = self.control_button(
            " Type Clipboard on Windows", "typeClipboard:", "doc.on.clipboard",
            "Types the Mac clipboard text on Windows (⌘V). US-layout ASCII only.")
        self.paste_button.setControlSize_(AppKit.NSControlSizeLarge)
        self.paste_button.setFont_(AppKit.NSFont.systemFontOfSize_weight_(13, AppKit.NSFontWeightSemibold))
        # Accent-filled while the window is active (inactive windows draw it grey).
        self.paste_button.setBezelColor_(AppKit.NSColor.controlAccentColor())
        if hasattr(self.paste_button, "setTintProminence_"):  # macOS 26+
            self.paste_button.setTintProminence_(AppKit.NSTintProminencePrimary)
        self.progress = AppKit.NSProgressIndicator.alloc().init()
        self.progress.setStyle_(AppKit.NSProgressIndicatorStyleBar)
        self.progress.setIndeterminate_(False)
        self.progress.setMinValue_(0)
        self.progress.setMaxValue_(1)
        self.progress.widthAnchor().constraintEqualToConstant_(160).setActive_(True)
        self.stop_button = self.control_button("Stop", "stopTyping:", "stop.circle", "Stop typing (Esc)")
        self.notice = label("⌘V also works, even while controlling Windows.", 12,
                            text_color="secondaryLabelColor")
        self.notice.setLineBreakMode_(AppKit.NSLineBreakByTruncatingTail)
        self.notice.setContentCompressionResistancePriority_forOrientation_(
            250, AppKit.NSLayoutConstraintOrientationHorizontal)
        self.notice.setContentHuggingPriority_forOrientation_(1, AppKit.NSLayoutConstraintOrientationHorizontal)
        row = stack([self.paste_button, self.progress, self.stop_button, self.notice], spacing=12,
                    alignment=AppKit.NSLayoutAttributeCenterY)
        return row

    def panel_button(self, title, index):
        _, combo = kp.BUTTONS[index]
        button = self.control_button(title, "panelKey:", kp.SYMBOLS.get(title), kp.tooltip(title, combo))
        button.setTag_(index)
        button.setFont_(AppKit.NSFont.systemFontOfSize_(12))
        if title in "←→↑↓" and button.image() is not None:
            button.setImagePosition_(AppKit.NSImageOnly)
        button.setContentCompressionResistancePriority_forOrientation_(
            250, AppKit.NSLayoutConstraintOrientationHorizontal)
        return button

    def build_section(self, name, rows, start):
        header = label(name.upper(), 10, AppKit.NSFontWeightSemibold, "secondaryLabelColor")
        views = [header]
        index = start
        for row in rows:
            buttons = []
            for title, _ in row:
                buttons.append(self.panel_button(title, index))
                index += 1
            views.append(stack(buttons, spacing=6, distribution=AppKit.NSStackViewDistributionFillEqually))
        section = stack(views, vertical=True, spacing=6, alignment=AppKit.NSLayoutAttributeLeading)
        for row in views[1:]:
            match_width(row, section)
        return section, index

    def build_panel(self):
        """Windows special-key buttons; clicking them never captures."""
        title = label("Special keys", 13, AppKit.NSFontWeightSemibold)
        hint = label("Sent to Windows on click", 11, text_color="secondaryLabelColor")
        spacer = AppKit.NSView.alloc().init()
        spacer.setContentHuggingPriority_forOrientation_(1, AppKit.NSLayoutConstraintOrientationHorizontal)
        hold = label("Hold for next key:", 11, text_color="secondaryLabelColor")
        self.sticky_control = AppKit.NSSegmentedControl.alloc().init()
        self.sticky_control.setSegmentCount_(len(kp.STICKY))
        self.sticky_control.setTrackingMode_(AppKit.NSSegmentSwitchTrackingSelectAny)
        for index, ((name, _), symbol_name) in enumerate(zip(kp.STICKY, kp.STICKY_SYMBOLS)):
            self.sticky_control.setLabel_forSegment_(name, index)
            image = symbol(symbol_name, 11)
            if image is not None:
                self.sticky_control.setImage_forSegment_(image, index)
                self.sticky_control.setImageScaling_forSegment_(AppKit.NSImageScaleNone, index)
            self.sticky_control.setToolTip_forSegment_(f"Hold {name} for the next button", index)
        self.sticky_control.setTarget_(self.delegate)
        self.sticky_control.setAction_("stickyKey:")
        self.sticky_control.setRefusesFirstResponder_(True)
        header = stack([title, hint, spacer, hold, self.sticky_control], spacing=8,
                       alignment=AppKit.NSLayoutAttributeCenterY)

        sections = dict(kp.SECTIONS)
        built, index = {}, 0
        for name, rows in kp.SECTIONS:
            built[name], index = self.build_section(name, rows, index)
        bands = []
        for names in kp.BANDS:
            band = stack([built[name] for name in names], spacing=20,
                         alignment=AppKit.NSLayoutAttributeTop,
                         distribution=AppKit.NSStackViewDistributionFill)
            for name in names:
                built[name].setContentHuggingPriority_forOrientation_(
                    1, AppKit.NSLayoutConstraintOrientationHorizontal)
            # Size sections by column count so every key in a band is the same width.
            columns = [len(sections[name][0]) for name in names]
            for name, count in zip(names[1:], columns[1:]):
                built[name].widthAnchor().constraintEqualToAnchor_multiplier_(
                    built[names[0]].widthAnchor(), count / columns[0]).setActive_(True)
            bands.append(band)
        content = stack([header] + bands, vertical=True, spacing=14)
        for view in [header] + bands:
            match_width(view, content)
        card = CardView.alloc().initWithFrame_(((0, 0), (100, 100)))
        card.addSubview_(content)
        pin(content, card, (14, 16, 16, 16))
        card.setContentHuggingPriority_forOrientation_(750, AppKit.NSLayoutConstraintOrientationVertical)
        return card

    def build_footer(self):
        note = label(FOOTER_NOTE, 11, text_color="tertiaryLabelColor")
        note.setLineBreakMode_(AppKit.NSLineBreakByTruncatingTail)
        note.setContentCompressionResistancePriority_forOrientation_(
            250, AppKit.NSLayoutConstraintOrientationHorizontal)
        note.setToolTip_(MAPPING_NOTE)
        spacer = AppKit.NSView.alloc().init()
        spacer.setContentHuggingPriority_forOrientation_(1, AppKit.NSLayoutConstraintOrientationHorizontal)
        help_button = AppKit.NSButton.alloc().init()
        help_button.setBezelStyle_(AppKit.NSBezelStyleHelpButton)
        help_button.setTitle_("")
        help_button.setTarget_(self.delegate)
        help_button.setAction_("showHelp:")
        help_button.setRefusesFirstResponder_(True)
        help_button.setToolTip_("Key mapping and limitations")
        return stack([note, spacer, help_button], spacing=8, alignment=AppKit.NSLayoutAttributeCenterY)

    def show_help(self, sender):
        popover = AppKit.NSPopover.alloc().init()
        popover.setBehavior_(AppKit.NSPopoverBehaviorTransient)
        text = label(MAPPING_NOTE, 12, wrap=True)
        text.setPreferredMaxLayoutWidth_(360)
        holder = AppKit.NSView.alloc().init()
        holder.addSubview_(text)
        pin(text, holder, (14, 16, 14, 16))
        controller = AppKit.NSViewController.alloc().init()
        controller.setView_(holder)
        popover.setContentViewController_(controller)
        popover.showRelativeToRect_ofView_preferredEdge_(sender.bounds(), sender, AppKit.NSRectEdgeMaxY)
        self.popover = popover

    def panel_key(self, index):
        taps, label_text = self.panel.press(index)
        self.sync_sticky()
        error = self.start_taps(taps, f"keys {label_text}", check_caps=False)
        if error:
            self.set_notice(error, "error")

    def toggle_sticky(self, control):
        for index, (_, bit) in enumerate(kp.STICKY):
            if control.isSelectedForSegment_(index) != bool(self.panel.sticky & bit):
                self.panel.toggle(index)
        self.sync_sticky()

    def sync_sticky(self):
        for index, (_, bit) in enumerate(kp.STICKY):
            self.sticky_control.setSelected_forSegment_(bool(self.panel.sticky & bit), index)

    def build_menu(self):
        menubar = AppKit.NSMenu.alloc().init()
        item = AppKit.NSMenuItem.alloc().init()
        menubar.addItem_(item)
        menu = AppKit.NSMenu.alloc().init()
        menu.addItemWithTitle_action_keyEquivalent_(f"Hide {APP_NAME}", "hide:", "h")
        menu.addItem_(AppKit.NSMenuItem.separatorItem())
        menu.addItemWithTitle_action_keyEquivalent_(f"Quit {APP_NAME}", "terminate:", "q")
        item.setSubmenu_(menu)
        edit_item = AppKit.NSMenuItem.alloc().init()
        menubar.addItem_(edit_item)
        edit = AppKit.NSMenu.alloc().initWithTitle_("Edit")
        paste = edit.addItemWithTitle_action_keyEquivalent_("Type Clipboard on Windows", "typeClipboard:", "v")
        paste.setTarget_(self.delegate)
        edit_item.setSubmenu_(edit)
        AppKit.NSApp.setMainMenu_(menubar)

    def refresh(self):
        for chip in list(self.chips.arrangedSubviews()):
            self.chips.removeView_(chip)
        for chip in kp.status_chips(self.connecting, self.cable_error, self.path if self.keyboard else None,
                                    self.rate, self.firmware, self.target, self.leds):
            self.chips.addArrangedSubview_(make_chip(*chip))
        self.capture_view.set_state(*kp.capture_message(
            bool(self.keyboard), self.connecting, self.capturing, self.typing, self.typing_label,
            self.cable_error, kc.RELEASE_SHORTCUT, kc.PASTE_SHORTCUT))
        self.paste_button.setEnabled_(bool(self.keyboard) and not self.typing)
        self.progress.setHidden_(not self.typing)
        self.stop_button.setHidden_(not self.typing)

    # ----- cable -----

    def start_connect(self):
        if self.connecting or self.keyboard:
            return
        self.connecting = True
        self.cable_error = None
        self.cable_text = "Connecting…"
        self.refresh()
        threading.Thread(target=self.connect_worker, daemon=True).start()

    def connect_worker(self):
        try:
            path = self.args.port
            if not path:
                ports = wk.usb_serial_ports()
                if len(ports) != 1:
                    raise wk.CableError(f"Found {len(ports)} USB serial ports; plug in the CH340 end "
                                        "or pass --port.")
                path = ports[0]
            port, keyboard, info, rate = wk.connect(path, self.args.baud, 0.01)
        except (wk.CableError, OSError) as exc:
            AppHelper.callAfter(self.connect_failed, exc)
            return
        AppHelper.callAfter(self.connected, path, port, keyboard, info, rate)

    def connected(self, path, port, keyboard, info, rate):
        self.connecting = False
        self.path, self.rate, self.firmware = path, rate, info[0]
        self.port, self.keyboard = port, keyboard
        if self.args.smoke_test:
            keyboard.keys = keyboard.mouse = refuse_report
        self.outbox = kc.Outbox()
        self.sender = kc.Sender(
            keyboard, self.outbox,
            lambda value: AppHelper.callAfter(self.show_info, value),
            lambda exc: AppHelper.callAfter(self.connection_lost, exc),
            on_text_done=lambda: AppHelper.callAfter(self.text_done))
        self.sender.start()
        self.show_info(info)
        if self.args.smoke_test:
            print(self.cable_text, flush=True)
            print("Smoke test: window built, status queried. No keyboard or mouse reports were sent.", flush=True)
            AppHelper.callLater(0.5, self.finish_smoke_test, 0 if info[1] == 1 else 1)

    def finish_smoke_test(self, code):
        # NSApp's own termination calls C exit(), which would drop the status code.
        self.shutdown()
        sys.stdout.flush()
        os._exit(code)

    def show_info(self, info):
        if not self.keyboard:
            return
        _, target, leds = info
        self.target, self.leds = target, leds
        self.cable_text = (
            f"Cable: {self.path}  {self.rate} baud  firmware 0x{self.firmware:02X}\n"
            f"Windows target: {'connected' if target == 1 else 'NOT connected (replug the CH9329 end)'}"
            f"   Caps Lock: {'ON' if leds & 2 else 'off'}"
        )
        self.refresh()

    def connect_failed(self, exc):
        self.connecting = False
        self.cable_error = str(exc)
        self.cable_text = f"Cable error: {exc}"
        self.refresh()
        if self.args.smoke_test:
            print(self.cable_text, file=sys.stderr, flush=True)
            self.finish_smoke_test(1)

    def connection_lost(self, exc):
        self.finish_typing(f"Cable error: {exc}")
        self.stop_capture(send=False)
        self.disconnect()
        self.cable_error = str(exc)
        self.cable_text = f"Cable error: {exc}"
        self.refresh()

    def disconnect(self):
        if self.outbox:
            self.outbox.close()
        if self.sender and self.sender is not threading.current_thread():
            self.sender.join(2)
        if self.port:
            self.port.close()
        self.port = self.keyboard = self.sender = self.outbox = None

    # ----- clipboard -----

    def set_notice(self, text, level="info"):
        self.notice.setStringValue_(text)
        self.notice.setToolTip_(text)
        self.notice.setTextColor_(color("systemRedColor" if level == "error" else "secondaryLabelColor"))

    def paste_clipboard(self):
        text = AppKit.NSPasteboard.generalPasteboard().stringForType_(AppKit.NSPasteboardTypeString)
        if not text:
            return self.set_notice("The Mac clipboard has no text.", "error")
        try:
            taps = wk.encode_text(text)
        except ValueError as exc:
            return self.set_notice(str(exc), "error")
        error = self.start_taps(taps, "the clipboard")
        if error:
            self.set_notice(error, "error")

    def start_taps(self, taps, label, on_finish=None, check_caps=True):
        """Queue validated taps; returns an error string instead if refused.

        ``on_finish(error_or_None)`` is called on the main thread when typing
        completes, is cancelled, or the cable fails.
        """
        if self.typing:
            return "Already typing; wait for it to finish or press Esc in the app window."
        if not self.keyboard:
            return "Cable not connected; nothing was typed."
        try:
            # Shortcuts work with Caps Lock on; only typed text needs it off.
            kb.check_ready(self.target, self.leds if check_caps else 0)
        except kb.BridgeError as exc:
            return str(exc)
        # Drop any keys or buttons Windows thinks are held before typing.
        self.keys.clear()
        self.buttons = 0
        self.outbox.release_all()
        count = self.outbox.taps(taps)
        self.typing = True
        self.typing_label = label
        self.typing_finish = on_finish
        self.typing_total = count
        self.progress.setDoubleValue_(0)
        self.set_notice(f"Typing {label} ({count} keys)… press Esc to stop.")
        self.refresh()
        AppHelper.callLater(0.1, self.update_progress)
        return None

    def update_progress(self):
        if not self.typing or not self.outbox:
            return
        if self.typing_total:
            done = self.typing_total - self.outbox.pending_taps()
            self.progress.setDoubleValue_(done / self.typing_total)
        AppHelper.callLater(0.1, self.update_progress)

    def finish_typing(self, error):
        if not self.typing:
            return
        self.typing = False
        callback, self.typing_finish = self.typing_finish, None
        self.set_notice(error or f"Typed {self.typing_label} on Windows.",
                        "error" if error and not error.startswith("Stopped") else "info")
        self.refresh()
        if callback:
            callback(error)

    def cancel_typing(self):
        if not self.typing:
            return
        self.keys.clear()
        if self.outbox:
            self.outbox.release_all()
        self.finish_typing(f"Stopped typing {self.typing_label}; the rest was not sent.")

    def text_done(self):
        self.finish_typing(None)

    # ----- requests from kvm_mcp.py -----

    def handle_bridge_request(self, request):
        """Runs on a bridge worker thread; hops to the main thread for state."""
        if request.get("op") == "status":
            result = {}
            done = threading.Event()

            def read_status():
                if not self.keyboard:
                    result["error"] = "kvm_app.py is running but the cable is not connected."
                else:
                    result["message"] = kb.status_text(self.path, self.rate, self.firmware,
                                                       self.target, self.leds, "kvm_app.py")
                done.set()
            AppHelper.callAfter(read_status)
            done.wait(5)
            if "error" in result or "message" not in result:
                raise kb.BridgeError(result.get("error", "kvm_app.py did not answer."))
            return result["message"]

        taps = kb.prepare(request)
        finished = threading.Event()
        outcome = {}

        def on_finish(error):
            outcome["error"] = error
            finished.set()

        def start():
            label = (f"keys {request['combo'].strip()}" if request["op"] == "keys"
                     else "text from Copilot")
            error = self.start_taps(taps, label, on_finish)
            if error:
                on_finish(error)
        AppHelper.callAfter(start)
        finished.wait()
        if outcome["error"]:
            raise kb.BridgeError(outcome["error"])
        return kb.describe(request, taps)

    # ----- capture -----

    def start_capture(self):
        if self.capturing or self.typing or not self.keyboard:
            return
        self.capturing = True
        self.keys.clear()
        self.buttons = 0
        for acc in (self.move_x, self.move_y, self.scroll):
            acc.reset()
        self.outbox.release_all()
        Quartz.CGAssociateMouseAndMouseCursorPosition(False)
        AppKit.NSCursor.hide()
        self.refresh()

    def stop_capture(self, send=True):
        if not self.capturing:
            return
        self.cancel_typing()
        self.capturing = False
        self.keys.clear()
        self.buttons = 0
        if send and self.outbox:
            self.outbox.release_all()
        Quartz.CGAssociateMouseAndMouseCursorPosition(True)
        AppKit.NSCursor.unhide()
        self.refresh()

    def handle_event(self, event):
        kind = event.type()
        if kind in KEY_EVENTS and event.window() == self.window or self.capturing:
            if self.typing:
                if kind == AppKit.NSEventTypeKeyDown and event.keyCode() == kc.MAC_ESCAPE:
                    self.cancel_typing()
                elif kind == AppKit.NSEventTypeFlagsChanged and kc.is_release_shortcut(event.modifierFlags()):
                    self.cancel_typing()
                    self.stop_capture()
                return None
            if kind == AppKit.NSEventTypeKeyDown and kc.is_paste_shortcut(
                    event.keyCode(), event.modifierFlags()):
                self.paste_clipboard()
                return None
        if not self.capturing:
            if (kind == AppKit.NSEventTypeLeftMouseDown and event.window() == self.window
                    and self.capture_view.hitTest_(
                        self.capture_view.superview().convertPoint_fromView_(event.locationInWindow(), None))):
                if self.keyboard:
                    self.start_capture()
                else:
                    self.start_connect()
                return None
            return event

        if kind == AppKit.NSEventTypeFlagsChanged:
            flags = event.modifierFlags()
            if kc.is_release_shortcut(flags):
                self.stop_capture()
                return None
            if event.keyCode() == kc.MAC_CAPS_LOCK:
                # macOS sends one event per toggle, so forward it as a full tap.
                self.send_keys(self.keys.press(kc.HID_CAPS_LOCK))
                self.send_keys(self.keys.release(kc.HID_CAPS_LOCK))
            else:
                self.send_keys(self.keys.set_modifier(kc.modifier_byte(flags)))
        elif kind == AppKit.NSEventTypeKeyDown:
            usage = kc.MAC_TO_HID.get(event.keyCode())
            if usage and not event.isARepeat():
                # Resync modifiers in case flag changes were missed (e.g. during typing).
                self.send_keys(self.keys.set_modifier(kc.modifier_byte(event.modifierFlags())))
                self.send_keys(self.keys.press(usage))
        elif kind == AppKit.NSEventTypeKeyUp:
            usage = kc.MAC_TO_HID.get(event.keyCode())
            if usage:
                self.send_keys(self.keys.release(usage))
        elif kind in MOUSE_MOVE:
            dx, dy = self.move_x.add(event.deltaX()), self.move_y.add(event.deltaY())
            if dx or dy:
                self.outbox.mouse(self.buttons, dx, dy)
        elif kind in MOUSE_DOWN or kind in MOUSE_UP:
            bit = BUTTON_BITS.get(event.buttonNumber(), 0)
            buttons = self.buttons | bit if kind in MOUSE_DOWN else self.buttons & ~bit
            if kind in MOUSE_DOWN:
                self.keys.use_win()
            if buttons != self.buttons:
                self.buttons = buttons
                self.outbox.mouse(buttons)
        elif kind == AppKit.NSEventTypeScrollWheel:
            steps = kc.scroll_steps(self.scroll, event.scrollingDeltaY(),
                                    event.hasPreciseScrollingDeltas())
            if steps:
                self.outbox.mouse(self.buttons, wheel=steps)
        return None

    def send_keys(self, reports):
        for report in reports:
            self.outbox.keys(report)

    def set_icon(self):
        image = AppKit.NSImage.alloc().initWithContentsOfFile_(ICON_PATH)
        if image is not None:  # missing or unreadable: keep the default icon
            AppKit.NSApp.setApplicationIconImage_(image)

    def launched(self):
        self.set_icon()
        self.build_menu()
        self.build_window()
        self.monitor = AppKit.NSEvent.addLocalMonitorForEventsMatchingMask_handler_(
            EVENT_MASK, self.handle_event)
        self.window.makeKeyAndOrderFront_(None)
        AppKit.NSApp.activateIgnoringOtherApps_(True)
        self.start_connect()
        if not self.args.smoke_test:
            self.start_bridge()

    def start_bridge(self):
        bridge = kb.BridgeServer(self.handle_bridge_request)
        try:
            if bridge.bind():
                bridge.start()
                self.bridge = bridge
            else:
                self.set_notice("Another kvm_app.py is already serving Copilot requests.", "error")
        except (OSError, kb.BridgeError) as exc:
            self.set_notice(f"Copilot bridge unavailable: {exc}", "error")

    def shutdown(self):
        if self.bridge:
            self.bridge.close()
            self.bridge = None
        self.cancel_typing()
        self.stop_capture()
        self.disconnect()


class Delegate(AppKit.NSObject):
    """App and window delegate; forwards to the Python controller."""

    def applicationDidFinishLaunching_(self, notification):
        self.controller.launched()

    def typeClipboard_(self, sender):
        self.controller.paste_clipboard()

    def panelKey_(self, sender):
        self.controller.panel_key(sender.tag())

    def stickyKey_(self, sender):
        self.controller.toggle_sticky(sender)

    def stopTyping_(self, sender):
        self.controller.cancel_typing()

    def showHelp_(self, sender):
        self.controller.show_help(sender)

    def windowDidResignKey_(self, notification):
        self.controller.stop_capture()

    def applicationWillResignActive_(self, notification):
        self.controller.stop_capture()

    def applicationShouldTerminateAfterLastWindowClosed_(self, app):
        return True

    def applicationWillTerminate_(self, notification):
        self.controller.shutdown()


def refuse_report(*args, **kwargs):
    raise wk.CableError("Smoke test must not send keyboard or mouse reports.")


def set_app_name():
    """Show APP_NAME instead of "Python" in the menu bar when run outside the .app bundle."""
    bundle = Foundation.NSBundle.mainBundle()
    for info in (bundle.localizedInfoDictionary(), bundle.infoDictionary()):
        if info is not None and info.get("CFBundleName") != APP_NAME:
            info["CFBundleName"] = APP_NAME
            info["CFBundleDisplayName"] = APP_NAME


def parse_args(argv=None):
    rates = [str(rate) for rate in sorted(wk.BAUD_RATES)]
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", help="Mac serial device; auto-selects a single USB serial port")
    parser.add_argument("--baud", default="auto", choices=["auto"] + rates,
                        help="Serial rate (default: auto-detect, trying 57600 first)")
    parser.add_argument("--mouse-scale", type=float, default=1.0,
                        help="Multiplier for pointer movement (default: 1.0)")
    parser.add_argument("--smoke-test", action="store_true",
                        help="Connect, show the window briefly, query status, and exit; sends NO HID reports")
    args = parser.parse_args(argv)
    if not 0 < args.mouse_scale <= 10:
        parser.error("--mouse-scale must be greater than 0 and at most 10")
    return args


def main(argv=None):
    args = parse_args(argv)
    set_app_name()
    app = AppKit.NSApplication.sharedApplication()
    app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyRegular)
    controller = Controller(args)
    app.setDelegate_(controller.delegate)
    try:
        AppHelper.runEventLoop(installInterrupt=True)
    finally:
        controller.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
