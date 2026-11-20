#!/usr/bin/env python3
"""Join a saved Microsoft Teams meeting on Windows and share its screen.

Lets an assistant on the Mac read Windows output by looking at the Teams
window on the Mac. The Windows PC only receives keystrokes through the cable:
Win+R, the saved meeting link, Enter, then Teams shortcuts. Only the one saved
meeting link is ever opened. The user still clicks twice on Windows: "Add this
device" (or "Join now") to join, and "Share" in Teams' share picker.

    python3 kvm_teams.py --set-url "https://teams.microsoft.com/l/meetup-join/..."
    python3 kvm_teams.py --show

An assistant can also save a link with the set_teams_meeting_link MCP tool,
for example after creating a fresh meeting through Microsoft Graph.

The steps live in teams.json next to the app's socket and can be edited:
    "open"          Win+R, type the meeting link, Enter
    "wait 30"       pause (seconds, at most 60)
    "keys ctrl+shift+e"   press keys, like the press_keys MCP tool
"""

import argparse
import json
import os
import sys
import threading
import time
from typing import Callable, List, NamedTuple, Optional
from urllib.parse import urlsplit

import kvm_bridge as kb
import windows_keyboard as wk

CONFIG_PATH = os.path.join(kb.SOCKET_DIR, "teams.json")
# The Windows Run box (Win+R) accepts at most 259 characters.
MAX_URL = 259
MAX_WAIT = 60.0
MAX_STEPS = 30
RUN_BOX_WAIT = 1.5
TEAMS_HOST = "teams.microsoft.com"
# No blind Enter to join: when the user's account is already in the meeting
# (on the Mac), Teams asks "How would you like to join on this device?" and a
# blind Enter or Tab could pick "Transfer", moving the Mac's call to Windows.
# The user clicks "Add this device" (or "Join now") during the wait instead.
# Ctrl+Shift+E (share) and Ctrl+Shift+H (leave) are documented Teams shortcuts.
DEFAULT_SHARE_STEPS = ["open", "wait 30", "keys ctrl+shift+e"]
# Earlier default, still saved in older teams.json files; replaced on load.
OLD_DEFAULT_SHARE_STEPS = ["open", "wait 15", "keys enter", "wait 8", "keys ctrl+shift+e"]
LEAVE_STEPS = ["keys ctrl+shift+h"]
VERIFY_HINT = ("Check the result with windows_screenshot (or a screenshot of the Teams window on "
               "the Mac); otherwise ask the user. Expect two clicks by the user on "
               "Windows: \"Add this device\" (or \"Join now\") to join, then \"Share\" in Teams' "
               "share picker (Entire screen is preselected, but keys don't press Share).")


class TeamsError(kb.BridgeError):
    """Invalid Teams settings, or a step failed."""


class Action(NamedTuple):
    label: str
    request: Optional[dict]  # None means wait
    seconds: float = 0.0


class TeamsConfig(NamedTuple):
    meeting_url: str
    share_steps: List[str]


def validate_url(url) -> str:
    """Return the cleaned meeting link, or raise TeamsError."""
    if not isinstance(url, str) or not url.strip():
        raise TeamsError("No Teams meeting link is saved. Set one in the app (Teams → Set Meeting "
                         "Link…) or run: python3 kvm_teams.py --set-url URL")
    url = url.strip()
    if any(ch.isspace() for ch in url) or not url.isascii() or not url.isprintable():
        raise TeamsError("The meeting link must be one line of plain ASCII without spaces.")
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme == "https":
        if (parts.hostname or "").lower() != TEAMS_HOST or parts.username or parts.port:
            raise TeamsError(f"Only https://{TEAMS_HOST}/... meeting links are allowed.")
    elif scheme == "msteams":
        if parts.netloc and parts.netloc.lower() != TEAMS_HOST:
            raise TeamsError(f"msteams: links must point at {TEAMS_HOST}.")
    else:
        raise TeamsError("The meeting link must start with https://teams.microsoft.com/ or msteams:.")
    if not parts.path.strip("/"):
        raise TeamsError("That is not a meeting link (no path).")
    typed = desktop_link(url)
    if len(typed) > MAX_URL:
        raise TeamsError(f"The meeting link is typed as {len(typed)} characters ({typed[:20]}…); the Windows "
                         f"Run box takes at most {MAX_URL}. Use the short "
                         "https://teams.microsoft.com/meet/... link from the invite.")
    try:
        wk.encode_text(url)
    except ValueError as exc:
        raise TeamsError(str(exc)) from None
    return url


def desktop_link(url: str) -> str:
    """Rewrite https://teams.microsoft.com/... as msteams://... so Windows opens the
    installed, signed-in Teams app instead of a browser (which joins as a guest)."""
    if urlsplit(url).scheme.lower() == "https":
        return "msteams://" + url.split("://", 1)[1]
    return url


