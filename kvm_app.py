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
KEY_EVENTS = {AppKit.NSEventTypeKeyDown, AppKit.NSEventTypeKeyUp, AppKit.NSEventTypeFlagsChanged}
MOUSE_DOWN = {AppKit.NSEventTypeLeftMouseDown, AppKit.NSEventTypeRightMouseDown, AppKit.NSEventTypeOtherMouseDown}
MOUSE_UP = {AppKit.NSEventTypeLeftMouseUp, AppKit.NSEventTypeRightMouseUp, AppKit.NSEventTypeOtherMouseUp}
MOUSE_MOVE = {AppKit.NSEventTypeMouseMoved, AppKit.NSEventTypeLeftMouseDragged,
              AppKit.NSEventTypeRightMouseDragged, AppKit.NSEventTypeOtherMouseDragged}

MAPPING_NOTE = (
    "Paste: ⌘V types the Mac clipboard on Windows (also while controlling; Win+V is not sent). "
    "US-layout ASCII only; Esc stops.\n"
    "Control → Ctrl   Option → Alt   Command → Windows key   Delete → Backspace\n"
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
            ((200, 200), (620, 380)), style, AppKit.NSBackingStoreBuffered, False)
        self.window.setTitle_("Mac → Windows (CH9329)")
        self.window.setDelegate_(self.delegate)
        self.window.setAcceptsMouseMovedEvents_(True)
        self.window.setReleasedWhenClosed_(False)
        content = self.window.contentView()

        self.status = AppKit.NSTextField.labelWithString_("")
        self.status.setFrame_(((20, 320), (580, 44)))
        self.status.setFont_(AppKit.NSFont.monospacedSystemFontOfSize_weight_(12, 0))
        content.addSubview_(self.status)

        self.capture_view = CaptureView.alloc().initWithFrame_(((20, 116), (580, 196)))
        content.addSubview_(self.capture_view)

        button = AppKit.NSButton.buttonWithTitle_target_action_(
            "Type Clipboard on Windows", self.delegate, "typeClipboard:")
        button.setFrame_(((14, 78), (230, 32)))
        content.addSubview_(button)

        self.notice = AppKit.NSTextField.wrappingLabelWithString_("")
        self.notice.setFrame_(((250, 76), (350, 34)))
        self.notice.setFont_(AppKit.NSFont.systemFontOfSize_(11))
        content.addSubview_(self.notice)

        note = AppKit.NSTextField.wrappingLabelWithString_(MAPPING_NOTE)
        note.setFrame_(((20, 8), (580, 64)))
        note.setFont_(AppKit.NSFont.systemFontOfSize_(11))
        note.setTextColor_(AppKit.NSColor.secondaryLabelColor())
        content.addSubview_(note)

        self.window.center()
        self.refresh()

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
            title = "Typing clipboard on Windows…"
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
        self.typing = False
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
        if self.typing:
            return
        if not self.keyboard:
            return self.set_notice("Cable not connected; nothing was typed.")
        if self.target != 1:
            return self.set_notice("Windows does not see the keyboard; nothing was typed.")
        if self.leds & 2:
            return self.set_notice("Turn Caps Lock off on Windows first; nothing was typed.")
        text = AppKit.NSPasteboard.generalPasteboard().stringForType_(AppKit.NSPasteboardTypeString)
        if not text:
            return self.set_notice("The Mac clipboard has no text.")
        try:
            wk.encode_text(text)
        except ValueError as exc:
            return self.set_notice(str(exc))
        # Drop any keys Windows thinks are held (e.g. Ctrl+Alt from the shortcut).
        self.keys.clear()
        self.buttons = 0
        self.outbox.release_all()
        count = self.outbox.text(text)
        self.typing = True
        self.set_notice(f"Typing {count} characters… press Esc to stop.")
        self.refresh()

    def cancel_typing(self):
        if not self.typing:
            return
        self.typing = False
        self.keys.clear()
        if self.outbox:
            self.outbox.release_all()
        self.set_notice("Stopped typing; the rest of the clipboard was not sent.")
        self.refresh()

    def text_done(self):
        if self.typing:
            self.typing = False
            self.set_notice("Clipboard typed on Windows.")
            self.refresh()

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
        self.cancel_typing()
        if not self.capturing:
            return
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
            if buttons != self.buttons:
                self.buttons = buttons
                self.outbox.mouse(buttons)
        elif kind == AppKit.NSEventTypeScrollWheel:
            steps = kc.scroll_steps(self.scroll, event.scrollingDeltaY(),
                                    event.hasPreciseScrollingDeltas())
            if steps:
                self.outbox.mouse(self.buttons, wheel=steps)
        return None

    def send_keys(self, report):
        if report is not None:
            self.outbox.keys(report)

    def launched(self):
        self.build_menu()
        self.build_window()
        self.monitor = AppKit.NSEvent.addLocalMonitorForEventsMatchingMask_handler_(
            EVENT_MASK, self.handle_event)
        self.window.makeKeyAndOrderFront_(None)
        AppKit.NSApp.activateIgnoringOtherApps_(True)
        self.start_connect()

    def shutdown(self):
        self.stop_capture()
        self.disconnect()


class Delegate(AppKit.NSObject):
    """App and window delegate; forwards to the Python controller."""

    def applicationDidFinishLaunching_(self, notification):
        self.controller.launched()

    def typeClipboard_(self, sender):
        self.controller.paste_clipboard()

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
