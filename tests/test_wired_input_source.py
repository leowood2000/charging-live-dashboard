import unittest

from server import (Sampler, WIRED_VOTER_TOPICS, clear_vote_topics,
                    merge_vote_topics, parse_vote_blocks, resolve_wired_input_source)


class WiredInputSourceTests(unittest.TestCase):
    def test_vote_topic_survives_partial_log_window(self):
        previous = {
            "wireless_buck_input": {
                "topic": "wireless_buck_input", "rows": [{"client": "xm_wls", "enable": 1, "value": 1300}],
                "result": {"client": "soc_limit", "value": 750},
            },
            "div2_single": {
                "topic": "div2_single", "rows": [{"client": "jeita", "enable": 1, "value": 4000}],
            },
        }
        incoming = {
            "chg_enable": {
                "topic": "chg_enable", "rows": [{"client": "online", "enable": 1, "value": 1}],
                "result": {"client": "online", "value": 1},
            }
        }
        merged = merge_vote_topics(previous, incoming)
        self.assertIn("wireless_buck_input", merged)
        self.assertEqual(750, merged["wireless_buck_input"]["result"]["value"])
        self.assertIn("div2_single", merged)

    def test_vote_topics_clear_only_at_session_boundary(self):
        voters = {"wireless_buck_input": {}, "div2_single": {}, "chg_enable": {}}
        cleared = clear_vote_topics(voters, {"wireless_buck_input", "div2_single"})
        self.assertNotIn("wireless_buck_input", cleared)
        self.assertNotIn("div2_single", cleared)
        self.assertIn("chg_enable", cleared)

    def test_shared_buck_fcc_is_not_owned_by_wired_clear_set(self):
        # buck_charge_curr 同时承载无线热控与有线 Buck FCC，不能由单侧断开清空。
        self.assertNotIn("buck_charge_curr", WIRED_VOTER_TOPICS)

    def test_vote_header_keeps_absolute_time_for_session_freshness(self):
        text = "[10:00:01:123] mca_vote:1 buck_charge_curr VOTER:\n1.mca_thermal 1 4000"
        block = parse_vote_blocks(text, fname="mca_log_20260811_095900")["buck_charge_curr"]
        self.assertGreater(block["at"], 0)
        self.assertEqual("10:00:01:123", block["time"])

    def test_wireless_path_survives_missing_wireless_voter_topic(self):
        sampler = Sampler.__new__(Sampler)
        sampler.last_wls_icl = 100
        sampler.last_wls_icl_log_time = "10:00:00:000"
        sampler.last_wls_icl_at = 1
        sampler.last_wls_icl_ms = 1
        sampler.last_wls_chg_en = 1
        sampler.last_quick_cur_max = 3400
        sampler.last_buck_fcc = None
        sampler.last_cp_mode = 2
        sampler.last_cp_work_mode = 2
        sampler.last_wls_cp_evidence = True
        sampler.last_wls_work_mode_ms = 1
        sampler.last_wls_mode = "epp_qc"
        sampler.last_rx_iout_limit = 2800
        sampler.rx_iout_limit_captured = True
        sampler.last_rx_iout_limit_at = 1
        sampler.last_rx_iout_limit_log_time = "10:00:00:000"
        sampler.session_logs_stale = False
        sampler.last_smartendura_soc_limit = False
        sampler.last_cur_decision = {"final": 3400}
        core = {"voters": {}, "derived": {
            "input_source": "wireless", "cp_ibus_total_ma": 1755,
        }}
        sampler._decorate_wireless_path(core)
        self.assertEqual("cp", core["derived"]["wireless_path"]["state"])
        self.assertEqual(2, core["derived"]["wireless_path"]["ratio"])
        self.assertEqual(3400, core["derived"]["wireless_path"]["battery_limit_ma"])
        self.assertNotIn("wireless_buck_input", core["voters"])

    def test_stopped_idle_cp_uses_usb_system_input(self):
        self.assertEqual(
            "usb_uevent",
            resolve_wired_input_source("cp", 5, 238, True, False, 0),
        )

    def test_active_cp_keeps_cp_bus_primary(self):
        self.assertEqual(
            "cp_ibus_total",
            resolve_wired_input_source("cp", 1800, 238, True, False, 0),
        )

    def test_charging_cp_keeps_low_prestart_cp_reading(self):
        self.assertEqual(
            "cp_ibus_total",
            resolve_wired_input_source("cp", 5, 238, True, True, 1200),
        )


if __name__ == "__main__":
    unittest.main()
