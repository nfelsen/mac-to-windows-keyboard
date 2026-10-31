# mac-to-windows-keyboard

Type in a Mac terminal and have the keystrokes appear on a Windows computer, using a CH340 + CH9329 USB keyboard-emulator cable. Windows sees a standard USB keyboard, so it needs no drivers or software.

```text
Mac → (USB-C adapter) → CH340 end ── cable ── CH9329 end → Windows
```

## Requirements

- A CH9329 + CH340 "dual male" USB HID keyboard/mouse cable
- macOS with Python 3 (standard library only, nothing to install)
- Windows input layout set to **English (United States)**, with Caps Lock off

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

Other options: `--port /dev/cu.usbserial-XXXX`, `--list`, `--baud`, `--delay`.

## Notes

- Input goes to whichever Windows window has focus. Try Notepad first.
- Only US-layout ASCII text is supported. Unsupported characters are rejected before anything is sent.
- Windows output is not sent back to the Mac.
- Managed Windows devices may block unapproved USB devices.

## Tests

```bash
python3 -m unittest -v test_windows_keyboard
```
