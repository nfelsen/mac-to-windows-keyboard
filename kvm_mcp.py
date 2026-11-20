"""MCP server that lets GitHub Copilot type on the Windows PC through the cable.

Run (stdio):  uv run --directory /path/to/mac-to-windows-keyboard kvm_mcp.py

Uses the running kvm_app.py if there is one, otherwise opens the cable directly.
"""

import sys

from mcp.server.mcpserver import Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

import kvm_bridge as kb
import kvm_teams as kt
import kvm_viewer as kv

mcp = MCPServer(
    "windows-keyboard",
    instructions=(
        "Types on the user's Windows PC through a USB keyboard cable. To see the Windows "
        "screen, use windows_screenshot (needs Windows sharing its screen to the saved Teams "
        "meeting); otherwise ask the user what happened after each action."
    ),
)
SENDS_KEYS = ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=True)


def run(request):
    try:
        return kb.execute(request)
    except kb.BridgeError as exc:
        raise ToolError(str(exc)) from None


@mcp.tool(annotations=SENDS_KEYS)
def type_on_windows(text: str, press_enter: bool = False, multiline: bool = False) -> str:
    """Type text on the user's Windows PC as if from a US keyboard, into whichever window has focus.

    Only printable US-layout ASCII, tabs, and line breaks are supported; if any character
    is unsupported (curly quotes, em dashes, non-English letters), nothing is typed. Max
    4000 characters. Enter is pressed only if press_enter is true. Line breaks are rejected
    unless multiline is true, because each one acts as Enter. All keys are released afterwards.
    To see the result, call windows_screenshot if Windows is sharing its screen to the saved
    Teams meeting; otherwise ask the user what happened (or to paste the output).
    """
    return run({"op": "type", "text": text, "press_enter": press_enter, "multiline": multiline})


@mcp.tool(annotations=SENDS_KEYS)
def press_keys(combo: str) -> str:
    """Press a key or key combination on the user's Windows PC, e.g. "enter", "esc",
    "ctrl+c", "ctrl+shift+esc", "alt+tab", "win+r", "win", "f5", "up".

    Modifiers: ctrl, shift, alt, win. Up to 10 combinations separated by spaces or commas
    are pressed in order, e.g. "ctrl+a ctrl+c". All keys are released afterwards.
    To see the result, call windows_screenshot if Windows is sharing its screen to Teams;
    otherwise ask the user what happened before continuing.
    """
    return run({"op": "keys", "combo": combo})


def run_teams(actions_for):
    try:
        actions = actions_for()
        done = kt.run_actions(actions, kb.execute)
    except kb.BridgeError as exc:
        raise ToolError(f"{exc} {kt.VERIFY_HINT}") from None
    return f"Sent: {' -> '.join(done)}. {kt.VERIFY_HINT}"


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False))
def set_teams_meeting_link(url: str) -> str:
    """Save the Microsoft Teams meeting link that start_teams_screen_share opens on Windows.
    Sends nothing to Windows.

    Use a meeting the user owns, for example a one-time meeting you just created for them
    through Microsoft Graph: POST /me/events with isOnlineMeeting true, onlineMeetingProvider
    "teamsForBusiness", showAs "free" (so their calendar stays free), isReminderOn false, and
    no attendees. Pass the short https://teams.microsoft.com/meet/<id>?p=... link found in the
    event body; onlineMeeting.joinUrl is usually a long /l/meetup-join/ link over 259
    characters, so use it only if it fits. Only https://teams.microsoft.com/... or msteams:
    links are accepted, at most 259 characters once typed as msteams:// (the Windows Run box
    limit). Windows opens the link in its installed, signed-in Teams app, not a browser.
    """
    try:
        return kt.set_link(url)
    except (kb.BridgeError, OSError) as exc:
        raise ToolError(str(exc)) from None


@mcp.tool(annotations=SENDS_KEYS)
def start_teams_screen_share() -> str:
    """Make the Windows PC join the user's saved Microsoft Teams meeting and share its screen,
    so the Windows screen can be seen on the Mac (windows_screenshot, or the Teams window).

    Only the saved meeting link is ever opened (set in the app, with kvm_teams.py --set-url,
    or with set_teams_meeting_link); this tool takes no link. Sends Win+R, the link as
    msteams://... (so the installed, signed-in Teams app opens it, not a browser), Enter,
    then the saved steps (by default: wait 30 seconds, then Ctrl+Shift+E to share). Takes
    about 35 seconds. Before calling, tell the user to expect two clicks on Windows: when
    their account is already in the meeting (on the Mac), Teams asks "How would you like to
    join on this device?" and they must click "Add this device" during the wait (never
    "Transfer", which would move their Mac call); otherwise they click "Join now". After
    Ctrl+Shift+E, Teams' share picker has "Entire screen" preselected but keystrokes don't
    press its Share button, so they click "Share". Do not send Enter or Tab to get past
    either screen. Afterwards, check with windows_screenshot (the app's hidden viewer may first
    need admitting from the lobby), a screenshot of the Mac Teams window, or ask the user.
    """
    return run_teams(lambda: kt.share_actions(kt.load_config()))


@mcp.tool(annotations=SENDS_KEYS)
def stop_teams_screen_share() -> str:
    """Leave the Teams call on the Windows PC (Ctrl+Shift+H), which also stops sharing.
    Teams must be the focused window on Windows. Once sharing stops, windows_screenshot
    can no longer see Windows; ask the user to confirm the call ended."""
    return run_teams(kt.leave_actions)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True))
def windows_screenshot() -> Image:
    """Return the current Windows screen as a JPEG image. Sends nothing to Windows.

    Needs the Mac2Win KVM app running and Windows sharing its screen in the saved Teams
    meeting (start_teams_screen_share). The app watches the share through a hidden,
    receive-only Teams guest named "Mac2Win viewer" (no mic, camera, or audio). On the first
    call it joins the saved meeting and lands in the lobby: this call then fails and asks you
    to have the user admit "Mac2Win viewer" in Teams (Teams shows it as an Unverified guest);
    call again once they have. Frames are the shared screen at full resolution, a moment
    behind live. Use it to read command output after type_on_windows or press_keys.
    """
    try:
        message = kb.send_to_app({"op": "screenshot"}, kb.SOCKET_PATH, timeout=30)
        if message is None:
            raise kv.ViewerError("The Mac2Win KVM app is not running; the Windows screen viewer runs "
                                 "inside it. Ask the user to open it.")
        data, _ = kv.read_screenshot(message)
    except (kb.BridgeError, OSError) as exc:
        raise ToolError(str(exc)) from None
    return Image(data=data, format="jpeg")


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def windows_keyboard_status() -> str:
    """Check the Mac -> Windows keyboard cable without typing anything: serial port, baud
    rate, whether Windows sees the keyboard, and whether Caps Lock is on."""
    return run({"op": "status"})


def main():
    if "--status" in sys.argv[1:]:
        try:
            print(kb.execute({"op": "status"}))
        except kb.BridgeError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
        return 0
    mcp.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
