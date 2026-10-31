"""Platform-independent logic for the Mac -> Windows CH9329 desktop app.

Translates macOS key codes and modifier flags into USB HID reports, turns
pointer/scroll deltas into CH9329 relative-mouse reports, and sends them in
order from a background thread. No GUI imports, so it is unit-testable.
"""

import threading
from collections import deque

from windows_keyboard import CableError, encode_text

# macOS virtual key code (kVK_*) -> USB HID keyboard usage. Physical keys, so
# the Windows layout decides which character appears (use English (US)).
MAC_TO_HID = {
    0x00: 0x04, 0x0B: 0x05, 0x08: 0x06, 0x02: 0x07, 0x0E: 0x08, 0x03: 0x09,  # a b c d e f
    0x05: 0x0A, 0x04: 0x0B, 0x22: 0x0C, 0x26: 0x0D, 0x28: 0x0E, 0x25: 0x0F,  # g h i j k l
    0x2E: 0x10, 0x2D: 0x11, 0x1F: 0x12, 0x23: 0x13, 0x0C: 0x14, 0x0F: 0x15,  # m n o p q r
    0x01: 0x16, 0x11: 0x17, 0x20: 0x18, 0x09: 0x19, 0x0D: 0x1A, 0x07: 0x1B,  # s t u v w x
    0x10: 0x1C, 0x06: 0x1D,                                                  # y z
    0x12: 0x1E, 0x13: 0x1F, 0x14: 0x20, 0x15: 0x21, 0x17: 0x22,              # 1 2 3 4 5
    0x16: 0x23, 0x1A: 0x24, 0x1C: 0x25, 0x19: 0x26, 0x1D: 0x27,              # 6 7 8 9 0
    0x24: 0x28, 0x35: 0x29, 0x33: 0x2A, 0x30: 0x2B, 0x31: 0x2C,              # return esc delete tab space
    0x1B: 0x2D, 0x18: 0x2E, 0x21: 0x2F, 0x1E: 0x30, 0x2A: 0x31,              # - = [ ] \
    0x29: 0x33, 0x27: 0x34, 0x32: 0x35, 0x2B: 0x36, 0x2F: 0x37, 0x2C: 0x38,  # ; ' ` , . /
    0x0A: 0x64,                                                              # ISO section key
    0x7A: 0x3A, 0x78: 0x3B, 0x63: 0x3C, 0x76: 0x3D, 0x60: 0x3E, 0x61: 0x3F,  # F1-F6
    0x62: 0x40, 0x64: 0x41, 0x65: 0x42, 0x6D: 0x43, 0x67: 0x44, 0x6F: 0x45,  # F7-F12
    0x69: 0x68, 0x6B: 0x69, 0x71: 0x6A, 0x6A: 0x6B, 0x40: 0x6C, 0x4F: 0x6D,  # F13-F18
    0x50: 0x6E, 0x5A: 0x6F,                                                  # F19 F20
    0x72: 0x49, 0x73: 0x4A, 0x74: 0x4B, 0x75: 0x4C, 0x77: 0x4D, 0x79: 0x4E,  # help(insert) home pgup fwd-del end pgdn
    0x7C: 0x4F, 0x7B: 0x50, 0x7D: 0x51, 0x7E: 0x52,                          # right left down up
    0x47: 0x53, 0x4B: 0x54, 0x43: 0x55, 0x4E: 0x56, 0x45: 0x57, 0x4C: 0x58,  # clear(numlock) / * - + enter
    0x53: 0x59, 0x54: 0x5A, 0x55: 0x5B, 0x56: 0x5C, 0x57: 0x5D,              # keypad 1-5
    0x58: 0x5E, 0x59: 0x5F, 0x5B: 0x60, 0x5C: 0x61, 0x52: 0x62,              # keypad 6-9 0
    0x41: 0x63, 0x51: 0x67,                                                  # keypad . =
}
MAC_CAPS_LOCK = 0x39
HID_CAPS_LOCK = 0x39

# NSEvent device-dependent modifier bits (NX_DEVICE*KEYMASK), which
# distinguish left/right, mapped to CH9329/HID modifier bits.
# Control -> Ctrl, Option -> Alt, Command -> Win.
LCTRL, LSHIFT, LALT, LWIN, RCTRL, RSHIFT, RALT, RWIN = (1 << n for n in range(8))
DEVICE_FLAG_TO_HID = (
    (0x0001, LCTRL), (0x2000, RCTRL),
    (0x0002, LSHIFT), (0x0004, RSHIFT),
    (0x0020, LALT), (0x0040, RALT),
    (0x0008, LWIN), (0x0010, RWIN),
)
# Device-independent masks, used when a keyboard doesn't report left/right bits.
NS_SHIFT, NS_CONTROL, NS_OPTION, NS_COMMAND = 1 << 17, 1 << 18, 1 << 19, 1 << 20

RELEASE_SHORTCUT = "Control + Option + Command"
PASTE_SHORTCUT = "⌘V"
MAC_V, MAC_ESCAPE = 0x09, 0x35


def is_paste_shortcut(key_code, flags):
    """Cmd+V (without Control/Option) types the Mac clipboard; Win+V is never sent."""
    return (key_code == MAC_V and flags & NS_COMMAND
            and not flags & (NS_CONTROL | NS_OPTION))


def modifier_byte(flags):
    """HID modifier byte for an NSEvent modifierFlags value."""
    result = 0
    for device_bit, hid_bit in DEVICE_FLAG_TO_HID:
        if flags & device_bit:
            result |= hid_bit
    for generic, left, right in ((NS_SHIFT, LSHIFT, RSHIFT), (NS_CONTROL, LCTRL, RCTRL),
                                 (NS_OPTION, LALT, RALT), (NS_COMMAND, LWIN, RWIN)):
        if flags & generic and not result & (left | right):
            result |= left
    return result


