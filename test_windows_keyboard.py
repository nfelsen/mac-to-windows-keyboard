import io
import os
import pty
import threading
import time
import unittest
from unittest.mock import patch

import windows_keyboard as wk


class FakePort:
    def __init__(self, replies):
        self.replies = bytearray(b"".join(replies))
        self.writes = []

    def write(self, data):
        self.writes.append(data)

    def read_exact(self, size, deadline):
        if len(self.replies) < size:
            raise wk.CableError("Test timeout")
        result = bytes(self.replies[:size])
        del self.replies[:size]
        return result


class ProtocolTests(unittest.TestCase):
    def test_known_packets(self):
        self.assertEqual(wk.packet(1).hex(), "57ab00010003")
        self.assertEqual(
            wk.packet(2, bytes(8)).hex(), "57ab00020800000000000000000c"
        )
        self.assertEqual(
            wk.packet(2, bytes((0, 0, 4, 0, 0, 0, 0, 0))).hex(),
            "57ab000208000004000000000010",
        )

    def test_all_printable_ascii(self):
        text = "".join(chr(n) for n in range(32, 127))
        self.assertEqual(len(wk.encode_text(text)), 95)
        self.assertEqual(wk.encode_text('aA"@\\|'), [
            (0, 4), (2, 4), (2, 0x34), (2, 0x1F), (0, 0x31), (2, 0x31),
        ])

    def test_newlines_normalized(self):
        self.assertEqual(wk.encode_text("\r\n\r\n"), [(0, 0x28)] * 2)

    def test_invalid_text_sends_nothing(self):
        port = FakePort([])
        with self.assertRaises(ValueError):
            wk.Keyboard(port).text("echo hello \N{RIGHT SINGLE QUOTATION MARK}")
        self.assertEqual(port.writes, [])

    def test_chords(self):
        self.assertEqual(wk.parse_chord("ctrl+c"), (1, 6))
        self.assertEqual(wk.parse_chord("win+r"), (8, 21))
        self.assertEqual(wk.parse_chord("ctrl+alt+delete"), (5, 0x4C))
        self.assertEqual(wk.parse_chord("shift+tab"), (2, 0x2B))
        with self.assertRaises(ValueError):
            wk.parse_chord("cmd+c")

    def test_status_query(self):
        port = FakePort([wk.packet(0x81, b"\x38\x01\x01" + bytes(5))])
        self.assertEqual(wk.Keyboard(port).info(), b"\x38\x01\x01")
        self.assertEqual(port.writes, [wk.packet(1)])

    def test_invalid_status_length(self):
        port = FakePort([wk.packet(0x81, b"\x30\x01\x00")])
        with self.assertRaises(wk.CableError):
            wk.Keyboard(port).info()

    def test_bad_checksum_and_response_command(self):
        for reply in (wk.packet(0x81, b"\x30\x01\x00")[:-1] + b"\x00",
                      wk.packet(0x82, b"\x00")):
            with self.subTest(reply=reply):
                with self.assertRaises(wk.CableError):
                    wk.Keyboard(FakePort([reply])).info()

    def test_tap_always_releases(self):
        port = FakePort([wk.packet(0x82, b"\x00")] * 2)
        with patch.object(wk.time, "sleep"):
            wk.Keyboard(port).tap(2, 4)
        self.assertEqual(port.writes, [
            wk.packet(2, bytes((2, 0, 4, 0, 0, 0, 0, 0))),
            wk.packet(2, bytes(8)),
        ])

    def test_releases_after_rejected_press(self):
        port = FakePort([wk.packet(0x82, b"\x01"), wk.packet(0x82, b"\x00")])
        with self.assertRaises(wk.CableError):
            wk.Keyboard(port).tap(0, 4)
        self.assertEqual(port.writes[-1], wk.packet(2, bytes(8)))

    def test_line_mode(self):
        keyboard = unittest.mock.Mock()
        with patch("builtins.input", side_effect=["echo hi", "/type test", "/key ctrl+c", "/quit"]), \
                patch("sys.stdout", new_callable=io.StringIO):
            wk.line_mode(keyboard, False)
        self.assertEqual(keyboard.text.call_args_list, [
            unittest.mock.call("echo hi\n"), unittest.mock.call("test")
        ])
        keyboard.tap.assert_called_once_with(1, 6)

    def test_no_enter(self):
        keyboard = unittest.mock.Mock()
        with patch("builtins.input", side_effect=["test", "/quit"]), \
                patch("sys.stdout", new_callable=io.StringIO):
            wk.line_mode(keyboard, True)
        keyboard.text.assert_called_once_with("test")

    def test_paste_validation(self):
        data = iter(bytes((n,)) for n in b"echo hi\r\n\x1b[201~")
        with patch.object(wk, "terminal_byte", side_effect=lambda *a: next(data)):
            self.assertEqual(wk.read_paste(0), "echo hi\r\n")

    def test_live_keys_and_exit(self):
        keyboard = unittest.mock.Mock()
        keys = [b"a", b"\x03", b"\t", b"\x7f", b"\r", b"\x1d"]
        with patch.object(wk, "raw_terminal") as context, \
                patch.object(wk, "terminal_byte", side_effect=keys), \
                patch("sys.stdout", new_callable=io.StringIO):
            context.return_value.__enter__.return_value = 0
            wk.live_mode(keyboard)
        self.assertEqual(keyboard.text.call_args_list, [
            unittest.mock.call("a"), unittest.mock.call("\t"), unittest.mock.call("\r")
        ])
        self.assertEqual(keyboard.tap.call_args_list, [
            unittest.mock.call(1, 6), unittest.mock.call(0, 0x2A)
        ])

    def test_arrow_escape(self):
        data = iter([b"[", b"A"])
        with patch.object(wk, "terminal_byte", side_effect=lambda *a: next(data)):
            self.assertEqual(wk.escape_sequence(0), b"[A")


