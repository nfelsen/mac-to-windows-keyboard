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
PANEL_HEIGHT = 160
ICON_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "icon.png")
KEY_EVENTS = {AppKit.NSEventTypeKeyDown, AppKit.NSEventTypeKeyUp, AppKit.NSEventTypeFlagsChanged}
MOUSE_DOWN = {AppKit.NSEventTypeLeftMouseDown, AppKit.NSEventTypeRightMouseDown, AppKit.NSEventTypeOtherMouseDown}
MOUSE_UP = {AppKit.NSEventTypeLeftMouseUp, AppKit.NSEventTypeRightMouseUp, AppKit.NSEventTypeOtherMouseUp}
MOUSE_MOVE = {AppKit.NSEventTypeMouseMoved, AppKit.NSEventTypeLeftMouseDragged,
              AppKit.NSEventTypeRightMouseDragged, AppKit.NSEventTypeOtherMouseDragged}

MAPPING_NOTE = (
    "Paste: ⌘V types the Mac clipboard on Windows (also while controlling; Ctrl+V is not sent). "
    "US-layout ASCII only; Esc stops.\n"
    "Control → Ctrl   Option → Alt   ⌘+key → Ctrl+key   ⌘ alone → Start   Delete → Backspace\n"
    "Not forwarded (macOS keeps them): Cmd+Tab, Cmd+Space, Mission Control, "
    "Cmd+Opt+Esc, media keys (hold fn for F1–F12)."
)


