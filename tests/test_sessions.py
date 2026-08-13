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
        self.assertIn("2000→2200→2400mA · 3次", session["events"][1]["detail"])

    def test_wired_cp_and_parallel_buck_events_are_kept(self):
        text = """[13:38:13:484-E] usb online: 1
[13:38:13:800-E] real_type changed: 0 => 10
[13:38:14:238-E] set chg current 3000
[13:38:15:517-I] sc8581_set_operation_mode:906 [0] set operation mode 1 reg 1 work_mode 1
[13:38:15:888-I] mca_quick_charge_update_work_mode_para:675 delta_volt: 600, single_curr: 20500, max_curr: 22000, work_mode: 4
[13:38:16:862-E] mca_quick_charge_select_max_ibat:1597 cur_stage 0 cur_max 20500 delta_cur 100 cur_work_cp
[13:38:21:919-I] strategy_quickchg_map_ibus_to_fsw:3556 ibus_avg: 1407, ratio: 4, cp_iout: 5628
[13:38:24:914-E] strategy_buckchg_charge_limit:307 set chg current 5000
[13:38:24:914-E] strategy_quickchg_enable_buck_charging:1810 enable buck parallel charging! ibus :2564
[13:38:26:716-E] strategy_buckchg_charge_limit:307 set chg current 3000
[13:38:26:717-E] strategy_quickchg_enable_buck_charging:1819 disable buck parallel charging!, ibus: 3196
[13:39:40:848-E] usb online: 0
"""
        session = parse_sessions(text)[0]
        labels = [e["label"] for e in session["events"]]
        self.assertEqual(session["source"], "wired")
        self.assertIn("CP 充电路径运行", labels)
        self.assertIn("CP 分压比", labels)
        self.assertIn("Buck 并行充电启用", labels)
        self.assertIn("Buck 并行充电关闭", labels)
        current = next(e for e in session["events"] if e["kind"] == "ichg")
        self.assertIn("3000→5000→3000mA · 3次", current["detail"])


if __name__ == "__main__":
    unittest.main()
