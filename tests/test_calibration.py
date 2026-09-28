import asyncio
import math
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from calibration import (
    CalibrationActiveError,
    CalibrationManager,
    CalibrationStore,
    calibration_statistics,
)


def ups_sample(load="7"):
    return {"ups.load": load, "ups.status": "OL CHRG"}


class CalibrationCalculationTests(unittest.TestCase):
    def test_time_weighted_average_and_uncertainty(self):
        samples = [
            {"elapsed_seconds": 0, "ups.load": 7, "total_watts": 189},
            {"elapsed_seconds": 10, "ups.load": 8, "total_watts": 216},
            {"elapsed_seconds": 30, "ups.load": 7, "total_watts": 189},
        ]
        result = calibration_statistics(
            samples,
            "cal-1",
            datetime.now(timezone.utc),
            ups_watts=2700,
            sample_interval=5,
        )
        self.assertAlmostEqual(result.baseline_watts, 202.5)
        self.assertEqual(result.sample_count, 3)
        self.assertEqual(result.min_watts, 189)
        self.assertEqual(result.max_watts, 216)
        self.assertAlmostEqual(
            result.quantization_uncertainty_watts, 27 / math.sqrt(12)
        )
        self.assertGreater(result.uncertainty_watts, result.observed_stddev_watts)

    def test_store_round_trip_and_ups_rating_guard(self):
        samples = [
            {"elapsed_seconds": 0, "ups.load": 7, "total_watts": 189},
            {"elapsed_seconds": 5, "ups.load": 8, "total_watts": 216},
        ]
        calibration = calibration_statistics(
            samples,
            "cal-2",
            datetime.now(timezone.utc),
            ups_watts=2700,
            sample_interval=5,
        )
        with tempfile.TemporaryDirectory() as directory:
            store = CalibrationStore(Path(directory) / "baseline.json")
            store.save(calibration)
            loaded = store.load(2700)
            self.assertIsNotNone(loaded)
            self.assertEqual(loaded.calibration_id, "cal-2")
            self.assertIsNone(store.load(3000))


class CalibrationManagerTests(unittest.IsolatedAsyncioTestCase):
    async def test_calibration_completes_persists_and_refuses_second_run(self):
        completed = asyncio.Event()
        outcome = {}

        async def on_complete(calibration, error):
            outcome["calibration"] = calibration
            outcome["error"] = error
            completed.set()

        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            store = CalibrationStore(data_dir / "baseline.json")
            manager = CalibrationManager(
                reader=ups_sample,
                store=store,
                data_dir=data_dir,
                ups_watts=2700,
                sample_interval=0.01,
                duration=0.04,
            )
            session = await manager.start(on_complete)
            with self.assertRaises(CalibrationActiveError):
                await manager.start(on_complete)

            await asyncio.wait_for(completed.wait(), timeout=1)

            self.assertIsNone(outcome["error"])
            self.assertIsNotNone(outcome["calibration"])
            self.assertIsNone(manager.active)
            self.assertTrue(session.csv_path.exists())
            self.assertIsNotNone(store.load(2700))


if __name__ == "__main__":
    unittest.main()