class CaptureView(AppKit.NSView):
    """Large click target that shows whether Windows is being controlled."""

    def drawRect_(self, rect):
        capturing = getattr(self, "capturing", False)
        color = (AppKit.NSColor.systemGreenColor() if capturing
                 else AppKit.NSColor.colorWithCalibratedWhite_alpha_(0.22, 1.0))
        color.setFill()
        AppKit.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(self.bounds(), 10, 10).fill()
        title = getattr(self, "title", "")
        subtitle = getattr(self, "subtitle", "")
        style = AppKit.NSMutableParagraphStyle.alloc().init()
        style.setAlignment_(AppKit.NSTextAlignmentCenter)
        white = AppKit.NSColor.whiteColor()
        big = {AppKit.NSFontAttributeName: AppKit.NSFont.boldSystemFontOfSize_(22),
               AppKit.NSForegroundColorAttributeName: white, AppKit.NSParagraphStyleAttributeName: style}
        small = {AppKit.NSFontAttributeName: AppKit.NSFont.systemFontOfSize_(14),
                 AppKit.NSForegroundColorAttributeName: white, AppKit.NSParagraphStyleAttributeName: style}
        bounds = self.bounds()
        width, middle = bounds.size.width, bounds.size.height / 2
        AppKit.NSString.stringWithString_(title).drawInRect_withAttributes_(
            ((0, middle + 2), (width, 32)), big)
        AppKit.NSString.stringWithString_(subtitle).drawInRect_withAttributes_(
            ((10, middle - 44), (width - 20, 42)), small)

    @objc.python_method
    def set_state(self, capturing, title, subtitle):
        self.capturing, self.title, self.subtitle = capturing, title, subtitle
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
        self.sticky_buttons = []
        self.target = self.leds = 0
        self.cable_text = "Connecting…"
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
                 | AppKit.NSWindowStyleMaskMiniaturizable)
        self.window = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            ((200, 200), (620, 380 + PANEL_HEIGHT)), style, AppKit.NSBackingStoreBuffered, False)
        self.window.setTitle_("Mac → Windows (CH9329)")
        self.window.setDelegate_(self.delegate)
        self.window.setAcceptsMouseMovedEvents_(True)
        self.window.setReleasedWhenClosed_(False)
        content = self.window.contentView()

        self.status = AppKit.NSTextField.labelWithString_("")
        self.status.setFrame_(((20, 320 + PANEL_HEIGHT), (580, 44)))
        self.status.setFont_(AppKit.NSFont.monospacedSystemFontOfSize_weight_(12, 0))
        content.addSubview_(self.status)

        self.capture_view = CaptureView.alloc().initWithFrame_(((20, 116 + PANEL_HEIGHT), (580, 196)))
        content.addSubview_(self.capture_view)

        button = AppKit.NSButton.buttonWithTitle_target_action_(
            "Type Clipboard on Windows", self.delegate, "typeClipboard:")
        button.setFrame_(((14, 78 + PANEL_HEIGHT), (230, 32)))
        content.addSubview_(button)

        self.notice = AppKit.NSTextField.wrappingLabelWithString_("")
        self.notice.setFrame_(((250, 76 + PANEL_HEIGHT), (350, 34)))
        self.notice.setFont_(AppKit.NSFont.systemFontOfSize_(11))
        content.addSubview_(self.notice)

        note = AppKit.NSTextField.wrappingLabelWithString_(MAPPING_NOTE)
        note.setFrame_(((20, 8 + PANEL_HEIGHT), (580, 64)))
        note.setFont_(AppKit.NSFont.systemFontOfSize_(11))
        note.setTextColor_(AppKit.NSColor.secondaryLabelColor())
        content.addSubview_(note)
        self.build_panel(content)

        self.window.center()
        self.refresh()

    def panel_button(self, content, title, action, tag, frame, tooltip=None):
        button = AppKit.NSButton.buttonWithTitle_target_action_(title, self.delegate, action)
        button.setTag_(tag)
        button.setFrame_(frame)
        button.setFont_(AppKit.NSFont.systemFontOfSize_(11))
        button.setBezelStyle_(AppKit.NSBezelStyleSmallSquare)
        # Never take keyboard focus, so Space/Return in the window don't click it.
        button.setRefusesFirstResponder_(True)
        if tooltip:
            button.setToolTip_(tooltip)
        content.addSubview_(button)
        return button

    def build_panel(self, content):
        """Windows special-key buttons below the capture area; clicking them never captures."""
        left, width, height, gap = 20, 580, 24, 3
        y = 8
        label = AppKit.NSTextField.labelWithString_("Hold for next key:")
        label.setFrame_(((left, y + 4), (110, 16)))
        label.setFont_(AppKit.NSFont.systemFontOfSize_(11))
        content.addSubview_(label)
        for index, (title, _) in enumerate(kp.STICKY):
            button = self.panel_button(content, title, "stickyKey:", index,
                                       ((left + 114 + index * 64, y), (60, height)),
                                       f"Hold {title} for the next button")
            button.setButtonType_(AppKit.NSButtonTypePushOnPushOff)
            self.sticky_buttons.append(button)
        index = len(kp.BUTTONS)
        for row in reversed(kp.ROWS):
            y += height + gap
            index -= len(row)
            weights = [max(len(title), 4) + 3 for title, _ in row]
            unit = (width - gap * (len(row) - 1)) / sum(weights)
            x = left
            for column, (title, combo) in enumerate(row):
                size = weights[column] * unit
                self.panel_button(content, title, "panelKey:", index + column,
                                  ((x, y), (size, height)), combo)
                x += size + gap

    def panel_key(self, index):
        taps, label = self.panel.press(index)
        for button in self.sticky_buttons:
            button.setState_(AppKit.NSControlStateValueOff)
        error = self.start_taps(taps, f"keys {label}", check_caps=False)
        if error:
            self.set_notice(error)

    def toggle_sticky(self, index, button):
        on = self.panel.toggle(index)
        button.setState_(AppKit.NSControlStateValueOn if on else AppKit.NSControlStateValueOff)

    def build_menu(self):
        menubar = AppKit.NSMenu.alloc().init()
        item = AppKit.NSMenuItem.alloc().init()
        menubar.addItem_(item)
        menu = AppKit.NSMenu.alloc().init()
        menu.addItemWithTitle_action_keyEquivalent_("Quit", "terminate:", "q")
        item.setSubmenu_(menu)
        edit_item = AppKit.NSMenuItem.alloc().init()
        menubar.addItem_(edit_item)
        edit = AppKit.NSMenu.alloc().initWithTitle_("Edit")
        paste = edit.addItemWithTitle_action_keyEquivalent_("Type Clipboard on Windows", "typeClipboard:", "v")
        paste.setTarget_(self.delegate)
        edit_item.setSubmenu_(edit)
        AppKit.NSApp.setMainMenu_(menubar)

    def refresh(self):
        self.status.setStringValue_(self.cable_text)
        if self.typing:
            title = f"Typing {self.typing_label} on Windows…"
            subtitle = "Press Esc to stop. Other input is paused until it finishes."
        elif self.capturing:
            title = "Controlling Windows"
            subtitle = (f"Press {kc.RELEASE_SHORTCUT} together to release.\n"
                        f"{kc.PASTE_SHORTCUT} types the Mac clipboard.")
        elif self.keyboard:
            title = "Click here to control Windows"
            subtitle = f"Release with {kc.RELEASE_SHORTCUT}.  ⌘V types the Mac clipboard."
        elif self.connecting:
            title, subtitle = "Connecting to cable…", ""
        else:
            title, subtitle = "Cable not connected", "Click to retry."
        self.capture_view.set_state(self.capturing, title, subtitle)

    # ----- cable -----

    def start_connect(self):
        if self.connecting or self.keyboard:
            return
        self.connecting = True
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
        self.cable_text = f"Cable error: {exc}"
        self.refresh()
        if self.args.smoke_test:
            print(self.cable_text, file=sys.stderr, flush=True)
            self.finish_smoke_test(1)

    def connection_lost(self, exc):
        self.finish_typing(f"Cable error: {exc}")
        self.stop_capture(send=False)
        self.disconnect()
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

    def set_notice(self, text):
        self.notice.setStringValue_(text)

    def paste_clipboard(self):
        text = AppKit.NSPasteboard.generalPasteboard().stringForType_(AppKit.NSPasteboardTypeString)
        if not text:
            return self.set_notice("The Mac clipboard has no text.")
        try:
            taps = wk.encode_text(text)
        except ValueError as exc:
            return self.set_notice(str(exc))
        error = self.start_taps(taps, "the clipboard")
        if error:
            self.set_notice(error)

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
        self.set_notice(f"Typing {label} ({count} keys)… press Esc to stop.")
        self.refresh()
        return None

    def finish_typing(self, error):
        if not self.typing:
            return
        self.typing = False
        callback, self.typing_finish = self.typing_finish, None
        self.set_notice(error or f"Typed {self.typing_label} on Windows.")
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
                self.set_notice("Another kvm_app.py is already serving Copilot requests.")
        except (OSError, kb.BridgeError) as exc:
            self.set_notice(f"Copilot bridge unavailable: {exc}")

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
        self.controller.toggle_sticky(sender.tag(), sender)

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
