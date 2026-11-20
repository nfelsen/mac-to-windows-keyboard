import base64
import importlib.util
import json
import os
import shutil
import stat
import tempfile
import unittest
from unittest.mock import patch

import kvm_bridge as kb
import kvm_viewer as kv

SHORT = "https://teams.microsoft.com/meet/2512345678?p=abcDEF"
JPEG = b"\xff\xd8\xff\xe0fake-jpeg"


def frame_result(data=JPEG, **extra):
    info = {"width": 1728, "height": 1080, "shared": True,
            "data": "data:image/jpeg;base64," + base64.b64encode(data).decode()}
    info.update(extra)
    return json.dumps(info)


class ViewerUrlTest(unittest.TestCase):
    def test_https_link_is_used_as_is(self):
        self.assertEqual(kv.viewer_url(SHORT), SHORT)

    def test_msteams_link_opens_teams_on_the_web(self):
        self.assertEqual(kv.viewer_url("msteams://teams.microsoft.com/meet/1?p=x"),
                         "https://teams.microsoft.com/meet/1?p=x")
        self.assertEqual(kv.viewer_url("msteams:/l/meetup-join/abc"),
                         "https://teams.microsoft.com/l/meetup-join/abc")

    def test_only_teams_links(self):
        for bad in ("", None, "https://evil.example/meet/1", "file:///etc/passwd", "javascript:alert(1)"):
            with self.assertRaises(kb.BridgeError):
                kv.viewer_url(bad)


class ScriptsTest(unittest.TestCase):
    def test_name_is_quoted_as_a_js_string(self):
        script = kv.join_script("a'); evil('")
        self.assertTrue(script.endswith('("a\'); evil(\'")'))

    def test_join_script_never_enables_mic_camera_or_audio(self):
        clicks = {"'continue on this browser'", "'continue without audio or video'",
                  "\"don't use audio\"", "'join now'"}
        found = {line.split("find(", 1)[1].split(")", 1)[0]
                 for line in kv.JOIN_SCRIPT.splitlines() if "= find(" in line}
        self.assertEqual(found, clicks)

    def test_every_script_state_has_a_message(self):
        for state in ("loading", "launcher", "prejoin", "joining", "lobby", "in-meeting", "ended"):
            self.assertIn(f"'{state}'", kv.JOIN_SCRIPT)
            self.assertIn(state, kv.STATES)
        self.assertIn(kv.VIEWER_NAME, kv.describe_state("lobby")[0])


class ParseFrameTest(unittest.TestCase):
    def test_good_frame(self):
        frame = kv.parse_frame(frame_result())
        self.assertEqual(frame, kv.Frame(JPEG, 1728, 1080, True))

    def test_no_video_explains_sharing(self):
        with self.assertRaisesRegex(kv.ViewerError, "sharing"):
            kv.parse_frame(json.dumps({"error": "no-video"}))

    def test_rejects_garbage(self):
        for result in (None, "", "not json", "[]", json.dumps({"data": "data:image/png;base64,AAAA"}),
                       frame_result(data=b"GIF89a"), frame_result(width=0),
                       json.dumps({"width": 1, "height": 1, "data": "data:image/jpeg;base64,!!"})):
            with self.subTest(result=result), self.assertRaises(kv.ViewerError):
                kv.parse_frame(result)


class SaveFrameTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(dir="/tmp")
        self.addCleanup(shutil.rmtree, self.dir)
        patcher = patch.object(kb, "SOCKET_DIR", self.dir)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_round_trip_is_private(self):
        frame = kv.Frame(JPEG, 10, 20, False)
        path = kv.save_frame(frame, os.path.join(self.dir, "windows-screen.jpg"))
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        data, info = kv.read_screenshot(kv.screenshot_message(frame, path))
        self.assertEqual(data, JPEG)
        self.assertEqual((info["width"], info["height"], info["shared"]), (10, 20, False))
        self.assertEqual(os.listdir(self.dir), ["windows-screen.jpg"])

    def test_refuses_paths_outside_the_data_folder(self):
        with self.assertRaises(kv.ViewerError):
            kv.read_screenshot(json.dumps({"path": "/etc/hosts"}))
        with self.assertRaises(kv.ViewerError):
            kv.read_screenshot("nonsense")


@unittest.skipUnless(importlib.util.find_spec("mcp"), "needs the mcp package (uv run)")
class ScreenshotToolTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(dir="/tmp")
        self.addCleanup(shutil.rmtree, self.dir)
        for name, value in (("SOCKET_DIR", self.dir), ("SOCKET_PATH", os.path.join(self.dir, "kvm.sock"))):
            patcher = patch.object(kb, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_returns_the_app_frame_as_jpeg(self):
        import kvm_mcp
        frame = kv.Frame(JPEG, 4, 3, True)
        requests = []

        def handler(request):
            requests.append(request)
            return kv.screenshot_message(frame, kv.save_frame(frame, os.path.join(self.dir, "shot.jpg")))
        server = kb.BridgeServer(handler, kb.SOCKET_PATH)
        self.assertTrue(server.bind())
        server.start()
        self.addCleanup(server.close)
        image = kvm_mcp.windows_screenshot()
        self.assertEqual(requests, [{"op": "screenshot"}])
        self.assertEqual(image.data, JPEG)
        self.assertEqual(image._mime_type, "image/jpeg")

    def test_app_errors_become_tool_errors(self):
        import kvm_mcp
        from mcp.server.mcpserver.exceptions import ToolError
        with self.assertRaisesRegex(ToolError, "not running"):
            kvm_mcp.windows_screenshot()

        def handler(request):
            raise kv.ViewerError(kv.describe_state("lobby")[0])
        server = kb.BridgeServer(handler, kb.SOCKET_PATH)
        self.assertTrue(server.bind())
        server.start()
        self.addCleanup(server.close)
        with self.assertRaisesRegex(ToolError, "admit"):
            kvm_mcp.windows_screenshot()


if __name__ == "__main__":
    unittest.main()
