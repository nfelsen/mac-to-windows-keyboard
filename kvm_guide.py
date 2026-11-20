"""Steps of the in-app "Windows Screen Setup" guide (no Cocoa here, so it is testable).

Sharing Windows to Teams and watching it with the hidden viewer still needs a few
clicks by the user. The guide lists every step with a status, derived from what the
app can see: the saved link, whether Teams runs on the Mac, the share sequence it
sent over the cable, and the viewer's state. The first step that waits for the user
is the one the window highlights.
"""

from typing import List, NamedTuple, Optional, Tuple

import kvm_viewer as kv

DONE, CURRENT, BUSY, TODO, ERROR = "done", "current", "busy", "todo", "error"
# Share sequence states the app tracks for step 3.
SHARE_IDLE, SHARE_SENDING, SHARE_SENT, SHARE_FAILED = "idle", "sending", "sent", "failed"
VIEWER_STARTING = ("loading", "launcher", "prejoin", "joining")

V = f"\u201c{kv.VIEWER_NAME}\u201d"


class Step(NamedTuple):
    title: str
    detail: str
    status: str
    button: Optional[Tuple[str, str]] = None  # (title, Delegate selector)
    enabled: bool = True
    hint: Optional[str] = None  # short "Next: …" text for the main window; defaults to the title


def short_link(url: str, limit: int = 60) -> str:
    return url if len(url) <= limit else url[:limit - 1] + "\u2026"


def steps(link: str, teams_running: bool, cable: bool, share: str, viewer: str) -> List[Step]:
    """The six guide steps. ``viewer`` is a kvm_viewer state, ``share`` a SHARE_* value."""
    live = viewer == "live"
    in_meeting = viewer in ("in-meeting", "live")
    # Admitting the viewer can't wait, so it outranks the Windows clicks while in the lobby.
    share_status = TODO if viewer == "lobby" else CURRENT
    result = []

    if link:
        result.append(Step("Meeting link saved", short_link(link), DONE, ("Change\u2026", "setTeamsLink:")))
    else:
        result.append(Step("Save the meeting link",
                           "Paste the join link of a Teams meeting you own (for example a one-time "
                           "\u201cMac2Win\u201d meeting).", CURRENT, ("Set Link\u2026", "setTeamsLink:")))

    if teams_running or in_meeting:
        result.append(Step("Join the meeting in Teams on the Mac",
                           "Teams is open on this Mac. Make sure you're in the meeting: you admit the "
                           "viewer from there.", DONE, ("Open in Teams", "openTeamsOnMac:"), bool(link)))
    else:
        result.append(Step("Join the meeting in Teams on the Mac",
                           "Open the meeting in Teams and join it. You'll admit the viewer from there.",
                           CURRENT, ("Open in Teams", "openTeamsOnMac:"), bool(link)))

    share_button = ("Share to Teams", "shareTeams:")
    if live:
        result.append(Step("Share the Windows screen", "Windows is sharing its screen.", DONE))
    elif share == SHARE_SENDING:
        result.append(Step("Share the Windows screen",
                           "Typing the meeting link on Windows and starting the share\u2026", BUSY))
    elif share == SHARE_SENT:
        result.append(Step("Share the Windows screen",
                           "On Windows, click \u201cAdd this device\u201d (or \u201cJoin now\u201d), never "
                           "\u201cTransfer\u201d. Then click \u201cShare\u201d in Teams' share picker.",
                           share_status, ("Send Again", "shareTeams:"), bool(link and cable),
                           "on Windows, click \u201cAdd this device\u201d, then \u201cShare\u201d"))
    else:
        detail = ("Windows opens the meeting in its Teams app and starts sharing. You then click "
                  "twice on Windows.")
        if not cable:
            detail += " Connect the cable first."
        status = ERROR if share == SHARE_FAILED else share_status
        if share == SHARE_FAILED:
            detail = "The share sequence didn't finish (see the status line). " + detail
        result.append(Step("Share the Windows screen", detail, status, share_button, bool(link and cable)))

    if in_meeting:
        result.append(Step(f"Admit {V}", "The viewer is in the meeting.", DONE))
    elif viewer == "lobby":
        result.append(Step(f"Admit {V}",
                           f"In Teams on the Mac, admit {V} from the lobby (shown as Unverified).", CURRENT,
                           hint=f"admit {V} in Teams on the Mac"))
    elif viewer in VIEWER_STARTING:
        result.append(Step(f"Admit {V}", kv.describe_state(viewer)[0], BUSY))
    elif viewer == "ended":
        result.append(Step(f"Admit {V}", kv.describe_state(viewer)[0], ERROR,
                           ("Restart Viewer", "startViewer:"), bool(link)))
    else:
        result.append(Step(f"Start the viewer and admit {V}",
                           "Mac2Win KVM joins as a hidden guest (no mic, camera, or audio). Admit it once "
                           "in Teams on the Mac.", CURRENT, ("Start Viewer", "startViewer:"), bool(link)))

    if live:
        result.append(Step("Receiving the Windows screen", "Live.", DONE,
                           ("Open Windows Screen", "watchWindows:")))
    elif in_meeting:
        waiting = ("Waiting for Windows to share. Click \u201cShare\u201d on Windows if you haven't."
                   if share == SHARE_SENT else "Waiting for Windows to share its screen\u2026")
        result.append(Step("Receiving the Windows screen", waiting, BUSY))
    else:
        result.append(Step("Receiving the Windows screen", "The Windows screen shows up here.", TODO))

    result.append(Step("Done", "You can minimize Teams on the Mac (or leave the call there; the viewer "
                       "stays in). Assistants can now call windows_screenshot.", DONE if live else TODO))
    return highlight_first(result)


def hint(items: List[Step], started: bool) -> str:
    """One line for the main window next to the Windows Screen button."""
    if items and items[-1].status == DONE:
        return "Windows screen is live."
    if not started:
        return "\u2190 view Windows here via Teams (step-by-step guide)"
    for status, prefix in ((ERROR, "Problem: "), (CURRENT, "Next: "), (BUSY, "")):
        for step in items:
            if step.status == status:
                text = step.detail if status == BUSY else (step.hint or step.title[0].lower() + step.title[1:])
                return prefix + text
    return ""


def highlight_first(items: List[Step]) -> List[Step]:
    """Keep only the first CURRENT step highlighted; later ones become TODO."""
    seen = False
    out = []
    for step in items:
        if step.status == CURRENT:
            if seen:
                step = step._replace(status=TODO)
            seen = True
        out.append(step)
    return out
