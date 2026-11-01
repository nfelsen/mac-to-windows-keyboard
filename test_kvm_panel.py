import unittest

import kvm_bridge as kb
import kvm_panel as kp
import windows_keyboard as wk


def index_of(label):
    return [title for title, _ in kp.BUTTONS].index(label)


class PanelTests(unittest.TestCase):
    def test_every_button_parses(self):
        self.assertEqual(len(set(kp.BUTTONS)), len(kp.BUTTONS))
        for title, combo in kp.BUTTONS:
            modifier, key = kb.parse_combo(combo)
            self.assertTrue(modifier or key, title)

    def test_requested_keys_present(self):
        combos = {combo for _, combo in kp.BUTTONS}
        for combo in ["ctrl+alt+delete", "win", "win+r", "win+e", "win+l", "win+d", "win+tab",
                      "alt+tab", "alt+f4", "ctrl+shift+esc", "esc", "tab", "printscreen",
                      "win+shift+s", "insert", "delete", "home", "end", "pageup", "pagedown",
                      "up", "down", "left", "right", "enter"]:
            self.assertIn(combo, combos)
        for letter in "cvxzas":
            self.assertIn(f"ctrl+{letter}", combos)
        for i in range(1, 13):
            self.assertIn(f"f{i}", combos)

    def test_specific_reports(self):
        panel = kp.Panel()
        self.assertEqual(panel.press(index_of("Ctrl+Alt+Del")),
                         ([(wk.CTRL | wk.ALT, 0x4C)], "ctrl+alt+delete"))
        self.assertEqual(panel.press(index_of("Win")), ([(wk.WIN, 0)], "win"))
        self.assertEqual(panel.press(index_of("PrtSc")), ([(0, 0x46)], "printscreen"))
        self.assertEqual(panel.press(index_of("Snip")),
                         ([(wk.WIN | wk.SHIFT, 0x16)], "win+shift+s"))

    def test_sticky_modifiers_apply_once(self):
        panel = kp.Panel()
        self.assertTrue(panel.toggle(0))   # Ctrl
        self.assertTrue(panel.toggle(2))   # Shift
        self.assertFalse(panel.toggle(2))  # Shift off again
        self.assertTrue(panel.toggle(2))
        self.assertEqual(panel.press(index_of("→")),
                         ([(wk.CTRL | wk.SHIFT, 0x4F)], "ctrl+shift+right"))
        self.assertEqual(panel.press(index_of("→")), ([(0, 0x4F)], "right"))
        panel.toggle(3)  # Win
        self.assertEqual(panel.press(index_of("Win+R")), ([(wk.WIN, 0x15)], "win+r"))
        panel.toggle(1)  # Alt
        self.assertEqual(panel.press(index_of("F4")), ([(wk.ALT, 0x3D)], "alt+f4"))



class LayoutTests(unittest.TestCase):
    def test_sections_and_bands(self):
        names = [name for name, _ in kp.SECTIONS]
        self.assertEqual(sorted(names), sorted(n for band in kp.BANDS for n in band))
        for name, rows in kp.SECTIONS:
            self.assertEqual(len({len(row) for row in rows}), 1, name)
        sections = dict(kp.SECTIONS)
        for band in kp.BANDS:
            self.assertEqual(len({len(sections[n]) for n in band}), 1, band)
        titles = [title for title, _ in kp.BUTTONS]
        self.assertEqual(len(titles), len(set(titles)))
        self.assertEqual(len(kp.STICKY_SYMBOLS), len(kp.STICKY))
        self.assertTrue(set(kp.SYMBOLS) <= set(titles))

    def test_tooltip(self):
        self.assertEqual(kp.tooltip("Lock", "win+l"), "Lock: Win+L")
        self.assertEqual(kp.tooltip("Win+R", "win+r"), "Win+R")
        self.assertEqual(kp.tooltip("F4", "f4"), "F4")


class StatusTests(unittest.TestCase):
    def chips(self, **kw):
        args = dict(connecting=False, error="", path="/dev/cu.usbserial-2130",
                    rate=57600, firmware=0x30, target=1, leds=0)
        args.update(kw)
        return kp.status_chips(**args)

    def test_connected(self):
        chips = self.chips()
        self.assertEqual([c[0] for c in chips], ["ok", "ok", "idle"])
        self.assertEqual(chips[0][2], "usbserial-2130 · 57600")
        self.assertIn("0x30", chips[0][3])

    def test_warnings(self):
        chips = self.chips(target=0, leds=2)
        self.assertEqual([c[0] for c in chips], ["ok", "warn", "warn"])
        self.assertEqual(chips[2][2], "Caps Lock on")

    def test_error_and_connecting(self):
        self.assertEqual(self.chips(error="no cable")[0][:3],
                         ("error", "cable.connector.slash", "Cable not connected"))
        self.assertEqual(self.chips(connecting=True)[0][0], "idle")
        self.assertEqual(self.chips(path="")[0][0], "idle")

    def test_capture_message_states(self):
        def state(**kw):
            args = dict(connected=True, connecting=False, capturing=False, typing=False,
                        typing_label="the clipboard", error="",
                        release_shortcut="Ctrl+Opt+Cmd", paste_shortcut="⌘V")
            args.update(kw)
            return kp.capture_message(**args)
        self.assertEqual(state()[0], "ready")
        self.assertIn("Ctrl+Opt+Cmd", state(capturing=True)[3])
        self.assertIn("⌘V", state(capturing=True)[3])
        self.assertEqual(state(capturing=True, typing=True)[2], "Typing the clipboard on Windows…")
        self.assertEqual(state(connected=False, connecting=True)[0], "connecting")
        offline = state(connected=False, error="busy")
        self.assertEqual(offline[0], "offline")
        self.assertIn("busy", offline[3])

if __name__ == "__main__":
    unittest.main()
