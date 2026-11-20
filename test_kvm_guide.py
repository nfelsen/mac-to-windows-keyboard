import unittest

import kvm_guide as kg

LINK = "https://teams.microsoft.com/meet/123?p=abc"


def statuses(**kwargs):
    args = dict(link=LINK, teams_running=True, cable=True, share=kg.SHARE_IDLE, viewer="stopped")
    args.update(kwargs)
    return [step.status for step in kg.steps(**args)]


class GuideTests(unittest.TestCase):
    def test_hint_before_starting_explains_the_button(self):
        self.assertIn("step-by-step guide", kg.hint(kg.steps(LINK, False, True, kg.SHARE_IDLE, "stopped"), False))

    def test_hint_names_the_windows_clicks_after_sharing(self):
        hint = kg.hint(kg.steps(LINK, True, True, kg.SHARE_SENT, "stopped"), True)
        self.assertTrue(hint.startswith("Next: on Windows, click"), hint)
        self.assertIn("Share", hint)

    def test_hint_asks_to_admit_in_lobby(self):
        self.assertEqual(kg.hint(kg.steps(LINK, True, True, kg.SHARE_SENT, "lobby"), True),
                         f"Next: admit {kg.V} in Teams on the Mac")

    def test_hint_when_live(self):
        self.assertEqual(kg.hint(kg.steps(LINK, True, True, kg.SHARE_IDLE, "live"), True),
                         "Windows screen is live.")

    def test_no_link_highlights_first_step_only(self):
        result = kg.steps("", False, True, kg.SHARE_IDLE, "stopped")
        self.assertEqual([s.status for s in result][:2], [kg.CURRENT, kg.TODO])
        self.assertEqual(sum(s.status == kg.CURRENT for s in result), 1)
        self.assertFalse(result[2].enabled)  # can't share without a link

    def test_teams_not_running_is_next(self):
        self.assertEqual(statuses(teams_running=False)[:3], [kg.DONE, kg.CURRENT, kg.TODO])

    def test_share_sent_asks_for_the_two_windows_clicks(self):
        step = kg.steps(LINK, True, True, kg.SHARE_SENT, "stopped")[2]
        self.assertEqual(step.status, kg.CURRENT)
        self.assertIn("Add this device", step.detail)
        self.assertIn("Share", step.detail)

    def test_sending_is_busy_and_viewer_can_start_meanwhile(self):
        self.assertEqual(statuses(share=kg.SHARE_SENDING)[2:4], [kg.BUSY, kg.CURRENT])

    def test_share_needs_cable(self):
        step = kg.steps(LINK, True, False, kg.SHARE_IDLE, "stopped")[2]
        self.assertFalse(step.enabled)
        self.assertIn("cable", step.detail)

    def test_lobby_asks_to_admit(self):
        step = kg.steps(LINK, True, True, kg.SHARE_SENT, "lobby")[3]
        self.assertIn("admit", step.detail)
        # Admitting outranks the Windows clicks while the viewer waits.
        self.assertEqual(statuses(share=kg.SHARE_SENT, viewer="lobby")[2:4], [kg.TODO, kg.CURRENT])
        self.assertEqual(statuses(viewer="lobby")[2:4], [kg.TODO, kg.CURRENT])

    def test_joining_is_busy(self):
        self.assertEqual(statuses(viewer="joining")[3], kg.BUSY)

    def test_ended_offers_restart(self):
        step = kg.steps(LINK, True, True, kg.SHARE_SENT, "ended")[3]
        self.assertEqual((step.status, step.button[1]), (kg.ERROR, "startViewer:"))

    def test_in_meeting_waits_for_share(self):
        self.assertEqual(statuses(share=kg.SHARE_SENT, viewer="in-meeting")[2:6],
                         [kg.CURRENT, kg.DONE, kg.BUSY, kg.TODO])

    def test_live_completes_everything(self):
        # Live video proves Windows shares, even if this app didn't send the sequence.
        self.assertEqual(statuses(teams_running=False, viewer="live"), [kg.DONE] * 6)

    def test_short_link(self):
        self.assertEqual(kg.short_link("x" * 10, 8), "xxxxxxx\u2026")
        self.assertEqual(kg.short_link(LINK), LINK)


if __name__ == "__main__":
    unittest.main()
