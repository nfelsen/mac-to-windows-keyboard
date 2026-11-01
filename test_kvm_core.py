import threading
import unittest

import kvm_core as kc
import windows_keyboard as wk
from test_windows_keyboard import FakePort

OK_KEY = wk.packet(0x82, b"\x00")
OK_MOUSE = wk.packet(0x85, b"\x00")


class MappingTests(unittest.TestCase):
    def test_letters_digits_and_specials(self):
        self.assertEqual(kc.MAC_TO_HID[0x00], 0x04)  # a
        self.assertEqual(kc.MAC_TO_HID[0x06], 0x1D)  # z
        self.assertEqual(kc.MAC_TO_HID[0x1D], 0x27)  # 0
        self.assertEqual(kc.MAC_TO_HID[0x24], 0x28)  # return
        self.assertEqual(kc.MAC_TO_HID[0x33], 0x2A)  # delete -> backspace
        self.assertEqual(kc.MAC_TO_HID[0x75], 0x4C)  # forward delete
        self.assertEqual(kc.MAC_TO_HID[0x7E], 0x52)  # up
        self.assertEqual(kc.MAC_TO_HID[0x7A], 0x3A)  # F1
        self.assertEqual(kc.MAC_TO_HID[0x6F], 0x45)  # F12

    def test_table_is_unique_and_complete(self):
        values = list(kc.MAC_TO_HID.values())
        self.assertEqual(len(values), len(set(values)))
        for usage in range(0x04, 0x46):  # letters through F12, except non-US hash and Caps Lock
            if usage not in (0x32, kc.HID_CAPS_LOCK):
                self.assertIn(usage, values, hex(usage))

    def test_modifier_byte_left_right(self):
        self.assertEqual(kc.modifier_byte(0), 0)
        self.assertEqual(kc.modifier_byte(kc.NS_CONTROL | 0x0001), kc.LCTRL)
        self.assertEqual(kc.modifier_byte(kc.NS_CONTROL | 0x2000), kc.RCTRL)
        self.assertEqual(kc.modifier_byte(kc.NS_SHIFT | 0x0004), kc.RSHIFT)
        self.assertEqual(kc.modifier_byte(kc.NS_OPTION | 0x0020), kc.LALT)
        self.assertEqual(kc.modifier_byte(kc.NS_COMMAND | 0x0008), kc.LWIN)
        self.assertEqual(kc.modifier_byte(kc.NS_COMMAND | 0x0010), kc.RWIN)

    def test_modifier_byte_without_device_bits(self):
        flags = kc.NS_SHIFT | kc.NS_CONTROL | kc.NS_OPTION | kc.NS_COMMAND
        self.assertEqual(kc.modifier_byte(flags), kc.LSHIFT | kc.LCTRL | kc.LALT | kc.LWIN)

    def test_release_shortcut(self):
        self.assertTrue(kc.is_release_shortcut(kc.NS_CONTROL | kc.NS_OPTION | kc.NS_COMMAND))
        self.assertFalse(kc.is_release_shortcut(kc.NS_CONTROL | kc.NS_OPTION))
        self.assertFalse(kc.is_release_shortcut(kc.NS_COMMAND | kc.NS_SHIFT))

    def test_paste_shortcut(self):
        self.assertTrue(kc.is_paste_shortcut(kc.MAC_V, kc.NS_COMMAND | 0x0008))
        self.assertTrue(kc.is_paste_shortcut(kc.MAC_V, kc.NS_COMMAND | kc.NS_SHIFT))
        self.assertFalse(kc.is_paste_shortcut(kc.MAC_V, 0))
        self.assertFalse(kc.is_paste_shortcut(kc.MAC_V, kc.NS_CONTROL | kc.NS_OPTION))
        self.assertFalse(kc.is_paste_shortcut(kc.MAC_V, kc.NS_COMMAND | kc.NS_CONTROL))
        self.assertFalse(kc.is_paste_shortcut(kc.MAC_V, kc.NS_COMMAND | kc.NS_OPTION))
        self.assertFalse(kc.is_paste_shortcut(0x08, kc.NS_COMMAND))  # Cmd+C still forwarded


