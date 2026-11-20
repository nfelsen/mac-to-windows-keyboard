"""MCP server that lets GitHub Copilot type on the Windows PC through the cable.

Run (stdio):  uv run --directory /path/to/mac-to-windows-keyboard kvm_mcp.py

Uses the running kvm_app.py if there is one, otherwise opens the cable directly.
"""

import sys

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

import kvm_bridge as kb
import kvm_teams as kt

mcp = MCPServer(
    "windows-keyboard",
    instructions=(
        "Types on the user's Windows PC through a USB keyboard cable. You cannot see the "
        "Windows screen or any output; ask the user what happened after each action."
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
    You cannot see the Windows screen or any command output: after typing, ask the user what
    happened (or to paste the output) before continuing.
    """
    return run({"op": "type", "text": text, "press_enter": press_enter, "multiline": multiline})


@mcp.tool(annotations=SENDS_KEYS)
def press_keys(combo: str) -> str:
    """Press a key or key combination on the user's Windows PC, e.g. "enter", "esc",
    "ctrl+c", "ctrl+shift+esc", "alt+tab", "win+r", "win", "f5", "up".

    Modifiers: ctrl, shift, alt, win. Up to 10 combinations separated by spaces or commas
    are pressed in order, e.g. "ctrl+a ctrl+c". All keys are released afterwards.
    You cannot see the Windows screen: ask the user what happened before continuing.
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
    no attendees; then pass its onlineMeeting.joinUrl. Only https://teams.microsoft.com/... or msteams: links of at most 259
    characters are accepted (the Windows Run box limit); prefer the short
    https://teams.microsoft.com/meet/... join link over long /l/meetup-join/ links.
    """
    try:
        return kt.set_link(url)
    except (kb.BridgeError, OSError) as exc:
        raise ToolError(str(exc)) from None


@mcp.tool(annotations=SENDS_KEYS)
def start_teams_screen_share() -> str:
    """Make the Windows PC join the user's saved Microsoft Teams meeting and share its screen,
    so the Windows screen can be seen in the Teams window on the Mac.

    Only the saved meeting link is ever opened (set in the app, with kvm_teams.py --set-url,
    or with set_teams_meeting_link); this tool takes no link. Sends Win+R, the link, Enter, then the saved
    steps (by default: wait, Enter to join, wait, Ctrl+Shift+E to share). Takes about 25
    seconds. You cannot see Windows: afterwards, check a screenshot of the Teams window on
    the Mac if you have one, otherwise ask the user whether the share started. The user may
    need to click "Join now" or pick the screen in Teams' share picker by hand.
    """
    return run_teams(lambda: kt.share_actions(kt.load_config()))


@mcp.tool(annotations=SENDS_KEYS)
def stop_teams_screen_share() -> str:
    """Leave the Teams call on the Windows PC (Ctrl+Shift+H), which also stops sharing.
    Teams must be the focused window on Windows. You cannot see Windows: verify via a
    screenshot of the Mac Teams window if available, otherwise ask the user."""
    return run_teams(kt.leave_actions)


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
