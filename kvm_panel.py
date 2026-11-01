"""Layout and logic for the app's on-screen Windows special-keys panel (no GUI code)."""

import kvm_bridge as kb
import windows_keyboard as wk

# Labeled sections of button rows; each button is (label, key combination for
# kvm_bridge.parse_combo). Sections sharing a band are laid out side by side.
SECTIONS = [
    ("System", [[("Ctrl+Alt+Del", "ctrl+alt+delete"), ("Task Mgr", "ctrl+shift+esc"), ("Lock", "win+l")],
                [("Alt+F4", "alt+f4"), ("Snip", "win+shift+s"), ("PrtSc", "printscreen")]]),
    ("Windows", [[("Win", "win"), ("Win+R", "win+r"), ("Win+E", "win+e")],
                 [("Win+D", "win+d"), ("Win+Tab", "win+tab"), ("Alt+Tab", "alt+tab")]]),
    ("Edit", [[("Ctrl+C", "ctrl+c"), ("Ctrl+V", "ctrl+v"), ("Ctrl+X", "ctrl+x")],
              [("Ctrl+Z", "ctrl+z"), ("Ctrl+A", "ctrl+a"), ("Ctrl+S", "ctrl+s")]]),
    ("Navigation", [[("Esc", "esc"), ("Tab", "tab"), ("Enter", "enter"), ("Bksp", "backspace"),
                     ("Ins", "insert"), ("Del", "delete"), ("↑", "up")],
                    [("Home", "home"), ("End", "end"), ("PgUp", "pageup"), ("PgDn", "pagedown"),
                     ("←", "left"), ("→", "right"), ("↓", "down")]]),
    ("Function keys", [[(f"F{i}", f"f{i}") for i in range(1, 7)],
                       [(f"F{i}", f"f{i}") for i in range(7, 13)]]),
]
BANDS = [["System", "Windows", "Edit"], ["Navigation", "Function keys"]]
ROWS = [row for _, rows in SECTIONS for row in rows]
# SF Symbols shown next to (or instead of) a label; the app falls back to text.
SYMBOLS = {
    "Lock": "lock", "Snip": "crop", "PrtSc": "camera.viewfinder", "Task Mgr": "chart.bar.xaxis",
    "Alt+F4": "xmark.square", "Win+E": "folder", "Win+D": "menubar.dock.rectangle",
    "Win+Tab": "square.grid.2x2", "Alt+Tab": "rectangle.2.swap", "Win+R": "terminal",
    "Ctrl+C": "doc.on.doc", "Ctrl+V": "doc.on.clipboard", "Ctrl+X": "scissors",
    "Ctrl+Z": "arrow.uturn.backward", "Ctrl+A": "selection.pin.in.out", "Ctrl+S": "square.and.arrow.down",
    "←": "arrow.left", "→": "arrow.right", "↑": "arrow.up", "↓": "arrow.down",
}
STICKY_SYMBOLS = ["control", "option", "shift", "squareshape.split.2x2"]
BUTTONS = [button for row in ROWS for button in row]
STICKY = [("Ctrl", wk.CTRL), ("Alt", wk.ALT), ("Shift", wk.SHIFT), ("Win", wk.WIN)]


def describe_combo(modifier, combo):
    names = [name.lower() for name, bit in STICKY if modifier & bit]
    return "+".join(names + [combo])


class Panel:
    """Sticky modifiers plus button lookup; ``press`` returns (taps, label) to send."""

    def __init__(self):
        self.sticky = 0

    def toggle(self, index):
        """Toggle sticky modifier ``index``; returns whether it is now on."""
        bit = STICKY[index][1]
        self.sticky ^= bit
        return bool(self.sticky & bit)

    def press(self, index):
        """Combine button ``index`` with the sticky modifiers, then clear them."""
        _, combo = BUTTONS[index]
        modifier, key = kb.parse_combo(combo)
        label = describe_combo(self.sticky & ~modifier, combo)
        taps = [(modifier | self.sticky, key)]
        self.sticky = 0
        return taps, label


def tooltip(title, combo):
    keys = "+".join(part.capitalize() if len(part) > 1 else part.upper() for part in combo.split("+"))
    return keys if keys.replace("+", "").lower() == title.replace("+", "").lower() else f"{title}: {keys}"


def status_chips(connecting, error, path, rate, firmware, target, leds):
    """Header status as (level, symbol, text, tooltip); level is ok, warn, error or idle."""
    if error:
        return [("error", "cable.connector.slash", "Cable not connected", error)]
    if connecting or not path:
        return [("idle", "cable.connector", "Connecting…", "Looking for the CH340 + CH9329 cable")]
    name = path.rsplit("/", 1)[-1].replace("cu.", "")
    chips = [("ok", "cable.connector", f"{name} · {rate}",
              f"{path}, {rate} baud, CH9329 firmware 0x{firmware:02X}")]
    if target == 1:
        chips.append(("ok", "pc", "Windows connected", "The Windows PC sees the CH9329 keyboard"))
    else:
        chips.append(("warn", "pc", "Windows not detected", "Replug the CH9329 end into the Windows PC"))
    if leds & 2:
        chips.append(("warn", "capslock.fill", "Caps Lock on",
                      "Caps Lock is on in Windows; pasted text is refused until it is off"))
    else:
        chips.append(("idle", "capslock", "Caps Lock off", "Windows Caps Lock state"))
    return chips


def capture_message(connected, connecting, capturing, typing, typing_label, error,
                    release_shortcut, paste_shortcut):
    """Capture card content as (state, symbol, title, subtitle)."""
    if typing:
        return ("typing", "keyboard", f"Typing {typing_label} on Windows…",
                "Press Esc to stop. Other input is paused until it finishes.")
    if capturing:
        return ("capturing", "keyboard.badge.ellipsis", "Controlling Windows",
                f"Press {release_shortcut} together to release.\n{paste_shortcut} types the Mac clipboard.")
    if connected:
        return ("ready", "cursorarrow.click.2", "Click to control Windows",
                f"Keyboard and trackpad go to Windows until you press {release_shortcut}")
    if connecting:
        return ("connecting", "cable.connector", "Connecting to the cable…", "")
    return ("offline", "cable.connector.slash", "Cable not connected",
            (error + "\n" if error else "") + "Click to retry.")