class KeyboardStateTests(unittest.TestCase):
    def test_press_release_and_limits(self):
        state = kc.KeyboardState()
        self.assertEqual(state.press(4), [(0, (4,))])
        self.assertEqual(state.press(4), [])
        for usage in range(5, 10):
            state.press(usage)
        self.assertEqual(state.press(10), [])  # seventh key ignored
        self.assertEqual(state.release(4), [(0, (5, 6, 7, 8, 9))])
        self.assertEqual(state.release(4), [])
        self.assertEqual(state.set_modifier(kc.LCTRL), [(kc.LCTRL, (5, 6, 7, 8, 9))])
        self.assertEqual(state.set_modifier(kc.LCTRL), [])
        state.clear()
        self.assertEqual(state.report(), (0, ()))


class LazyWinTests(unittest.TestCase):
    E = 0x08

    def test_command_alone_waits_then_taps_win(self):
        state = kc.KeyboardState()
        self.assertEqual(state.set_modifier(kc.LWIN), [])
        self.assertEqual(state.set_modifier(0), [(kc.LWIN, ()), (0, ())])

    def test_command_shortcut_sends_ctrl_with_key(self):
        state = kc.KeyboardState()
        reports = state.set_modifier(kc.RWIN)
        reports += state.press(self.E) + state.release(self.E) + state.set_modifier(0)
        self.assertEqual(reports, [(kc.RCTRL, ()), (kc.RCTRL, (self.E,)), (kc.RCTRL, ()), (0, ())])

    def test_command_shift_key_sends_ctrl_shift(self):
        state = kc.KeyboardState()
        reports = state.set_modifier(kc.LWIN | kc.LSHIFT) + state.press(self.E)
        self.assertEqual(reports[-1], (kc.LCTRL | kc.LSHIFT, (self.E,)))
        self.assertFalse(any(modifier & kc.WIN_BITS for modifier, _ in reports))

    def test_chords_with_other_modifiers_do_not_tap_win(self):
        state = kc.KeyboardState()
        reports = state.set_modifier(kc.LWIN) + state.set_modifier(kc.LWIN | kc.LSHIFT)
        reports += state.set_modifier(kc.LWIN) + state.set_modifier(0)
        self.assertEqual(reports, [(kc.LSHIFT, ()), (0, ())])
        reports = state.set_modifier(kc.LCTRL) + state.set_modifier(kc.LCTRL | kc.LWIN)
        reports += state.set_modifier(kc.LCTRL) + state.set_modifier(0)
        self.assertEqual(reports, [(kc.LCTRL, ()), (0, ())])

    def test_click_while_command_held_does_not_tap_win(self):
        state = kc.KeyboardState()
        state.set_modifier(kc.LWIN)
        state.use_win()
        self.assertEqual(state.set_modifier(0), [])

    def test_resync_after_cleared_state(self):
        # Typing clears state while Command may still be held; the next keyDown resyncs.
        state = kc.KeyboardState()
        state.set_modifier(kc.LWIN)
        state.clear()
        self.assertEqual(state.set_modifier(kc.LWIN) + state.press(self.E),
                         [(kc.LCTRL, ()), (kc.LCTRL, (self.E,))])
        state.clear()
        self.assertEqual(state.set_modifier(0), [])  # Command released after typing

    def test_cmd_v_paste_never_sends_win(self):
        """Mirror kvm_app: Command down, ⌘V intercepted for paste, Command up."""
        state, outbox, sent = kc.KeyboardState(), kc.Outbox(), []

        def send(reports):
            sent.extend(reports)  # record before release_all can drop them from the queue
            for report in reports:
                outbox.keys(report)
        send(state.set_modifier(kc.modifier_byte(kc.NS_COMMAND | 0x0008)))
        self.assertTrue(kc.is_paste_shortcut(kc.MAC_V, kc.NS_COMMAND))
        state.clear()  # start_taps
        outbox.release_all()
        outbox.taps(wk.encode_text("az group list"))
        send(state.set_modifier(0))  # Command released
        self.assertEqual(sent, [])
        self.assertFalse([item for item in outbox.items
                          if item[0] in ("keys", "tap") and item[1] & kc.WIN_BITS])