def is_release_shortcut(flags):
    return all(flags & mask for mask in (NS_CONTROL, NS_OPTION, NS_COMMAND))


class KeyboardState:
    """Tracks held keys and modifiers; methods return a report tuple when it changes."""

    def __init__(self):
        self.modifier = 0
        self.keys = []

    def report(self):
        return self.modifier, tuple(self.keys)

    def set_modifier(self, modifier):
        if modifier == self.modifier:
            return None
        self.modifier = modifier
        return self.report()

    def press(self, usage):
        if usage in self.keys or len(self.keys) >= 6:
            return None
        self.keys.append(usage)
        return self.report()

    def release(self, usage):
        if usage not in self.keys:
            return None
        self.keys.remove(usage)
        return self.report()

    def clear(self):
        self.modifier = 0
        self.keys = []


class Accumulator:
    """Scales deltas and carries the fractional remainder between events."""

    def __init__(self, scale=1.0):
        self.scale = scale
        self.remainder = 0.0

    def add(self, delta):
        total = delta * self.scale + self.remainder
        whole = int(total)
        self.remainder = total - whole
        return whole

    def reset(self):
        self.remainder = 0.0


def scroll_steps(accumulator, delta_y, precise, pixels_per_step=20.0):
    """Wheel steps for a scroll event. Trackpads report pixels; wheels report lines."""
    if precise:
        return accumulator.add(delta_y / pixels_per_step)
    if delta_y == 0:
        return 0
    steps = int(round(delta_y))
    return steps if steps else (1 if delta_y > 0 else -1)


def clamp_chunks(dx, dy, wheel):
    """Split deltas into CH9329-sized (-127..127) pieces."""
    chunks = []
    while dx or dy or wheel or not chunks:
        step = tuple(max(-127, min(127, value)) for value in (dx, dy, wheel))
        chunks.append(step)
        dx, dy, wheel = dx - step[0], dy - step[1], wheel - step[2]
    return chunks


class Outbox:
    """Ordered, thread-safe report queue. Consecutive motion is merged."""

    def __init__(self):
        self.items = deque()
        self.cond = threading.Condition()
        self.closed = False

    def _push(self, item):
        with self.cond:
            self.items.append(item)
            self.cond.notify()

    def keys(self, report):
        self._push(("keys",) + tuple(report))

    def mouse(self, buttons, dx=0, dy=0, wheel=0):
        with self.cond:
            last = self.items[-1] if self.items else None
            if (dx or dy or wheel) and last and last[0] == "move" and last[1] == buttons:
                self.items[-1] = ("move", buttons, last[2] + dx, last[3] + dy, last[4] + wheel)
            else:
                self.items.append(("move" if dx or dy or wheel else "buttons", buttons, dx, dy, wheel))
            self.cond.notify()

    def text(self, text):
        """Queue validated US-layout text as key taps; returns the tap count.

        Raises ValueError before queueing anything if a character is unsupported.
        ``release_all`` cancels whatever has not been typed yet.
        """
        taps = encode_text(text)
        with self.cond:
            self.items.extend(("tap", modifier, key) for modifier, key in taps)
            self.items.append(("keys", 0, ()))
            self.items.append(("text_done",))
            self.cond.notify()
        return len(taps)

    def release_all(self):
        """Drop anything pending and release every key and button."""
        with self.cond:
            self.items.clear()
            self.items.append(("keys", 0, ()))
            self.items.append(("buttons", 0, 0, 0, 0))
            self.cond.notify()

    def close(self):
        with self.cond:
            self.closed = True
            self.cond.notify_all()

    def take(self, timeout=None):
        """Next item, ``None`` on timeout, or raises EOFError once closed and drained."""
        with self.cond:
            if not self.items and not self.closed:
                self.cond.wait(timeout)
            if not self.items:
                if self.closed:
                    raise EOFError
                return None
            item = self.items.popleft()
            if item[0] == "move":
                chunks = clamp_chunks(*item[2:])
                if len(chunks) > 1:
                    rest = [sum(c[i] for c in chunks[1:]) for i in range(3)]
                    self.items.appendleft(("move", item[1], *rest))
                item = ("move", item[1]) + chunks[0]
            return item


def send_item(keyboard, item):
    if item[0] == "keys":
        keyboard.keys(item[1], item[2])
    elif item[0] == "tap":
        keyboard.tap(item[1], item[2])
    else:
        keyboard.mouse(*item[1:])


class Sender(threading.Thread):
    """Sends queued reports; polls device info while idle.

    ``on_info(info)`` and ``on_error(exc)`` are called from this thread.
    After an error the thread stops; nothing further is sent.
    """

    def __init__(self, keyboard, outbox, on_info, on_error, info_interval=2.0, on_text_done=None):
        super().__init__(daemon=True)
        self.keyboard = keyboard
        self.outbox = outbox
        self.on_info = on_info
        self.on_error = on_error
        self.info_interval = info_interval
        self.on_text_done = on_text_done or (lambda: None)

    def run(self):
        try:
            while True:
                try:
                    item = self.outbox.take(self.info_interval)
                except EOFError:
                    return
                if item is None:
                    self.on_info(self.keyboard.info())
                elif item[0] == "text_done":
                    self.on_text_done()
                else:
                    send_item(self.keyboard, item)
        except (CableError, OSError) as exc:
            self.on_error(exc)
