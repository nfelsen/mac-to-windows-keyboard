# mac-to-windows-keyboard

Type in a Mac terminal, or use the Mac keyboard and trackpad from a desktop window, and control a Windows computer through a CH340 + CH9329 USB keyboard/mouse-emulator cable. Windows sees a standard USB keyboard and mouse, so it needs no drivers or software.

```text
Mac → (USB-C adapter) → CH340 end ── cable ── CH9329 end → Windows
```

## Requirements

- A CH9329 + CH340 "dual male" USB HID keyboard/mouse cable. Tested with [this CH9329+CH340 UART/TTL serial-to-USB HID keyboard and mouse cable (dual male USB-A)](https://www.aliexpress.us/item/3256811932570354.html).
- macOS with Python 3 (standard library only, nothing to install) for the terminal script
- [uv](https://docs.astral.sh/uv/) for the desktop app (it installs PyObjC automatically)
- Windows input layout set to **English (United States)**, with Caps Lock off

## Desktop app (keyboard + trackpad)

`kvm_app.py` opens a window. Click it, and the Mac keyboard, trackpad, mouse buttons, and scrolling control Windows. Press **Control + Option + Command** together to give control back to the Mac. Switching to another app (for example with Cmd+Tab) or closing the window also releases control.

```bash
uv run kvm_app.py
```

The first run creates `.venv` and installs the dependencies from `pyproject.toml`. The window shows the serial port, baud rate, whether Windows sees the keyboard, and the Windows Caps Lock state. Useful options:

```bash
uv run kvm_app.py --mouse-scale 1.5        # faster pointer
uv run kvm_app.py --port /dev/cu.usbserial-2130 --baud 57600
uv run kvm_app.py --smoke-test             # connect, query status, exit; sends no keyboard or mouse reports
```

Key mapping: Control → Ctrl, Option → Alt, Command → Windows key, Delete → Backspace, fn+Delete → Delete, Help/Insert → Insert. Keys are sent by physical position, so keep the Windows layout on English (US). Windows applies its own key repeat, pointer speed, and scroll direction for wheel steps; the app follows your Mac scroll direction.

No macOS Accessibility or Input Monitoring permission is needed, because the app only reads events sent to its own window. As a result, macOS keeps a few system shortcuts and they are not forwarded: Cmd+Tab, Cmd+Space, Mission Control (Ctrl+arrows), Cmd+Opt+Esc, and media keys (hold fn to send F1–F12). Some Windows shortcuts that also involve Control + Option + Command can't be sent, because that chord releases control.

## Usage

Check the connection (sends no keystrokes):

```bash
python3 windows_keyboard.py --check
```

Line mode: edit a line locally, and Enter sends it plus Enter to Windows.

```bash
python3 windows_keyboard.py
```

| Command | Action |
|---|---|
| `/quit` | Exit (or Ctrl+C / Ctrl+D) |
| `/enter` | Send Enter alone |
| `/type TEXT` | Send text without Enter |
| `/key ctrl+c` | Send a key or chord (`ctrl`, `shift`, `alt`, `win`), e.g. `win+r` |
| `/help` | Show help |

Add `--no-enter` to send lines without pressing Enter.

Live mode forwards each keystroke immediately, including arrows, Tab, Backspace, and Ctrl shortcuts:

```bash
python3 windows_keyboard.py --live
```

Press **Ctrl+]** to exit live mode. Ctrl+C is forwarded to Windows.

Other options: `--port /dev/cu.usbserial-XXXX`, `--list`, `--baud`, `--delay` (default 0.01 s between key reports).

## Baud rate

The CH9329 ships at 9600 baud. The script auto-detects the rate, trying 57600 first, then 9600, 115200, 38400, and 19200. Switching the cable to 57600 makes typing faster:

```bash
python3 windows_keyboard.py --set-baud 57600
```

This reads the chip's 50-byte parameter config (command `0x08`), changes only the baud field (bytes 3–6, big-endian), writes it back (`0x09`), soft-resets the chip (`0x0F`), and reconnects at the new rate to verify. It sends no keystrokes. The setting persists across power cycles. After the reset, Windows may not re-detect the keyboard; if `--check` shows `Target USB: NOT connected` or the cable stops answering, unplug and replug the CH9329 (Windows) end. To go back to the factory rate, run `--set-baud 9600`. Supported rates: 9600, 19200, 38400, 57600, 115200. 115200 is available but WCH notes it is unsupported if the chip runs at 3.3 V, and it was unreliable on the tested cable.

## Notes

- Input goes to whichever Windows window has focus. Try Notepad first.
- Only US-layout ASCII text is supported. Unsupported characters are rejected before anything is sent.
- Windows output is not sent back to the Mac.
- Managed Windows devices may block unapproved USB devices.

## Tests

```bash
python3 -m unittest -v test_windows_keyboard test_kvm_core
```
