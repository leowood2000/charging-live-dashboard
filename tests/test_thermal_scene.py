import unittest

import server


class ThermalSceneTests(unittest.TestCase):
    def test_sconfig_is_preferred_over_rolling_dump_scene(self):
        result = server.parse_thermal_dump(
            "[VIDEO-MONITOR-WIRELESS][VIRTUAL-SENSOR-FORMULA 33500]",
            "27", "1", True)
        self.assertEqual(result["scene"], "charge（充电中）")
        self.assertEqual(result["scene_source"], "sconfig")
        self.assertEqual(result["sconfig"], 27)

    def test_screen_off_charging_uses_special_chg_only_scene(self):
        result = server.parse_thermal_dump(
            "[VIDEO-MONITOR-WIRELESS][VIRTUAL-SENSOR-FORMULA 33500]",
            "11", "0", True)
        self.assertEqual(result["scene"], "chg-only（熄屏充电）")
        self.assertEqual(result["scene_source"], "screen_state+sconfig")

    def test_screen_off_without_charging_does_not_claim_chg_only(self):
        result = server.parse_thermal_dump("", "11", "0", False)
        self.assertEqual(result["scene"], "video（视频）")
        self.assertEqual(result["scene_source"], "sconfig")


if __name__ == "__main__":
    unittest.main()
