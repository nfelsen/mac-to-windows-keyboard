"""Logic for the built-in Windows screen viewer (no Cocoa here, so it is testable).

The viewer is a hidden web view inside Mac2Win KVM that joins the saved Teams
meeting through Teams on the web as a receive-only guest ("Mac2Win viewer": no
mic, no camera, no audio). Once someone admits it from the lobby, it receives
the Windows screen share as a <video> element, and FRAME_SCRIPT copies the
current frame into a JPEG. The visible Teams app is not needed on the Mac.
"""

import base64
import json
import os
from typing import NamedTuple
from urllib.parse import urlsplit

import kvm_bridge as kb
import kvm_teams as kt

VIEWER_NAME = "Mac2Win viewer"
# Teams on the web only offers its browser join flow to browsers it recognises.
USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
              "(KHTML, like Gecko) Version/18.0 Safari/605.1.15")
FRAME_PATH = os.path.join(kb.SOCKET_DIR, "windows-screen.jpg")
MAX_FRAME_BYTES = 20 * 1024 * 1024

ADMIT_HINT = (f"In Teams, admit \u201c{VIEWER_NAME}\u201d from the lobby (Teams marks it Unverified "
              "because it joins as a guest).")
STATES = {
    "loading": ("Opening the Teams meeting…", "info"),
    "launcher": ("Opening the Teams meeting…", "info"),
    "prejoin": ("Joining the Teams meeting without mic or camera…", "info"),
    "joining": ("Joining the Teams meeting…", "info"),
    "lobby": (f"Waiting in the lobby. {ADMIT_HINT}", "warn"),
    "in-meeting": ("In the meeting; waiting for Windows to share its screen.", "info"),
    "live": ("Live", "ok"),
    "ended": ("The viewer is no longer in the meeting (denied, removed, or the meeting ended).", "error"),
    "stopped": ("Viewer stopped.", "idle"),
}


class ViewerError(kb.BridgeError):
    pass


class Frame(NamedTuple):
    data: bytes  # JPEG
    width: int
    height: int
    shared: bool  # True if Teams labels the video as shared content


def viewer_url(meeting_url) -> str:
    """The https:// form of the saved meeting link, for Teams on the web."""
    url = kt.validate_url(meeting_url)
    parts = urlsplit(url)
    if parts.scheme.lower() == "msteams":
        rest = url.split(":", 1)[1].lstrip("/")
        if not rest.lower().startswith(kt.TEAMS_HOST):
            rest = f"{kt.TEAMS_HOST}/{rest}"
        url = "https://" + rest
    return url


def describe_state(state):
    return STATES.get(state, (f"Viewer: {state}", "info"))


# Advances Teams' guest join flow by one step per call and returns the state.
# Only ever clicks the buttons named here; never turns on mic, camera, or audio.
JOIN_SCRIPT = r"""
(function(name){
  const norm = s => (s || '').replace(/[\u2018\u2019]/g, "'").replace(/\s+/g, ' ').trim().toLowerCase();
  const body = norm(document.body && document.body.innerText);
  const enabled = e => !e.disabled && e.getAttribute('aria-disabled') !== 'true';
  const find = text => [...document.querySelectorAll('button,[role=button],[role=radio],[role=menuitemradio],label,input[type=radio]')]
    .find(e => enabled(e) && (norm(e.innerText) === text || norm(e.getAttribute('aria-label')) === text));
  if (/you've been removed|you have been removed|denied access|denied your request|meeting has ended|you left the meeting|call ended/.test(body))
    return 'ended';
  if (find('leave')) return 'in-meeting';
  if (/let you in shortly|let you in soon|waiting for someone to let you in/.test(body)) return 'lobby';
  let b = find('continue on this browser');
  if (b) { b.click(); return 'launcher'; }
  b = find('continue without audio or video');
  if (b) { b.click(); return 'prejoin'; }
  const input = document.querySelector('input[type=text], input:not([type])');
  if (input && !input.value) {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(input, name);
    input.dispatchEvent(new Event('input', {bubbles: true}));
    return 'prejoin';
  }
  b = find("don't use audio");
  if (b && !window.__m2wNoAudio) {
    if (!b.checked && b.getAttribute('aria-checked') !== 'true') b.click();
    window.__m2wNoAudio = true;
    return 'prejoin';
  }
  b = find('join now');
  if (b && window.__m2wNoAudio) { b.click(); return 'joining'; }
  return input ? 'prejoin' : 'loading';
})
"""