class MotionTests(unittest.TestCase):
    def test_accumulator_keeps_fractions(self):
        acc = kc.Accumulator(0.5)
        self.assertEqual([acc.add(1) for _ in range(4)], [0, 1, 0, 1])
        acc = kc.Accumulator(1.0)
        self.assertEqual([acc.add(-0.6), acc.add(-0.6)], [0, -1])

    def test_scroll_steps(self):
        acc = kc.Accumulator()
        self.assertEqual(kc.scroll_steps(acc, 10, True), 0)
        self.assertEqual(kc.scroll_steps(acc, 10, True), 1)
        self.assertEqual(kc.scroll_steps(acc, -45, True), -2)
        self.assertEqual(kc.scroll_steps(acc, 0.1, False), 1)
        self.assertEqual(kc.scroll_steps(acc, -3, False), -3)
        self.assertEqual(kc.scroll_steps(acc, 0, False), 0)

    def test_clamp_chunks(self):
        self.assertEqual(kc.clamp_chunks(0, 0, 0), [(0, 0, 0)])
        self.assertEqual(kc.clamp_chunks(300, -5, 1), [(127, -5, 1), (127, 0, 0), (46, 0, 0)])


class OutboxTests(unittest.TestCase):
    def drain(self, box):
        items = []
        while True:
            item = box.take(0)
            if item is None:
                return items
            items.append(item)

    def test_order_merge_and_split(self):
        box = kc.Outbox()
        box.mouse(0, 100, 0)
        box.mouse(0, 100, 5)
        box.keys((0, (4,)))
        box.mouse(1)
        box.mouse(1, 3, 3)
        box.mouse(1, -1, 0)
        self.assertEqual(self.drain(box), [
            ("move", 0, 127, 5, 0), ("move", 0, 73, 0, 0),
            ("keys", 0, (4,)), ("buttons", 1, 0, 0, 0), ("move", 1, 2, 3, 0),
        ])

    def test_moves_with_different_buttons_not_merged(self):
        box = kc.Outbox()
        box.mouse(0, 1, 0)
        box.mouse(1, 1, 0)
        self.assertEqual(len(self.drain(box)), 2)

    def test_release_all_drops_pending(self):
        box = kc.Outbox()
        box.keys((kc.LCTRL, (4,)))
        box.mouse(0, 5, 5)
        box.release_all()
        self.assertEqual(self.drain(box), [("keys", 0, ()), ("buttons", 0, 0, 0, 0)])

    def test_close(self):
        box = kc.Outbox()
        box.keys((0, ()))
        box.close()
        self.assertEqual(box.take(0), ("keys", 0, ()))
        with self.assertRaises(EOFError):
            box.take(0)

    def test_text_queues_taps_then_release_and_done(self):
        box = kc.Outbox()
        self.assertEqual(box.text("aB\r\n"), 3)
        self.assertEqual(self.drain(box), [
            ("tap", 0, 4), ("tap", wk.SHIFT, 5), ("tap", 0, 0x28),
            ("keys", 0, ()), ("text_done",),
        ])

    def test_unsupported_text_queues_nothing(self):
        box = kc.Outbox()
        with self.assertRaisesRegex(ValueError, "U\\+2019.*nothing in this block was sent"):
            box.text("it\N{RIGHT SINGLE QUOTATION MARK}s")
        self.assertEqual(self.drain(box), [])

    def test_release_all_cancels_text(self):
        box = kc.Outbox()
        box.text("hello")
        box.take(0)
        box.release_all()
        self.assertEqual(self.drain(box), [("keys", 0, ()), ("buttons", 0, 0, 0, 0)])


    def test_pending_taps_counts_down(self):
        box = kc.Outbox()
        box.text("abc")
        self.assertEqual(box.pending_taps(), 3)
        box.take(0)
        self.assertEqual(box.pending_taps(), 2)
        box.release_all()
        self.assertEqual(box.pending_taps(), 0)

