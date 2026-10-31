"""Typing requests shared by the MCP server (kvm_mcp.py) and the desktop app.

Requests are small JSON objects:
    {"op": "status"}
    {"op": "type", "text": "...", "press_enter": false, "multiline": false}
    {"op": "keys", "combo": "ctrl+c"}

If kvm_app.py is running it owns the serial port, so requests go to it over a
Unix socket in a user-only directory. Otherwise the port is opened directly
for the duration of one request. Either way the text is validated in full
before anything is sent, Windows must see the keyboard with Caps Lock off,
and every key is released afterwards.
"""

import json
import os
import socket
import stat
import threading

import windows_keyboard as wk

MAX_TEXT = 4000
MAX_COMBOS = 10
SOCKET_DIR = os.path.expanduser("~/Library/Application Support/mac-to-windows-keyboard")
SOCKET_PATH = os.path.join(SOCKET_DIR, "kvm.sock")
ENTER = (0, wk.KEYS["enter"])

MODIFIER_NAMES = {
    "ctrl": wk.CTRL, "control": wk.CTRL, "shift": wk.SHIFT,
    "alt": wk.ALT, "option": wk.ALT, "win": wk.WIN, "windows": wk.WIN,
}
KEY_ALIASES = {
    "return": "enter", "escape": "esc", "del": "delete", "ins": "insert",
    "pgup": "pageup", "pgdn": "pagedown", "bksp": "backspace", "prtsc": "printscreen",
    "arrowup": "up", "arrowdown": "down", "arrowleft": "left", "arrowright": "right",
}


class BridgeError(Exception):
    """A request was refused or failed; nothing (more) was typed."""


def parse_combo(combo):
    """One chord such as 'ctrl+c', 'alt+tab', 'win', or 'enter' -> (modifier, key)."""
    parts = [part.strip() for part in combo.lower().split("+")]
    if not all(parts):
        raise BridgeError(f"Invalid key combination: {combo!r}")
    modifier = 0
    for part in parts[:-1]:
        if part not in MODIFIER_NAMES:
            raise BridgeError(f"Unknown modifier {part!r} in {combo!r}; use ctrl, shift, alt, or win.")
        modifier |= MODIFIER_NAMES[part]
    last = KEY_ALIASES.get(parts[-1], parts[-1])
    if last in MODIFIER_NAMES:
        return modifier | MODIFIER_NAMES[last], 0
    if last in wk.KEYS:
        return modifier, wk.KEYS[last]
    if len(last) == 1 and last in wk.CHARACTERS:
        implied, code = wk.CHARACTERS[last]
        return modifier | implied, code
    raise BridgeError(f"Unknown key {parts[-1]!r} in {combo!r}.")


def prepare(request):
    """Validate a typing request and return its (modifier, key) taps."""
    op = request.get("op")
    if op == "type":
        text = request.get("text")
        if not isinstance(text, str) or not text:
            raise BridgeError("Nothing to type.")
        if len(text) > MAX_TEXT:
            raise BridgeError(f"Text is {len(text)} characters; the limit is {MAX_TEXT}. Nothing was typed.")
        if ("\n" in text or "\r" in text) and not request.get("multiline"):
            raise BridgeError("Text contains line breaks, which Windows would run as Enter. "
                              "Send one line, or set multiline=true if each line should be entered. "
                              "Nothing was typed.")
        try:
            taps = wk.encode_text(text)
        except ValueError as exc:
            raise BridgeError(str(exc)) from None
        return taps + [ENTER] if request.get("press_enter") else taps
    if op == "keys":
        combo = request.get("combo")
        if not isinstance(combo, str) or not combo.strip():
            raise BridgeError("No key combination given.")
        combos = combo.replace(",", " ").split()
        if len(combos) > MAX_COMBOS:
            raise BridgeError(f"At most {MAX_COMBOS} key combinations per request.")
        return [parse_combo(item) for item in combos]
    raise BridgeError(f"Unknown request {op!r}.")


def check_ready(target, leds):
    if target != 1:
        raise BridgeError("Windows does not see the CH9329 keyboard (replug the Windows end). Nothing was typed.")
    if leds & 2:
        raise BridgeError("Caps Lock is on in Windows; turn it off first. Nothing was typed.")


def status_text(path, rate, firmware, target, leds, via):
    return (f"Cable {path} at {rate} baud (firmware 0x{firmware:02X}), via {via}. "
            f"Windows target: {'connected' if target == 1 else 'NOT connected'}. "
            f"Caps Lock: {'ON' if leds & 2 else 'off'}.")


def describe(request, taps):
    if request["op"] == "keys":
        return f"Pressed {request['combo'].strip()} on Windows."
    enter = " and pressed Enter" if request.get("press_enter") else ""
    return f"Typed {len(taps) - (1 if enter else 0)} characters{enter} on Windows."


# ----- direct serial access (app not running) -----

