import unittest
from unittest.mock import patch

import network_checker as nc


def device(name, kind, usb=False):
    return {"name": name, "type": kind, "state": "connected", "usb_backed": usb,
            "default_routes": [{"gateway": "192.0.2.1", "metric": 100}]}


class FailoverTests(unittest.TestCase):
    def test_switch_after_three_failures_and_six_recoveries(self):
        machine = nc.Failover(3, 6)
        self.assertEqual(machine.decide(True, True), "primary")
        machine.selected = "primary"
        self.assertEqual(machine.decide(False, True), "primary")
        self.assertEqual(machine.decide(False, True), "primary")
        self.assertEqual(machine.decide(False, True), "backup")
        machine.selected = "backup"
        for _ in range(5):
            self.assertEqual(machine.decide(True, True), "backup")
        self.assertEqual(machine.decide(True, True), "primary")

    def test_never_switch_to_unhealthy_backup(self):
        machine = nc.Failover(2, 2)
        machine.selected = "primary"
        for _ in range(3):
            self.assertEqual(machine.decide(False, False), "primary")

    def test_discover_wifi(self):
        snap = {"devices": [device("end0", "ethernet"), device("wlan0", "wifi")]}
        self.assertEqual(nc.discover(snap, {}), ("end0", "wlan0"))

    def test_discover_usb_modem_ethernet(self):
        snap = {"devices": [device("end0", "ethernet"), device("enx123", "ethernet", True)]}
        self.assertEqual(nc.discover(snap, {}), ("end0", "enx123"))

    def test_ambiguous_backup_does_not_guess(self):
        snap = {"devices": [device("end0", "ethernet"), device("wlan0", "wifi"), device("wwan0", "wwan")]}
        with self.assertRaisesRegex(ValueError, "ambigu"):
            nc.discover(snap, {})
        self.assertEqual(nc.discover(snap, {"backup_interface": "wwan0"}), ("end0", "wwan0"))

    def test_wifi_station_preferred_over_access_point_without_default_route(self):
        access_point = device("wlan0ap", "wifi")
        access_point["default_routes"] = []
        lan = device("eth0", "ethernet")
        lan["state"] = "unavailable"
        lan["default_routes"] = []
        snap = {"devices": [access_point, device("wlan0", "wifi"), lan]}
        self.assertEqual(nc.discover(snap, {}), ("eth0", "wlan0"))

    def test_two_wifi_devices_without_default_routes_remain_ambiguous(self):
        access_point = device("wlan0ap", "wifi")
        access_point["default_routes"] = []
        station = device("wlan0", "wifi")
        station["default_routes"] = []
        snap = {"devices": [device("eth0", "ethernet"), access_point, station]}
        with self.assertRaisesRegex(ValueError, "ambigu"):
            nc.discover(snap, {})

    def test_no_gateway_detected_but_not_usable(self):
        other = device("wlan0", "wifi")
        other["default_routes"] = []
        self.assertEqual(nc.discover({"devices": [device("end0", "ethernet"), other]}, {}), ("end0", "wlan0"))

    def test_dry_run_never_executes_network_mutation(self):
        with patch.object(nc, "command") as cmd:
            self.assertTrue(nc.set_route_metric("wlan0", 50, False))
            cmd.assert_not_called()

    def test_metric_preference_verification(self):
        result = unittest.mock.Mock(returncode=0, stdout='[{"dev":"end0","metric":600},{"dev":"wlan0","metric":50}]')
        with patch.object(nc, "command", return_value=result):
            self.assertTrue(nc.route_is_preferred("wlan0", "end0"))
            self.assertFalse(nc.route_is_preferred("end0", "wlan0"))

    def test_validate_config_rejects_malformed(self):
        config = {"interval_seconds": 10, "timeout_seconds": 4, "fail_threshold": 3,
                  "recover_threshold": 6, "preferred_metric": 50, "standby_metric": 600,
                  "probe_urls": ["https://example.org/generate_204"]}
        nc.validate_config(config)
        with self.assertRaises(ValueError):
            nc.validate_config({**config, "probe_urls": ["http://example.org/"]})
        with self.assertRaises(ValueError):
            nc.validate_config({**config, "preferred_metric": 700})


if __name__ == "__main__":
    unittest.main()
