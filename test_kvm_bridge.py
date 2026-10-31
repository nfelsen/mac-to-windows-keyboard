import os
import shutil
import tempfile
import threading
import unittest
from unittest.mock import patch

import kvm_bridge as kb
import windows_keyboard as wk
from test_windows_keyboard import FakePort

OK_KEY = wk.packet(0x82, b"\x00")
INFO_OK = (0x38, 1, 0)


class PrepareTests(unittest.TestCase):
    def test_combos(self):
        self.assertEqual(kb.parse_combo("ctrl+c"), (wk.CTRL, 0x06))
        self.assertEqual(kb.parse_combo("Alt+Tab"), (wk.ALT, 0x2B))
        self.assertEqual(kb.parse_combo("ctrl+shift+esc"), (wk.CTRL | wk.SHIFT, 0x29))
        self.assertEqual(kb.parse_combo("win"), (wk.WIN, 0))
        self.assertEqual(kb.parse_combo("win+r"), (wk.WIN, 0x15))
        self.assertEqual(kb.parse_combo("return"), (0, 0x28))
        self.assertEqual(kb.parse_combo("control+option+del"), (wk.CTRL | wk.ALT, 0x4C))
        self.assertEqual(kb.parse_combo("F5"), (0, 0x3E))
        for bad in ("cmd+c", "ctrl+", "ctrl+banana", ""):
            with self.assertRaises(kb.BridgeError, msg=bad):
                kb.parse_combo(bad)

    def test_keys_request(self):
        self.assertEqual(kb.prepare({"op": "keys", "combo": "ctrl+a, ctrl+c"}),
                         [(wk.CTRL, 0x04), (wk.CTRL, 0x06)])
        with self.assertRaises(kb.BridgeError):
            kb.prepare({"op": "keys", "combo": " ".join(["a"] * 11)})
        with self.assertRaises(kb.BridgeError):
            kb.prepare({"op": "keys", "combo": "  "})

    def test_type_request(self):
        self.assertEqual(kb.prepare({"op": "type", "text": "az"}), [(0, 4), (0, 0x1D)])
        self.assertEqual(kb.prepare({"op": "type", "text": "a", "press_enter": True}),
                         [(0, 4), (0, 0x28)])
        self.assertEqual(kb.prepare({"op": "type", "text": "a\nb", "multiline": True}),
                         [(0, 4), (0, 0x28), (0, 5)])

    def test_type_rejections(self):
        cases = [
            ({"op": "type", "text": ""}, "Nothing to type"),
            ({"op": "type", "text": "x" * (kb.MAX_TEXT + 1)}, "limit"),
            ({"op": "type", "text": "dir\nrm"}, "line breaks"),
            ({"op": "type", "text": "say \u201chi\u201d"}, "U\\+201C"),
            ({"op": "delete_everything"}, "Unknown request"),
        ]
        for request, pattern in cases:
            with self.assertRaisesRegex(kb.BridgeError, pattern):
                kb.prepare(request)

    def test_ready_and_describe(self):
        kb.check_ready(1, 0)
        with self.assertRaisesRegex(kb.BridgeError, "does not see"):
            kb.check_ready(0, 0)
        with self.assertRaisesRegex(kb.BridgeError, "Caps Lock"):
            kb.check_ready(1, 2)
        request = {"op": "type", "text": "ab", "press_enter": True}
        self.assertEqual(kb.describe(request, kb.prepare(request)),
                         "Typed 2 characters and pressed Enter on Windows.")


class DirectTests(unittest.TestCase):
    def run_direct(self, request, replies, info=INFO_OK):
        port = FakePort(replies)
        port.close = lambda: setattr(port, "closed", True)
        keyboard = wk.Keyboard(port, delay=0)
        with patch.object(wk, "connect", return_value=(port, keyboard, info, 57600)) as connect:
            result = kb.run_direct(request, port_path="/dev/cu.test")
        connect.assert_called_once_with("/dev/cu.test", "auto", 0.01)
        self.assertTrue(port.closed)
        return result, port.writes

    def test_types_and_releases(self):
        result, writes = self.run_direct({"op": "keys", "combo": "ctrl+c"}, [OK_KEY] * 3)
        self.assertEqual(result, "Pressed ctrl+c on Windows.")
        self.assertEqual(writes, [
            wk.packet(2, bytes((wk.CTRL, 0, 6, 0, 0, 0, 0, 0))),
            wk.packet(2, bytes(8)), wk.packet(2, bytes(8)),
        ])

    def test_status_sends_no_reports(self):
        result, writes = self.run_direct({"op": "status"}, [])
        self.assertIn("57600 baud", result)
        self.assertIn("Windows target: connected", result)
        self.assertEqual(writes, [])

    def test_refuses_when_caps_lock_on(self):
        with self.assertRaisesRegex(kb.BridgeError, "Caps Lock"):
            self.run_direct({"op": "type", "text": "hi"}, [], info=(0x38, 1, 2))

    def test_invalid_text_never_opens_port(self):
        with patch.object(wk, "connect") as connect:
            with self.assertRaises(kb.BridgeError):
                kb.run_direct({"op": "type", "text": "\u2019"}, port_path="/dev/cu.test")
        connect.assert_not_called()

    def test_busy_port(self):
        with patch.object(wk, "connect", side_effect=BlockingIOError(35, "busy")):
            with self.assertRaisesRegex(kb.BridgeError, "in use"):
                kb.run_direct({"op": "status"}, port_path="/dev/cu.test")


class SocketTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(dir="/tmp")
        self.path = os.path.join(self.dir, "sub", "kvm.sock")
        self.addCleanup(shutil.rmtree, self.dir)

    def test_no_app_falls_back_to_direct(self):
        self.assertIsNone(kb.send_to_app({"op": "status"}, self.path))
        with patch.object(kb, "run_direct", return_value="direct") as direct:
            self.assertEqual(kb.execute({"op": "status"}, self.path), "direct")
        direct.assert_called_once()

    def test_roundtrip_through_app(self):
        seen = []

        def handler(request):
            seen.append(request)
            if request["op"] == "keys":
                raise kb.BridgeError("Caps Lock is on")
            return "typed"

        server = kb.BridgeServer(handler, self.path)
        self.assertTrue(server.bind())
        self.addCleanup(server.close)
        server.start()
        self.assertEqual(os.stat(os.path.dirname(self.path)).st_mode & 0o777, 0o700)
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)
        with patch.object(kb, "run_direct") as direct:
            self.assertEqual(kb.execute({"op": "type", "text": "hi"}, self.path), "typed")
            with self.assertRaisesRegex(kb.BridgeError, "Caps Lock"):
                kb.execute({"op": "keys", "combo": "enter"}, self.path)
        direct.assert_not_called()
        self.assertEqual(seen[0], {"op": "type", "text": "hi"})
        # A second instance must not steal the socket.
        self.assertFalse(kb.BridgeServer(handler, self.path).bind())

    def test_invalid_request_not_forwarded(self):
        with patch.object(kb, "send_to_app") as send:
            with self.assertRaises(kb.BridgeError):
                kb.execute({"op": "type", "text": "a\nb"}, self.path)
        send.assert_not_called()

    def test_stale_socket_replaced(self):
        os.makedirs(os.path.dirname(self.path), mode=0o700)
        open(self.path, "w").close()
        server = kb.BridgeServer(lambda r: "ok", self.path)
        self.assertTrue(server.bind())
        server.start()
        self.addCleanup(server.close)
        self.assertEqual(kb.send_to_app({"op": "status"}, self.path), "ok")


if __name__ == "__main__":
    unittest.main()