def run_direct(request, port_path=None, baud="auto"):
    taps = None if request.get("op") == "status" else prepare(request)
    if not port_path:
        ports = wk.usb_serial_ports()
        if len(ports) != 1:
            raise BridgeError(f"Found {len(ports)} USB serial ports; plug in the CH340 end of the cable.")
        port_path = ports[0]
    try:
        port, keyboard, info, rate = wk.connect(port_path, baud, 0.01)
    except BlockingIOError:
        raise BridgeError("The cable is in use by another program (for example windows_keyboard.py "
                          "or an older kvm_app.py). Quit it, or restart kvm_app.py.") from None
    except (wk.CableError, OSError) as exc:
        raise BridgeError(str(exc)) from None
    try:
        firmware, target, leds = info
        if taps is None:
            return status_text(port_path, rate, firmware, target, leds, "direct serial")
        check_ready(target, leds)
        try:
            for modifier, key in taps:
                keyboard.tap(modifier, key)
        finally:
            keyboard.report()
        return describe(request, taps)
    except wk.CableError as exc:
        raise BridgeError(f"Cable error while typing: {exc}") from None
    finally:
        port.close()


# ----- Unix socket between the MCP server and the app -----

def ensure_socket_dir(path=SOCKET_DIR):
    os.makedirs(path, mode=0o700, exist_ok=True)
    info = os.lstat(path)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise BridgeError(f"{path} is not a directory owned by you.")
    if stat.S_IMODE(info.st_mode) & 0o077:
        os.chmod(path, 0o700)


def read_line(sock, limit=64 * 1024):
    data = b""
    while not data.endswith(b"\n"):
        chunk = sock.recv(4096)
        if not chunk:
            break
        data += chunk
        if len(data) > limit:
            raise BridgeError("Request too large.")
    return data


def send_to_app(request, path=SOCKET_PATH, timeout=None):
    """Send a request to a running kvm_app.py; ``None`` if the app isn't listening."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.settimeout(2)
        try:
            sock.connect(path)
        except OSError:
            return None  # no app listening (missing, stale, or not a socket)
        sock.settimeout(timeout)
        sock.sendall(json.dumps(request).encode() + b"\n")
        line = read_line(sock)
    except socket.timeout:
        raise BridgeError("kvm_app.py did not finish in time; check the app window.") from None
    finally:
        sock.close()
    if not line:
        raise BridgeError("kvm_app.py closed the connection without answering.")
    reply = json.loads(line)
    if not reply.get("ok"):
        raise BridgeError(reply.get("error", "kvm_app.py refused the request."))
    return reply["message"]


def execute(request, path=SOCKET_PATH, port_path=None, baud="auto"):
    """Run a request through the app if it is running, else directly. Returns a message."""
    taps = [] if request.get("op") == "status" else prepare(request)
    # Allow ~50 ms per tap through the app, plus slack for a queued paste.
    message = send_to_app(request, path, timeout=10 + 0.05 * len(taps))
    if message is not None:
        return message
    return run_direct(request, port_path, baud)


class BridgeServer(threading.Thread):
    """Serves requests from kvm_mcp.py inside the app.

    ``handler(request)`` runs on a worker thread, must return a message string
    or raise BridgeError, and is responsible for thread-safety.
    """

    def __init__(self, handler, path=SOCKET_PATH):
        super().__init__(daemon=True)
        self.handler = handler
        self.path = path
        self.sock = None

    def bind(self):
        """Claim the socket; returns False if another app instance already serves it."""
        ensure_socket_dir(os.path.dirname(self.path))
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            probe.connect(self.path)
            return False
        except OSError:
            pass  # missing, stale, or not a socket: safe to replace
        finally:
            probe.close()
        if os.path.lexists(self.path):
            os.unlink(self.path)
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(self.path)
        os.chmod(self.path, 0o600)
        self.sock.listen(4)
        return True

    def run(self):
        sock = self.sock
        while sock is not None:
            try:
                conn, _ = sock.accept()
            except OSError:
                return
            threading.Thread(target=self.serve, args=(conn,), daemon=True).start()

    def serve(self, conn):
        with conn:
            try:
                conn.settimeout(5)
                request = json.loads(read_line(conn))
                conn.settimeout(None)
                if not isinstance(request, dict):
                    raise BridgeError("Invalid request.")
                reply = {"ok": True, "message": self.handler(request)}
            except BridgeError as exc:
                reply = {"ok": False, "error": str(exc)}
            except (ValueError, OSError) as exc:
                reply = {"ok": False, "error": f"Invalid request: {exc}"}
            try:
                conn.sendall(json.dumps(reply).encode() + b"\n")
            except OSError:
                pass

    def close(self):
        if self.sock:
            self.sock.close()
            self.sock = None
            try:
                os.unlink(self.path)
            except FileNotFoundError:
                pass
