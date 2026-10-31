"""MCP server that lets GitHub Copilot type on the Windows PC through the cable.

Run (stdio):  uv run --directory /path/to/mac-to-windows-keyboard kvm_mcp.py

Uses the running kvm_app.py if there is one, otherwise opens the cable directly.
"""

import sys

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

import kvm_bridge as kb

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