FACTORY_CONFIG = bytes((0x80, 0x80, 0x00)) + (9600).to_bytes(4, "big") + bytes(range(7, 50))


class BaudTests(unittest.TestCase):
    def test_config_packets(self):
        self.assertEqual(wk.packet(0x08).hex(), "57ab0008000a")
        self.assertEqual(wk.packet(0x0F).hex(), "57ab000f0011")
        frame = wk.packet(0x09, wk.with_baud(FACTORY_CONFIG, 115200))
        self.assertEqual(frame[:12].hex(), "57ab0009328080000001c200")
        self.assertEqual(len(frame), 5 + 50 + 1)

    def test_with_baud_changes_only_baud_field(self):
        for rate in (9600, 115200):
            with self.subTest(rate=rate):
                new = wk.with_baud(FACTORY_CONFIG, rate)
                self.assertEqual(wk.config_baud(new), rate)
                self.assertEqual(new[:3], FACTORY_CONFIG[:3])
                self.assertEqual(new[7:], FACTORY_CONFIG[7:])
        self.assertEqual(wk.config_baud(FACTORY_CONFIG), 9600)
        with self.assertRaises(wk.CableError):
            wk.with_baud(FACTORY_CONFIG[:49], 9600)

    def test_get_and_set_config(self):
        port = FakePort([wk.packet(0x88, FACTORY_CONFIG), wk.packet(0x89, b"\x00")])
        keyboard = wk.Keyboard(port)
        self.assertEqual(keyboard.get_config(), FACTORY_CONFIG)
        keyboard.set_config(FACTORY_CONFIG)
        self.assertEqual(port.writes, [wk.packet(0x08), wk.packet(0x09, FACTORY_CONFIG)])

    def test_config_errors(self):
        with self.assertRaises(wk.CableError):
            wk.Keyboard(FakePort([wk.packet(0x88, bytes(10))])).get_config()
        with self.assertRaises(wk.CableError):
            wk.Keyboard(FakePort([wk.packet(0x89, b"\x01")])).set_config(FACTORY_CONFIG)
        port = FakePort([])
        with self.assertRaises(wk.CableError):
            wk.Keyboard(port).set_config(bytes(49))
        self.assertEqual(port.writes, [])

    def test_reset_tolerates_missing_ack(self):
        port = FakePort([])
        wk.Keyboard(port).reset()
        self.assertEqual(port.writes, [wk.packet(0x0F)])

    def test_set_baud_flow_sends_no_keyboard_reports(self):
        old_port = FakePort([
            wk.packet(0x88, FACTORY_CONFIG), wk.packet(0x89, b"\x00"), wk.packet(0x8F, b"\x00"),
        ])
        old_port.close = unittest.mock.Mock()
        new_config = wk.with_baud(FACTORY_CONFIG, 115200)
        new_port = FakePort([wk.packet(0x88, new_config)])
        new_port.close = unittest.mock.Mock()
        info = b"\x31\x01\x00"
        with patch.object(wk, "connect", return_value=(new_port, wk.Keyboard(new_port), info, 115200)) as connect, \
                patch("sys.stdout", new_callable=io.StringIO):
            result = wk.set_baud("/dev/x", old_port, wk.Keyboard(old_port), 9600, 115200, 0.01, settle=0)
        self.assertEqual(result[0], new_port)
        self.assertEqual(result[2], info)
        old_port.close.assert_called_once()
        new_port.close.assert_not_called()
        connect.assert_called_once_with("/dev/x", "115200", 0.01)
        self.assertEqual(old_port.writes, [
            wk.packet(0x08), wk.packet(0x09, new_config), wk.packet(0x0F),
        ])
        for frame in old_port.writes + new_port.writes:
            self.assertNotEqual(frame[3], 0x02)

    def test_set_baud_detects_unchanged_stored_rate(self):
        old_port = FakePort([wk.packet(0x88, FACTORY_CONFIG), wk.packet(0x89, b"\x00")])
        old_port.close = unittest.mock.Mock()
        new_port = FakePort([wk.packet(0x88, FACTORY_CONFIG)])
        new_port.close = unittest.mock.Mock()
        with patch.object(wk, "connect", return_value=(new_port, wk.Keyboard(new_port), b"", 115200)), \
                patch("sys.stdout", new_callable=io.StringIO):
            with self.assertRaises(wk.CableError):
                wk.set_baud("/dev/x", old_port, wk.Keyboard(old_port), 9600, 115200, 0.01, settle=0)
        new_port.close.assert_called_once()

    def test_auto_detect_prefers_57600_then_9600(self):
        self.assertEqual(wk.BAUD_RATES[:2], [57600, 9600])
        self.assertEqual(sorted(wk.BAUD_RATES), [9600, 19200, 38400, 57600, 115200])
        opened = []

        class Port:
            def __init__(self, path, rate):
                opened.append(rate)
                self.rate = rate

            def close(self):
                pass

        def info(keyboard):
            if keyboard.port.rate != 9600:
                raise wk.CableError("no reply")
            return b"\x31\x01\x00"

        with patch.object(wk, "SerialPort", Port), patch.object(wk.Keyboard, "info", info):
            port, keyboard, _, rate = wk.connect("/dev/x", "auto", 0.01)
        self.assertEqual(opened, [57600, 9600])
        self.assertEqual(rate, 9600)
        self.assertEqual(keyboard.delay, 0.01)

    def test_defaults_and_argument_validation(self):
        self.assertEqual(wk.Keyboard(FakePort([])).delay, 0.01)
        with patch("sys.stderr", new_callable=io.StringIO):
            for argv in (["--set-baud", "14400"], ["--set-baud", "115200", "--live"],
                         ["--set-baud", "9600", "--check"]):
                with self.subTest(argv=argv), self.assertRaises(SystemExit):
                    wk.main(argv)

    def test_serial_roundtrip_with_fragmented_reply(self):
        master, slave = pty.openpty()
        port = wk.SerialPort(os.ttyname(slave), 9600)
        received = []

        def device():
            received.append(os.read(master, 6))
            for byte in wk.packet(0x81, b"\x30\x01\x00" + bytes(5)):
                os.write(master, bytes((byte,)))
                time.sleep(0.001)

        worker = threading.Thread(target=device, daemon=True)
        try:
            worker.start()
            self.assertEqual(wk.Keyboard(port).info(), b"\x30\x01\x00")
            worker.join(timeout=2)
            self.assertFalse(worker.is_alive())
            self.assertEqual(received, [wk.packet(1)])
        finally:
            port.close()
            os.close(slave)
            os.close(master)


if __name__ == "__main__":
    unittest.main()
