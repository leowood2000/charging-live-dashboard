import unittest

from server import resolve_wireless_connected


class WirelessConnectionTests(unittest.TestCase):
    def test_latched_on_survives_low_rx_bypass_current(self):
        self.assertTrue(resolve_wireless_connected(True, "none", 8500))

    def test_latched_off_rejects_residual_vout(self):
        self.assertFalse(resolve_wireless_connected(False, "wireless", 8500))

    def test_unknown_connection_falls_back_to_vout(self):
        self.assertTrue(resolve_wireless_connected(None, "none", 8500))


if __name__ == "__main__":
    unittest.main()
