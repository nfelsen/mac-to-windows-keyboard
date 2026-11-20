import importlib.util
import json
import os
import stat
import tempfile
import threading
import unittest

import kvm_bridge as kb
import kvm_teams as kt

URL = "https://teams.microsoft.com/l/meetup-join/19%3ameeting_abc%40thread.v2/0?context=%7b%7d"
SHORT = "https://teams.microsoft.com/meet/2512345678?p=abcDEF"


class ValidateUrlTest(unittest.TestCase):
    def test_accepts_teams_links(self):
        for url in (URL, SHORT, "msteams://teams.microsoft.com/l/meetup-join/x", "  " + SHORT + "\n"):
            self.assertEqual(kt.validate_url(url), url.strip())

    def test_rejects_other_links(self):
        for url in ("", None, "http://teams.microsoft.com/l/x", "https://evil.com/teams.microsoft.com/l/x",
                    "https://teams.microsoft.com.evil.com/l/x", "https://user@teams.microsoft.com/l/x",
                    "https://teams.microsoft.com:8443/l/x", "https://teams.microsoft.com/",
                    "msteams://evil.com/l/x", "file:///C:/Windows/System32/cmd.exe", "cmd /c calc",
                    "https://teams.microsoft.com/l/a b", "https://teams.microsoft.com/l/é",
                    "https://teams.microsoft.com/l/x\nnotepad", "https://teams.microsoft.com/l/" + "x" * 300):
            with self.assertRaises(kt.TeamsError, msg=url):
                kt.validate_url(url)

    def test_desktop_link_opens_teams_app(self):
        self.assertEqual(kt.desktop_link(URL), "msteams://" + URL[len("https://"):])
        self.assertEqual(kt.desktop_link(SHORT), "msteams://teams.microsoft.com/meet/2512345678?p=abcDEF")
        msteams = "msteams://teams.microsoft.com/meet/1?p=x"
        self.assertEqual(kt.desktop_link(msteams), msteams)

    def test_length_limit_applies_to_typed_link(self):
        base = "https://teams.microsoft.com/meet/"
        fits = base + "1" * (kt.MAX_URL - 2 - len(base))
        self.assertEqual(len(kt.desktop_link(kt.validate_url(fits))), kt.MAX_URL)
        with self.assertRaisesRegex(kt.TeamsError, "typed as 260"):
            kt.validate_url(fits + "1")


class StepsTest(unittest.TestCase):
    def test_default_share_sequence(self):
        actions = kt.share_actions(kt.TeamsConfig(URL, kt.DEFAULT_SHARE_STEPS))
        requests = [a.request for a in actions if a.request]
        self.assertEqual(requests, [
            {"op": "keys", "combo": "win+r"},
            {"op": "type", "text": kt.desktop_link(URL), "press_enter": True},
            {"op": "keys", "combo": "ctrl+shift+e"},
        ])
        self.assertEqual([a.seconds for a in actions if a.request is None], [kt.RUN_BOX_WAIT, 30])
        for request in requests:
            kb.prepare(request)  # every request passes the bridge's own validation

    def test_leave_is_hang_up_only(self):
        self.assertEqual([a.request for a in kt.leave_actions()], [{"op": "keys", "combo": "ctrl+shift+h"}])

    def test_custom_steps(self):
        actions = kt.parse_steps(["open", "wait 0.5", "keys ctrl+shift+m ctrl+shift+o"], SHORT)
        self.assertEqual(actions[-1].request, {"op": "keys", "combo": "ctrl+shift+m ctrl+shift+o"})
        self.assertEqual(actions[-2].seconds, 0.5)

    def test_rejects_bad_steps(self):
        for steps in ([], "open", ["open"] * (kt.MAX_STEPS + 1), ["wait"], ["wait 0"], ["wait 61"],
                      ["wait soon"], ["keys"], ["keys ctrl+nope"], ["type hello"], ["open now"], [3]):
            with self.assertRaises(kb.BridgeError, msg=steps):
                kt.parse_steps(steps, SHORT)

    def test_open_needs_saved_link(self):
        with self.assertRaisesRegex(kt.TeamsError, "No Teams meeting link"):
            kt.share_actions(kt.TeamsConfig("", kt.DEFAULT_SHARE_STEPS))


