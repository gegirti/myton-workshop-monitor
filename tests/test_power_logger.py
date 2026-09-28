import tempfile
import unittest
from pathlib import Path

from power_logger import (
    PowerSessionManager,
    SessionAlreadyActiveError,
    calculate_statistics,
    integrate_energy_wh,
)


def ups_sample(load="10", charge="98", voltage="82.0"):
    return {
        "ups.load": load,
        "ups.status": "OL CHRG",
        "battery.charge": charge,
        "battery.runtime": "3231",
        "battery.voltage": voltage,
        "output.voltage": "230.2",
        "output.frequency": "49.9",
    }


class CalculationTests(unittest.TestCase):
    def setUp(self):
        self.samples = [
            {
                "elapsed_seconds": 0,
                "ups.load": 10,
                "total_watts": 100,
                "printer_watts": 73,
                "battery.charge": 100,
                "battery.voltage": 82,
            },
            {
                "elapsed_seconds": 1800,
                "ups.load": 20,
                "total_watts": 200,
                "printer_watts": 173,
                "battery.charge": "invalid",
                "battery.voltage": None,
            },
            {
                "elapsed_seconds": 3600,
                "ups.load": 30,
                "total_watts": 300,
                "printer_watts": 273,
                "battery.charge": 96,
                "battery.voltage": 78.4,
            },
        ]

    def test_trapezoidal_energy_integration(self):
        self.assertAlmostEqual(integrate_energy_wh(self.samples, "total_watts"), 200)
        self.assertAlmostEqual(integrate_energy_wh(self.samples, "printer_watts"), 173)

    def test_statistics(self):
        stats = calculate_statistics(self.samples, 100, 82)
        self.assertEqual(stats["sample_count"], 3)
        self.assertEqual(stats["duration_seconds"], 3600)
        self.assertEqual(stats["min_total_watts"], 100)
        self.assertEqual(stats["max_total_watts"], 300)
        self.assertEqual(stats["avg_total_watts"], 200)
        self.assertEqual(stats["avg_load_percent"], 20)
        self.assertEqual(stats["ending_battery_charge"], 96)
        self.assertEqual(stats["ending_battery_voltage"], 78.4)

    def test_empty_statistics(self):
        stats = calculate_statistics([], 99, 81)
        self.assertEqual(stats["sample_count"], 0)
        self.assertEqual(stats["total_energy_wh"], 0)
        self.assertIsNone(stats["avg_total_watts"])


class SessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_second_session_is_refused_and_csv_is_persisted(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = PowerSessionManager(
                reader=ups_sample,
                data_dir=Path(directory),
                ups_watts=2700,
                baseline_watts=27,
                sample_interval=3600,
            )
            session = await manager.start("Creality Hi Benchy")
            with self.assertRaises(SessionAlreadyActiveError):
                await manager.start("Another test")
            self.assertTrue(session.csv_path.exists())
            self.assertEqual(len(session.samples), 1)
            result = await manager.stop()
            self.assertIsNotNone(result)
            self.assertIsNone(manager.active)

    async def test_invalid_initial_load_does_not_start_session(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = PowerSessionManager(
                reader=lambda: ups_sample(load="not-a-number"),
                data_dir=Path(directory),
                ups_watts=2700,
                baseline_watts=27,
                sample_interval=5,
            )
            with self.assertRaises(ValueError):
                await manager.start("Invalid")
            self.assertIsNone(manager.active)


if __name__ == "__main__":
    unittest.main()