# Injected before Teams' own scripts: refuse every mic/camera request at once, so
# Teams never waits on a permission prompt and the viewer can never send media.
# Inside the app (launched by macOS, no camera/mic usage declared) WebKit has no
# navigator.mediaDevices at all, and Teams' meeting page then fails with "device
# enumeration unsupported" and stays blank, so install an empty one.
NO_MEDIA_SCRIPT = r"""
(function(){
  let md = navigator.mediaDevices;
  if (!md) {
    md = new EventTarget();
    md.ondevicechange = null;
    md.getSupportedConstraints = () => ({});
    md.__m2wStub = true;
    Object.defineProperty(Navigator.prototype, 'mediaDevices', {get: () => md, configurable: true});
  }
  const deny = () => Promise.reject(new DOMException('Mac2Win viewer is receive-only', 'NotAllowedError'));
  md.getUserMedia = deny;
  md.getDisplayMedia = deny;
  md.enumerateDevices = () => Promise.resolve([]);
})();
"""


def join_script(name=VIEWER_NAME) -> str:
    return f"{JOIN_SCRIPT.strip()}({json.dumps(name)})"


# Injected at document start: remembers the last few script errors and failed
# resource loads, so the app log can say why a Teams page stayed blank.
ERROR_LOG_SCRIPT = r"""
(function(){
  const errs = window.__m2wErrors = [];
  const add = m => { errs.push(String(m).slice(0, 160)); if (errs.length > 6) errs.shift(); };
  window.addEventListener('error', e => {
    const t = e.target;
    if (t && t !== window && (t.src || t.href)) add('load failed: ' + (t.src || t.href).split('?')[0]);
    else add((e.message || 'error') + ' @' + String(e.filename || '').split('?')[0].split('/').pop() + ':' + e.lineno);
  }, true);
  window.addEventListener('unhandledrejection', e => add('rejection: ' + (e.reason && (e.reason.message || e.reason))));
})();
"""

# A one-line snapshot of the page for the app log: its shape and button labels, no page text.
PAGE_INFO_SCRIPT = r"""
JSON.stringify({url: location.href.split('?')[0], title: document.title, ready: document.readyState,
  text: (document.body && document.body.innerText || '').length,
  visibility: document.visibilityState, media: navigator.mediaDevices && navigator.mediaDevices.__m2wStub ? 'stub' : 'webkit', rtc: typeof RTCPeerConnection, videos: document.querySelectorAll('video').length,
  buttons: [...document.querySelectorAll('button,[role=button]')].map(b => (b.innerText || b.getAttribute('aria-label') || '').trim().slice(0, 40)).filter(Boolean).slice(0, 12),
  scripts: document.scripts.length, resources: performance.getEntriesByType('resource').length,
  errors: window.__m2wErrors || []})
"""

STALL_STATES = ("loading", "launcher")
STALL_SECONDS = 30
MAX_RELOADS = 3


class StallWatch:
    """Decides when a viewer stuck on "Opening the Teams meeting…" should reload the page."""

    def __init__(self, limit=STALL_SECONDS, max_reloads=MAX_RELOADS):
        self.limit, self.max_reloads = limit, max_reloads
        self.since = None
        self.reloads = 0

    def update(self, state, now) -> bool:
        """Record ``state`` at time ``now``; True means reload the page now."""
        if state not in STALL_STATES:
            self.since = None
            return False
        if self.since is None:
            self.since = now
            return False
        if now - self.since < self.limit or self.reloads >= self.max_reloads:
            return False
        self.reloads += 1
        self.since = now
        return True


