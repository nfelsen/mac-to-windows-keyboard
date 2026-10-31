#!/usr/bin/env python3
"""Send terminal input to a CH340/CH9329 USB keyboard cable from macOS.

Usage:
    python3 windows_keyboard.py --check
    python3 windows_keyboard.py
    python3 windows_keyboard.py --live

The Windows input layout must be English (United States), with Caps Lock off.
Only input to this program is forwarded. Commands never run on the Mac.
No third-party Python packages are required.

Wire format: 57 AB, address, command, payload length, payload, sum modulo 256.
Protocol reference:
https://github.com/beijixiaohu/CH9329_COMM/tree/master/doc
"""

import argparse
import errno
import fcntl
import glob
import math
import os
import select
import signal
import sys
import termios
import time
import tty
from contextlib import contextmanager


CTRL, SHIFT, ALT, WIN = 1, 2, 4, 8
KEYS = {
    "enter": 0x28, "esc": 0x29, "backspace": 0x2A, "tab": 0x2B,
    "space": 0x2C, "insert": 0x49, "home": 0x4A, "pageup": 0x4B,
    "delete": 0x4C, "end": 0x4D, "pagedown": 0x4E,
    "right": 0x4F, "left": 0x50, "down": 0x51, "up": 0x52,
}
KEYS.update({f"f{i}": 0x39 + i for i in range(1, 13)})
CHARACTERS = {}
for index, char in enumerate("abcdefghijklmnopqrstuvwxyz"):
    CHARACTERS[char] = (0, 4 + index)
    CHARACTERS[char.upper()] = (SHIFT, 4 + index)
for index, char in enumerate("1234567890"):
    CHARACTERS[char] = (0, 0x1E + index)
for index, char in enumerate("!@#$%^&*()"):
    CHARACTERS[char] = (SHIFT, 0x1E + index)
for plain, shifted, code in (
    ("-", "_", 0x2D), ("=", "+", 0x2E), ("[", "{", 0x2F),
    ("]", "}", 0x30), ("\\", "|", 0x31), (";", ":", 0x33),
    ("'", '"', 0x34), ("`", "~", 0x35), (",", "<", 0x36),
    (".", ">", 0x37), ("/", "?", 0x38),
):
    CHARACTERS[plain] = (0, code)
    CHARACTERS[shifted] = (SHIFT, code)
CHARACTERS.update({" ": (0, 0x2C), "\t": (0, 0x2B), "\n": (0, 0x28)})
ESCAPES = {
    b"[A": "up", b"[B": "down", b"[C": "right", b"[D": "left",
    b"OA": "up", b"OB": "down", b"OC": "right", b"OD": "left",
    b"[H": "home", b"[F": "end", b"OH": "home", b"OF": "end",
    b"[1~": "home", b"[2~": "insert", b"[3~": "delete",
    b"[4~": "end", b"[5~": "pageup", b"[6~": "pagedown",
    b"OP": "f1", b"OQ": "f2", b"OR": "f3", b"OS": "f4",
    b"[15~": "f5", b"[17~": "f6", b"[18~": "f7", b"[19~": "f8",
    b"[20~": "f9", b"[21~": "f10", b"[23~": "f11", b"[24~": "f12",
}


class CableError(Exception):
    pass


def packet(command, payload=b""):
    body = bytes((0x57, 0xAB, 0, command, len(payload))) + payload
    return body + bytes((sum(body) & 0xFF,))


