"""Layout and logic for the app's on-screen Windows special-keys panel (no GUI code)."""

import kvm_bridge as kb
import windows_keyboard as wk

# Rows of (button label, key combination for kvm_bridge.parse_combo).
ROWS = [
    [("Ctrl+Alt+Del", "ctrl+alt+delete"), ("Task Mgr", "ctrl+shift+esc"),
     ("Alt+Tab", "alt+tab"), ("Alt+F4", "alt+f4"), ("Win", "win"), ("Win+R", "win+r"),
     ("Win+E", "win+e"), ("Win+L", "win+l"), ("Win+D", "win+d"), ("Win+Tab", "win+tab")],
    [("Snip", "win+shift+s"), ("PrtSc", "printscreen"), ("Esc", "esc"), ("Tab", "tab"),
     ("Enter", "enter"), ("Bksp", "backspace"), ("Ins", "insert"), ("Del", "delete"),
     ("Home", "home"), ("End", "end"), ("PgUp", "pageup"), ("PgDn", "pagedown")],
    [("←", "left"), ("↑", "up"), ("↓", "down"), ("→", "right"),
     ("Ctrl+C", "ctrl+c"), ("Ctrl+V", "ctrl+v"), ("Ctrl+X", "ctrl+x"),
     ("Ctrl+Z", "ctrl+z"), ("Ctrl+A", "ctrl+a"), ("Ctrl+S", "ctrl+s")],
    [(f"F{i}", f"f{i}") for i in range(1, 13)],
]
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
