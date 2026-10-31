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


if __name__ == "__main__":
    unittest.main()