def encode_text(text):
    """Validate all characters before sending any of a text block."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    for char in text:
        if char not in CHARACTERS:
            raise ValueError(
                f"Unsupported character {char!r} (U+{ord(char):04X}). "
                "Only US-layout ASCII text is supported; nothing in this block was sent."
            )
    return [CHARACTERS[char] for char in text]


def parse_chord(value):
    parts = value.lower().split("+")
    modifier = 0
    for part in parts[:-1]:
        if part not in {"ctrl", "shift", "alt", "win"}:
            raise ValueError(f"Unknown modifier: {part!r}")
        modifier |= {"ctrl": CTRL, "shift": SHIFT, "alt": ALT, "win": WIN}[part]
    key = parts[-1]
    if key in KEYS:
        return modifier, KEYS[key]
    if len(key) == 1 and key in CHARACTERS:
        implied, code = CHARACTERS[key]
        return modifier | implied, code
    raise ValueError(f"Unknown key: {key!r}. Example: /key ctrl+c")


class SerialPort:
    def __init__(self, path, baud):
        self.fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        self.saved = None
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.saved = termios.tcgetattr(self.fd)
            attrs = termios.tcgetattr(self.fd)
            attrs[0] = attrs[1] = attrs[3] = 0
            attrs[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
            attrs[4] = attrs[5] = getattr(termios, f"B{baud}")
            attrs[6][termios.VMIN] = 0
            attrs[6][termios.VTIME] = 0
            termios.tcsetattr(self.fd, termios.TCSANOW, attrs)
            termios.tcflush(self.fd, termios.TCIOFLUSH)
        except BaseException:
            self.close()
            raise

    def close(self):
        try:
            if self.saved is not None:
                termios.tcsetattr(self.fd, termios.TCSANOW, self.saved)
        finally:
            os.close(self.fd)

    def write(self, data):
        deadline = time.monotonic() + 1
        while data:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([], [self.fd], [], remaining)[1]:
                raise CableError("Serial write timed out; some input may already have been sent.")
            try:
                count = os.write(self.fd, data)
            except BlockingIOError:
                continue
            if not count:
                raise CableError("Serial connection closed.")
            data = data[count:]

    def read_exact(self, size, deadline):
        data = bytearray()
        while len(data) < size:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([self.fd], [], [], remaining)[0]:
                raise CableError("No complete reply from cable (check connection and baud rate).")
            try:
                chunk = os.read(self.fd, size - len(data))
            except BlockingIOError:
                continue
            if not chunk:
                raise CableError("Cable disconnected.")
            data.extend(chunk)
        return bytes(data)


class Keyboard:
    def __init__(self, port, delay=0.02):
        self.port = port
        self.delay = delay

    def command(self, command, payload=b""):
        self.port.write(packet(command, payload))
        deadline = time.monotonic() + 1
        header = b""
        while header != b"\x57\xab":
            header = (header + self.port.read_exact(1, deadline))[-2:]
        fields = self.port.read_exact(3, deadline)
        data = self.port.read_exact(fields[2], deadline)
        checksum = self.port.read_exact(1, deadline)[0]
        if (sum(header + fields + data) & 0xFF) != checksum:
            raise CableError("Cable reply has an invalid checksum; stopping.")
        if fields[0] != 0 or fields[1] != (command | 0x80):
            raise CableError(f"Unexpected cable reply: command 0x{fields[1]:02X}.")
        return data

    def info(self):
        data = self.command(1)
        if len(data) != 8:
            raise CableError(f"Invalid device-info reply: {data.hex()}")
        # Version, enumeration state, LED flags, then five reserved bytes.
        return data[:3]

    def report(self, modifier=0, key=0):
        result = self.command(2, bytes((modifier, 0, key, 0, 0, 0, 0, 0)))
        if result != b"\x00":
            raise CableError(f"Keyboard report rejected: {result.hex() or 'empty response'}")

    def tap(self, modifier, key):
        try:
            self.report(modifier, key)
            time.sleep(self.delay)
        finally:
            # A failed acknowledgement may still mean the press reached Windows.
            self.report()
        time.sleep(self.delay)

    def text(self, text):
        for modifier, key in encode_text(text):
            self.tap(modifier, key)


def connect(path, baud, delay):
    failures = []
    rates = [int(baud)] if baud != "auto" else [9600, 115200]
    for rate in rates:
        port = SerialPort(path, rate)
        try:
            keyboard = Keyboard(port, delay)
            info = keyboard.info()
            return port, keyboard, info, rate
        except CableError as exc:
            failures.append(f"{rate}: {exc}")
        except BaseException:
            port.close()
            raise
        port.close()
    raise CableError(
        "Cable did not answer a device-info query; no keystrokes were sent.\n"
        + "\n".join(failures)
        + "\nCheck CH340 -> Mac, CH9329 -> Windows, and try --baud with the seller's setting."
    )


HELP = """Line mode: edit locally, then Enter sends the line AND Enter to Windows.
Nothing executes on your Mac. Windows receives input in its currently focused app.
  /quit             Exit (or Ctrl+C / Ctrl+D)
  /enter            Send Enter alone
  /type TEXT        Send text without Enter (also sends a literal /command)
  /key ctrl+c       Send a key/chord: ctrl, shift, alt, win; e.g. win+r
  /help             Show this help
