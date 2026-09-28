import csv
import tempfile
import unittest
from pathlib import Path

from power_logger import PowerSessionManager


class CalibratedSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_calibration_is_applied_and_snapshotted_in_csv_and_result(self):
        def reader():
            return {
                "ups.load": "10",
                "ups.status": "OL CHRG",
                "battery.charge": "100",
                "battery.runtime": "3000",
                "battery.voltage": "82.0",
            }

        with tempfile.TemporaryDirectory() as directory:
            manager = PowerSessionManager(
                reader=reader,
                data_dir=Path(directory),
                ups_watts=2700,
                baseline_watts=27,
                sample_interval=3600,
            )
            manager.set_baseline(201, 15, "calibration test-id")
            session = await manager.start("Calibrated test")

            self.assertEqual(session.samples[0]["total_watts"], 270)
            self.assertEqual(session.samples[0]["printer_watts"], 69)

            result = await manager.stop()
            self.assertEqual(result.baseline_watts, 201)
            self.assertEqual(result.baseline_uncertainty_watts, 15)
            self.assertEqual(result.baseline_source, "calibration test-id")

            with session.csv_path.open(newline="", encoding="utf-8") as handle:
                row = next(csv.DictReader(handle))
            self.assertEqual(row["baseline_watts"], "201.0")
            self.assertEqual(row["baseline_uncertainty_watts"], "15.0")
            self.assertEqual(row["baseline_source"], "calibration test-id")


if __name__ == "__main__":
    unittest.main()