# Copies the current frame of the screen-share video into a JPEG data URL.
# Prefers videos Teams labels as shared content; otherwise the largest video.
FRAME_SCRIPT = r"""
(function(){
  const vids = [...document.querySelectorAll('video')].filter(v => v.videoWidth > 0 && v.readyState >= 2);
  if (!vids.length) return JSON.stringify({error: 'no-video'});
  const shared = v => {
    for (let e = v, i = 0; e && i < 15; e = e.parentElement, i++) {
      const a = [e.getAttribute && e.getAttribute('aria-label'), e.getAttribute && e.getAttribute('data-tid'),
                 e.getAttribute && e.getAttribute('data-stream-type')].join(' ');
      if (/screen ?shar|content shared|shared content|desktop/i.test(a)) return 1;
    }
    return 0;
  };
  vids.sort((a, b) => shared(b) - shared(a) || b.videoWidth * b.videoHeight - a.videoWidth * a.videoHeight);
  const v = vids[0];
  const c = document.createElement('canvas');
  c.width = v.videoWidth; c.height = v.videoHeight;
  c.getContext('2d').drawImage(v, 0, 0);
  return JSON.stringify({width: c.width, height: c.height, shared: shared(v) === 1,
                         data: c.toDataURL('image/jpeg', 0.85)});
})()
"""


def parse_frame(result) -> Frame:
    """Turn FRAME_SCRIPT's JSON result into a Frame, or raise ViewerError."""
    try:
        info = json.loads(result) if isinstance(result, str) else None
    except ValueError:
        info = None
    if not isinstance(info, dict):
        raise ViewerError("The Teams viewer returned no frame.")
    if info.get("error") == "no-video":
        raise ViewerError("The viewer is in the meeting but receives no video yet. Is Windows sharing "
                          "its screen? (start_teams_screen_share, then click Share on Windows.)")
    data_url = info.get("data")
    prefix = "data:image/jpeg;base64,"
    if not isinstance(data_url, str) or not data_url.startswith(prefix):
        raise ViewerError("The Teams viewer returned an unreadable frame.")
    if len(data_url) > MAX_FRAME_BYTES * 4 // 3 + len(prefix) + 4:
        raise ViewerError("The Windows screen frame is too large.")
    try:
        data = base64.b64decode(data_url[len(prefix):], validate=True)
        width, height = int(info["width"]), int(info["height"])
    except (ValueError, KeyError, TypeError):
        raise ViewerError("The Teams viewer returned an unreadable frame.") from None
    if not data.startswith(b"\xff\xd8") or width <= 0 or height <= 0:
        raise ViewerError("The Teams viewer returned an unreadable frame.")
    return Frame(data, width, height, bool(info.get("shared")))


def save_frame(frame: Frame, path=FRAME_PATH) -> str:
    """Write the frame privately (0600) and atomically; returns the path."""
    kb.ensure_socket_dir(os.path.dirname(path))
    tmp = f"{path}.{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(frame.data)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
    return path


def screenshot_message(frame: Frame, path: str) -> str:
    return json.dumps({"path": path, "width": frame.width, "height": frame.height, "shared": frame.shared})


def read_screenshot(message):
    """Parse the app's reply to a screenshot request into (jpeg bytes, info dict)."""
    try:
        info = json.loads(message)
        path = info["path"]
    except (ValueError, KeyError, TypeError):
        raise ViewerError("kvm_app.py sent an unreadable screenshot reply.") from None
    if os.path.realpath(os.path.dirname(path)) != os.path.realpath(kb.SOCKET_DIR):
        raise ViewerError("kvm_app.py sent a screenshot outside its data folder.")
    with open(path, "rb") as handle:
        return handle.read(MAX_FRAME_BYTES + 1), info
