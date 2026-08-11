import unittest

from server import parse_sessions


class SessionBoundaryTests(unittest.TestCase):
    def test_latest_wired_session_replaces_ended_wireless_session(self):
        text = """[13:36:29:763-E] wireless power_good_on
[13:36:29:842-E] set chg current 4000
[13:36:54:392-I] wireless power_good_off
[13:47:39:284-E] usb online: 1
[13:47:42:213-E] set chg current 2100
[13:47:44:213-E] set chg current 2300
[13:47:46:213-E] set chg current 2400
[13:48:56:015-E] usb online: 0
[13:48:56:020-E] set chg current 100
"""
        sessions = parse_sessions(text)
        self.assertEqual(len(sessions), 1)
        session = sessions[0]
        self.assertEqual(session["source"], "wired")
        self.assertEqual(session["start"], "13:47:39:284")
        self.assertTrue(session["ended"])
        self.assertEqual(session["final_limit_ma"], 2400)
        self.assertEqual(len(session["events"]), 3)

    def test_consecutive_current_updates_are_coalesced(self):
        text = """[13:50:00:000-E] usb online: 1
[13:50:01:000-E] set chg current 2000
[13:50:03:000-E] set chg current 2200
[13:50:05:000-E] set chg current 2400
"""
        session = parse_sessions(text)[0]
        self.assertEqual(len(session["events"]), 2)
        self.assertIn("2000→2400mA · 3次", session["events"][1]["detail"])


if __name__ == "__main__":
    unittest.main()
