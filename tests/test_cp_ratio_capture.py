import unittest

from server import parse_session_cp_state


class CpRatioCaptureTests(unittest.TestCase):
    def test_wireless_sc8581_operation_line_supplies_ratio(self):
        text = """[22:30:00:000-I] wireless power_good_on
[22:30:04:142-I][cp_sc8581]sc8581_set_operation_mode:906 [0] set operation mode 1 reg 2 work_mode 2
"""
        state = parse_session_cp_state(text, fname="mca_log_0817_2200.log")
        wireless = state["wireless"]
        self.assertEqual(wireless[0], 1)
        self.assertEqual(wireless[1], 2)


if __name__ == "__main__":
    unittest.main()
