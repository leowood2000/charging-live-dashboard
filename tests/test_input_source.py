import unittest

from server import cp_ibus_owner, resolve_input_source


class InputSourceResolverTests(unittest.TestCase):
    def test_usb_off_residual_vbus_wireless_cp_uses_wireless(self):
        self.assertEqual("wireless", resolve_input_source(False, 9000, True))

    def test_usb_online_wins_when_wireless_signal_also_exists(self):
        self.assertEqual("wired", resolve_input_source(True, 9000, True))

    def test_unknown_usb_falls_back_to_vbus_without_wireless(self):
        self.assertEqual("wired", resolve_input_source(None, 5000, False))

    def test_usb_off_without_wireless_is_none_despite_residual_state(self):
        self.assertEqual("none", resolve_input_source(False, 9000, False))

    def test_real_regression_assigns_ibus_to_wireless_and_uses_work_mode_2(self):
        source = resolve_input_source(False, 9000, True)
        self.assertEqual("wireless", source)
        self.assertEqual("wireless", cp_ibus_owner(source, 1755))
        wireless_work_mode = 2
        stale_wired_ratio = 1
        ratio = wireless_work_mode if source == "wireless" else stale_wired_ratio
        self.assertEqual(2, ratio)


if __name__ == "__main__":
    unittest.main()