def parse_steps(steps, meeting_url: Optional[str]) -> List[Action]:
    """Validate step strings and expand them into actions."""
    if not isinstance(steps, list) or not steps or len(steps) > MAX_STEPS:
        raise TeamsError(f"Steps must be a list of 1 to {MAX_STEPS} strings.")
    actions: List[Action] = []
    for raw in steps:
        if not isinstance(raw, str) or not raw.strip():
            raise TeamsError(f"Invalid step {raw!r}.")
        verb, _, rest = raw.strip().partition(" ")
        verb, rest = verb.lower(), rest.strip()
        if verb == "open" and not rest:
            url = desktop_link(validate_url(meeting_url))
            actions += [Action("Win+R", {"op": "keys", "combo": "win+r"}),
                        Action("wait for the Run box", None, RUN_BOX_WAIT),
                        Action("type the meeting link and Enter",
                               {"op": "type", "text": url, "press_enter": True})]
        elif verb == "wait":
            try:
                seconds = float(rest)
            except ValueError:
                raise TeamsError(f"Invalid wait {raw!r}; use e.g. 'wait 10'.") from None
            if not 0 < seconds <= MAX_WAIT:
                raise TeamsError(f"Waits must be between 0 and {MAX_WAIT:g} seconds: {raw!r}.")
            actions.append(Action(f"wait {seconds:g} s", None, seconds))
        elif verb == "keys" and rest:
            request = {"op": "keys", "combo": rest}
            kb.prepare(request)
            actions.append(Action(rest, request))
        else:
            raise TeamsError(f"Unknown step {raw!r}; use 'open', 'wait N' or 'keys COMBO'.")
    return actions


def load_config(path: str = CONFIG_PATH) -> TeamsConfig:
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        data = {}
    except (OSError, ValueError) as exc:
        raise TeamsError(f"Can't read {path}: {exc}") from None
    if not isinstance(data, dict):
        raise TeamsError(f"{path} must contain a JSON object.")
    steps = data.get("share_steps")
    if not steps or steps == OLD_DEFAULT_SHARE_STEPS:
        steps = list(DEFAULT_SHARE_STEPS)
    return TeamsConfig(data.get("meeting_url") or "", steps)


def save_url(url: str, path: str = CONFIG_PATH) -> str:
    """Validate and store the meeting link, keeping any custom steps."""
    url = validate_url(url)
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        if not isinstance(data, dict):
            data = {}
    except (OSError, ValueError):
        data = {}
    data["meeting_url"] = url
    if not data.get("share_steps") or data["share_steps"] == OLD_DEFAULT_SHARE_STEPS:
        data["share_steps"] = list(DEFAULT_SHARE_STEPS)
    kb.ensure_socket_dir(os.path.dirname(path))
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2)
        handle.write("\n")
    os.replace(tmp, path)
    return url


def set_link(url: str, path: str = CONFIG_PATH) -> str:
    """Save a new meeting link (MCP set_teams_meeting_link); sends nothing to Windows."""
    url = save_url(url, path)
    return (f"Saved the Teams meeting link {url}. Nothing was sent to Windows; "
            "start_teams_screen_share will open this meeting.")


def share_actions(config: TeamsConfig) -> List[Action]:
    return parse_steps(config.share_steps, config.meeting_url)


def leave_actions() -> List[Action]:
    return parse_steps(LEAVE_STEPS, None)


def run_actions(actions: List[Action], execute: Callable[[dict], str],
                wait: Callable[[float], bool] = lambda s: (time.sleep(s), False)[1],
                progress: Callable[[int, int, str], None] = lambda i, n, label: None) -> List[str]:
    """Run actions in order. ``wait(seconds)`` returns True to stop. Raises TeamsError."""
    done: List[str] = []
    for index, action in enumerate(actions, 1):
        progress(index, len(actions), action.label)
        if action.request is None:
            if wait(action.seconds):
                raise TeamsError(f"Stopped at step {index}/{len(actions)}. Done: {', '.join(done) or 'nothing'}.")
        else:
            try:
                execute(action.request)
            except kb.BridgeError as exc:
                raise TeamsError(f"Step {index}/{len(actions)} ({action.label}) failed: {exc} "
                                 f"Done before it: {', '.join(done) or 'nothing'}.") from None
        done.append(action.label)
    return done


def stoppable_wait(event: threading.Event) -> Callable[[float], bool]:
    return lambda seconds: event.wait(seconds)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Teams screen-share settings (sends no keys).")
    parser.add_argument("--set-url", metavar="URL", help="save the Teams meeting link")
    parser.add_argument("--show", action="store_true", help="show the saved link and steps")
    args = parser.parse_args(argv)
    try:
        if args.set_url:
            print(f"Saved {save_url(args.set_url)} to {CONFIG_PATH}")
        config = load_config()
        print(f"Meeting link: {config.meeting_url or '(not set)'}")
        print(f"Share steps:  {json.dumps(config.share_steps)}")
        if config.meeting_url:
            print(f"Typed as:     {desktop_link(validate_url(config.meeting_url))}")
            print("Actions:      " + " → ".join(a.label for a in share_actions(config)))
    except TeamsError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
