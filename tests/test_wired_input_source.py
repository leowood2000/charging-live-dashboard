import unittest

from server import resolve_wired_input_source


class WiredInputSourceTests(unittest.TestCase):
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