class RunActionsTest(unittest.TestCase):
    def setUp(self):
        self.actions = kt.share_actions(kt.TeamsConfig(SHORT, kt.DEFAULT_SHARE_STEPS))
        self.sent, self.waits = [], []

    def execute(self, request):
        self.sent.append(request)
        return "ok"

    def wait(self, seconds):
        self.waits.append(seconds)
        return False

    def test_runs_in_order(self):
        done = kt.run_actions(self.actions, self.execute, self.wait)
        self.assertEqual(len(done), len(self.actions))
        self.assertEqual(self.sent[0], {"op": "keys", "combo": "win+r"})
        self.assertEqual(self.sent[-1], {"op": "keys", "combo": "ctrl+shift+e"})
        self.assertEqual(self.waits, [kt.RUN_BOX_WAIT, 30])

    def test_stops_at_first_error(self):
        def execute(request):
            self.sent.append(request)
            raise kb.BridgeError("Caps Lock is on.")
        with self.assertRaisesRegex(kt.TeamsError, r"Step 1/.*Caps Lock is on.*nothing"):
            kt.run_actions(self.actions, execute, self.wait)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.waits, [])

    def test_cancel_during_wait_sends_nothing_more(self):
        stop = threading.Event()
        stop.set()
        with self.assertRaisesRegex(kt.TeamsError, "Stopped at step 2"):
            kt.run_actions(self.actions, self.execute, kt.stoppable_wait(stop))
        self.assertEqual(self.sent, [{"op": "keys", "combo": "win+r"}])


class ConfigTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "app", "teams.json")

    def tearDown(self):
        self.dir.cleanup()

    def test_missing_file_has_defaults(self):
        config = kt.load_config(self.path)
        self.assertEqual(config, kt.TeamsConfig("", kt.DEFAULT_SHARE_STEPS))

    def test_save_round_trip_keeps_custom_steps(self):
        kt.save_url(SHORT, self.path)
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
        with open(self.path) as handle:
            data = json.load(handle)
        data["share_steps"] = ["open", "wait 20", "keys ctrl+shift+e"]
        with open(self.path, "w") as handle:
            json.dump(data, handle)
        kt.save_url(URL, self.path)
        self.assertEqual(kt.load_config(self.path), kt.TeamsConfig(URL, ["open", "wait 20", "keys ctrl+shift+e"]))

    def test_old_default_steps_are_upgraded(self):
        # The old default pressed Enter blindly, which could pick "Transfer" in Teams' join dialog.
        os.makedirs(os.path.dirname(self.path))
        with open(self.path, "w") as handle:
            json.dump({"meeting_url": SHORT, "share_steps": kt.OLD_DEFAULT_SHARE_STEPS}, handle)
        self.assertEqual(kt.load_config(self.path).share_steps, kt.DEFAULT_SHARE_STEPS)
        kt.save_url(URL, self.path)
        with open(self.path) as handle:
            self.assertEqual(json.load(handle)["share_steps"], kt.DEFAULT_SHARE_STEPS)

    def test_set_link_message(self):
        message = kt.set_link(" " + SHORT + " ", self.path)
        self.assertIn(SHORT, message)
        self.assertIn("Nothing was sent", message)
        self.assertEqual(kt.load_config(self.path).meeting_url, SHORT)
        with self.assertRaises(kt.TeamsError):
            kt.set_link("https://teams.microsoft.com/meet/" + "1" * 300, self.path)
        self.assertEqual(kt.load_config(self.path).meeting_url, SHORT)

    def test_invalid_link_is_not_saved(self):
        with self.assertRaises(kt.TeamsError):
            kt.save_url("https://example.com/meeting", self.path)
        self.assertFalse(os.path.exists(self.path))

    def test_bad_json(self):
        os.makedirs(os.path.dirname(self.path))
        with open(self.path, "w") as handle:
            handle.write("[1]")
        with self.assertRaises(kt.TeamsError):
            kt.load_config(self.path)


@unittest.skipUnless(importlib.util.find_spec("mcp"), "needs the mcp package (uv run)")
class McpToolsTest(unittest.TestCase):
    def test_only_set_link_takes_a_url(self):
        import asyncio
        import kvm_mcp
        tools = {t.name: t for t in asyncio.run(kvm_mcp.mcp.list_tools())}
        self.assertEqual(set(tools["set_teams_meeting_link"].input_schema["properties"]), {"url"})
        self.assertEqual(tools["start_teams_screen_share"].input_schema.get("properties", {}), {})
        self.assertTrue(tools["start_teams_screen_share"].annotations.destructive_hint)


if __name__ == "__main__":
    unittest.main()
