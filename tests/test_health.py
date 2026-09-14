import unittest
from unittest.mock import patch
import health

class TestHealth(unittest.TestCase):
    def test_get_server_metrics_keys(self):
        metrics = health.get_server_metrics()
        self.assertIn("cpu_pct", metrics)
        self.assertIn("ram_total_mb", metrics)
        self.assertIn("disk_free_gb", metrics)
        self.assertIn("battery_pct", metrics)
        self.assertIn("uptime_str", metrics)

    @patch("health.send_msg")
    @patch("health.get_server_metrics")
    def test_critical_alerts_triggers(self, mock_metrics, mock_send_msg):
        mock_send_msg.return_value.ok = True
        health.LAST_HEALTH_ALERTS = {"battery": 0, "disk": 0, "ram": 0, "temp": 0}

        # Simulated critical conditions: low battery (10% unplugged), low disk (1.2GB), high RAM (95%), high temp (44°C)
        mock_metrics.return_value = {
            "battery_pct": 10,
            "battery_status": "DISCHARGING",
            "battery_temp": 44.0,
            "disk_free_gb": 1.2,
            "disk_free_pct": 3.5,
            "disk_total_gb": 32.0,
            "ram_pct": 95.0,
            "ram_used_mb": 5800,
            "ram_total_mb": 6000,
            "uptime_str": "12h 30m"
        }

        health.check_hardware_health_and_alert()
        self.assertEqual(mock_send_msg.call_count, 4)

if __name__ == '__main__':
    unittest.main()