class SenderTests(unittest.TestCase):
    def test_packets(self):
        port = FakePort([OK_KEY, OK_MOUSE, OK_MOUSE, OK_MOUSE])
        keyboard = wk.Keyboard(port)
        kc.send_item(keyboard, ("keys", kc.LCTRL | kc.LSHIFT, (4, 5)))
        kc.send_item(keyboard, ("move", 1, -1, 2, 0))
        kc.send_item(keyboard, ("move", 0, 0, 0, -1))
        kc.send_item(keyboard, ("buttons", 0, 0, 0, 0))
        self.assertEqual(port.writes, [
            wk.packet(0x02, bytes((3, 0, 4, 5, 0, 0, 0, 0))),
            wk.packet(0x05, bytes((1, 1, 0xFF, 2, 0))),
            wk.packet(0x05, bytes((1, 0, 0, 0, 0xFF))),
            wk.packet(0x05, bytes((1, 0, 0, 0, 0))),
        ])

    def test_mouse_range_checked(self):
        port = FakePort([])
        with self.assertRaises(ValueError):
            wk.Keyboard(port).mouse(0, 128, 0)
        self.assertEqual(port.writes, [])

    def test_rejected_mouse_report(self):
        port = FakePort([wk.packet(0x85, b"\x01")])
        with self.assertRaises(wk.CableError):
            wk.Keyboard(port).mouse(0, 1, 0)

    def test_thread_sends_polls_and_stops(self):
        info = wk.packet(0x81, b"\x38\x01\x00" + bytes(5))
        port = FakePort([OK_KEY, info])
        box = kc.Outbox()
        polled = threading.Event()
        infos = []

        def on_info(value):
            infos.append(value)
            box.close()
            polled.set()

        sender = kc.Sender(wk.Keyboard(port), box, on_info, self.fail, info_interval=0.01)
        box.keys((0, ()))
        sender.start()
        self.assertTrue(polled.wait(2))
        sender.join(2)
        self.assertFalse(sender.is_alive())
        self.assertEqual(infos, [b"\x38\x01\x00"])
        self.assertEqual(port.writes, [wk.packet(0x02, bytes(8)), wk.packet(0x01)])

    def test_thread_types_text_and_reports_done(self):
        port = FakePort([OK_KEY] * 5)  # two taps (press+release each) + final release
        box = kc.Outbox()
        done = threading.Event()
        sender = kc.Sender(wk.Keyboard(port, delay=0), box, lambda _: None, self.fail,
                           info_interval=5, on_text_done=done.set)
        box.text("A1")
        sender.start()
        self.assertTrue(done.wait(2))
        box.close()
        sender.join(2)
        self.assertEqual(port.writes, [
            wk.packet(0x02, bytes((wk.SHIFT, 0, 4, 0, 0, 0, 0, 0))), wk.packet(0x02, bytes(8)),
            wk.packet(0x02, bytes((0, 0, 0x1E, 0, 0, 0, 0, 0))), wk.packet(0x02, bytes(8)),
            wk.packet(0x02, bytes(8)),
        ])

    def test_thread_reports_errors(self):
        port = FakePort([])  # no reply -> CableError
        box = kc.Outbox()
        errors = []
        sender = kc.Sender(wk.Keyboard(port), box, lambda _: None, errors.append, info_interval=5)
        box.mouse(0, 1, 1)
        sender.start()
        sender.join(2)
        self.assertFalse(sender.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], wk.CableError)


if __name__ == "__main__":
    unittest.main()