Use --no-enter to stop appending Enter to ordinary lines.
Use --live for immediate typing, arrows, Tab, Backspace, and Ctrl shortcuts.
Set the Windows input layout to English (United States) and turn Caps Lock OFF."""


def line_mode(keyboard, no_enter):
    print(HELP)
    while True:
        try:
            line = input("windows> ")
        except EOFError:
            return
        try:
            if line == "/quit":
                return
            if line == "/help":
                print(HELP)
            elif line == "/enter":
                keyboard.tap(0, KEYS["enter"])
            elif line.startswith("/key "):
                keyboard.tap(*parse_chord(line[5:]))
            elif line.startswith("/type "):
                keyboard.text(line[6:])
            elif line.startswith("/"):
                print("Unknown local command. Use /type to send a literal leading slash.")
            else:
                keyboard.text(line + ("" if no_enter else "\n"))
        except ValueError as exc:
            print(f"Input error: {exc}", file=sys.stderr)


@contextmanager
def raw_terminal():
    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        # Bracketed paste lets us validate pasted text as a whole.
        sys.stdout.write("\x1b[?2004h")
        sys.stdout.flush()
        yield fd
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)
        sys.stdout.write("\x1b[?2004l")
        sys.stdout.flush()


def terminal_byte(fd, timeout=None):
    if not select.select([fd], [], [], timeout)[0]:
        return None
    data = os.read(fd, 1)
    if not data:
        raise CableError("Terminal input closed.")
    return data


def escape_sequence(fd):
    first = terminal_byte(fd, 0.08)
    if first is None:
        return b""
    if first not in (b"[", b"O"):
        raise ValueError("Unsupported Alt/Option sequence; it was not forwarded.")
    result = first
    while len(result) < 32:
        char = terminal_byte(fd, 0.08)
        if char is None:
            break
        result += char
        if 0x40 <= char[0] <= 0x7E:
            return result
    raise ValueError("Incomplete terminal escape sequence; it was not forwarded.")


def read_paste(fd):
    data = bytearray()
    ending = b"\x1b[201~"
    while not data.endswith(ending):
        char = terminal_byte(fd, 30)
        if char is None:
            raise CableError("Timed out reading bracketed paste; paste was not forwarded.")
        if char == b"\x1d":
            raise KeyboardInterrupt
        data.extend(char)
        if len(data) > 1024 * 1024:
            raise CableError("Paste exceeds 1 MiB; paste was not forwarded.")
    return bytes(data[:-len(ending)]).decode("utf-8")


def live_mode(keyboard):
    print(
        "LIVE: input goes immediately to Windows, including Enter and Ctrl+C.\n"
        "EXIT: Ctrl+] (Control and ]). Mac Command shortcuts are not captured.\n"
        "Watch the Windows screen: its output is NOT returned to this terminal.",
        flush=True,
    )
    with raw_terminal() as fd:
        while True:
            char = terminal_byte(fd)
            if char == b"\x1d":
                return
            try:
                if char == b"\x1b":
                    sequence = escape_sequence(fd)
                    if not sequence:
                        keyboard.tap(0, KEYS["esc"])
                    elif sequence == b"[200~":
                        keyboard.text(read_paste(fd))
                    elif sequence == b"[Z":
                        keyboard.tap(SHIFT, KEYS["tab"])
                    elif sequence in ESCAPES:
                        keyboard.tap(0, KEYS[ESCAPES[sequence]])
                    else:
                        raise ValueError(f"Unsupported escape sequence {sequence!r}; not forwarded.")
                elif char in (b"\x08", b"\x7f"):
                    keyboard.tap(0, KEYS["backspace"])
                elif char in (b"\r", b"\n", b"\t"):
                    keyboard.text(char.decode("ascii"))
                elif 1 <= char[0] <= 26:
                    keyboard.tap(CTRL, char[0] + 3)
                elif char == b"\x00":
                    keyboard.tap(CTRL, KEYS["space"])
                elif char in (b"\x1c", b"\x1e", b"\x1f"):
                    keyboard.tap(*parse_chord({
                        b"\x1c": "ctrl+\\", b"\x1e": "ctrl+shift+6",
                        b"\x1f": "ctrl+shift+-",
                    }[char]))
                elif char[0] < 128:
                    keyboard.text(char.decode("ascii"))
                else:
                    raise ValueError("Non-ASCII input is unsupported; not forwarded.")
            except ValueError as exc:
                sys.stderr.write(f"\r\nInput error: {exc}\r\n")
                sys.stderr.flush()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", help="Mac serial device; auto-selects a single USB serial port")
    parser.add_argument("--baud", default="auto", choices=["auto", "9600", "19200", "38400", "57600", "115200"])
    parser.add_argument("--check", action="store_true", help="Query cable status only; sends NO keystrokes")
    parser.add_argument("--list", action="store_true", help="List USB serial ports without opening them")
    parser.add_argument("--live", action="store_true", help="Forward keystrokes immediately; Ctrl+] exits")
    parser.add_argument("--no-enter", action="store_true", help="Line mode: send text without executing it")
    parser.add_argument("--delay", type=float, default=0.02, help="Seconds between HID reports (default: 0.02)")
    args = parser.parse_args(argv)
    if not math.isfinite(args.delay) or args.delay < 0.01:
        parser.error("--delay must be a finite number of at least 0.01 seconds")
    if args.live and args.no_enter:
        parser.error("--live and --no-enter cannot be combined")
    ports = sorted(set(glob.glob("/dev/cu.usb*") + glob.glob("/dev/cu.wchusbserial*")))
    if args.list:
        print("\n".join(ports) if ports else "No USB serial ports found.")
        return 0
    if not args.port:
        if len(ports) != 1:
            parser.error("Connect the CH340 end to Mac, then select a device with --port. Use --list.")
        args.port = ports[0]
    if not args.check and not sys.stdin.isatty():
        parser.error("Run interactively in a terminal; piped input is not supported.")
    port, keyboard, info, baud = connect(args.port, args.baud, args.delay)
    try:
        version, connected, leds = info
        print(
            f"Cable responding: {args.port}, {baud} baud, firmware 0x{version:02X}\n"
            f"Target USB: {'connected' if connected == 1 else 'NOT connected'}; "
            f"Caps Lock: {'ON' if leds & 2 else 'off'}",
            flush=True,
        )
        if args.check:
            print("Status query only. No keystrokes were sent.")
            return 0 if connected == 1 else 1
        if connected != 1:
            raise CableError("Connect the CH9329 end to Windows before typing.")
        if leds & 2:
            raise CableError("Turn Caps Lock off on Windows, then restart this script.")
        print("Focus Notepad first for a harmless typing check; use a Windows terminal for commands.")
        keyboard.report()
        try:
            if args.live:
                live_mode(keyboard)
            else:
                line_mode(keyboard, args.no_enter)
        finally:
            keyboard.report()
    finally:
        port.close()
    return 0


def stop_on_signal(signum, frame):
    raise KeyboardInterrupt


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, stop_on_signal)
    signal.signal(signal.SIGHUP, stop_on_signal)
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nDisconnected.")
        sys.exit(130)
    except (CableError, OSError, termios.error) as exc:
        detail = " Close any other app using the cable." if getattr(exc, "errno", None) in (errno.EBUSY, errno.EAGAIN) else ""
        print(
            f"\nError: {exc}.{detail}\n"
            "Stopped. If transmission had started, Windows may have received partial input. "
            "Check its screen before retrying; unplug the target end if a key is stuck.",
            file=sys.stderr,
        )
        sys.exit(1)
