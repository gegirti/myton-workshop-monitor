import asyncio
import tempfile
import unittest
from pathlib import Path

from power_controller import PowerController, parse_duration, parse_powerstart_args
from power_logger import PowerSessionManager, SessionAlreadyActiveError


def ups_sample(load: int) -> dict[str, str]:
    return {
        "ups.load": str(load),
        "ups.status": "OL",
        "battery.charge": "100",
        "battery.runtime": "3200",
        "battery.voltage": "82.1",
        "output.voltage": "230.0",
        "output.frequency": "50.0",
    }


class DurationParsingTests(unittest.TestCase):
    def test_supported_duration_formats(self) -> None:
        self.assertEqual(parse_duration("4h"), 14400)
        self.assertEqual(parse_duration("90m"), 5400)
        self.assertEqual(parse_duration("2h30m"), 9000)

    def test_invalid_duration(self) -> None:
        for value in ("", "four hours", "0m", "31d"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_duration(value)

    def test_powerstart_name_and_optional_duration(self) -> None:
        self.assertEqual(
            parse_powerstart_args(["hi_benchy", "4h"]),
            ("hi benchy", 14400),
        )
        self.assertEqual(parse_powerstart_args(["hi_benchy"]), ("hi benchy", None))


class PowerControllerTests(unittest.IsolatedAsyncioTestCase):
    async def test_second_session_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manager = PowerSessionManager(
                reader=lambda: ups_sample(7),
                data_dir=Path(temporary),
                ups_watts=2700,
                baseline_watts=192.2,
                baseline_uncertainty_watts=11.7,
                sample_interval=0.01,
            )
            controller = PowerController(manager, 0.05, 3, 6, 3)

            async def completed(_result: object) -> None:
                return None

            await controller.start("first", None, None, completed)
            with self.assertRaises(SessionAlreadyActiveError):
                await controller.start("second", None, None, completed)
            await controller.shutdown()

    async def test_calibrated_idle_ends_timed_session(self) -> None:
        # 10% gives 270 W total, or 77.8 W above the 192.2 W baseline.
        # That is above 6 × 11.7 W; 7% returns to calibrated idle.
        loads = iter([7, 10, 10, 10, 7, 7, 7, 7, 7, 7, 7])

        def reader() -> dict[str, str]:
            return ups_sample(next(loads, 7))

        with tempfile.TemporaryDirectory() as temporary:
            manager = PowerSessionManager(
                reader=reader,
                data_dir=Path(temporary),
                ups_watts=2700,
                baseline_watts=192.2,
                baseline_uncertainty_watts=11.7,
                baseline_source="test calibration",
                sample_interval=0.01,
            )
            controller = PowerController(manager, 0.03, 3, 6, 3)
            finished = asyncio.Event()
            result_holder = []

            async def completed(result: object) -> None:
                result_holder.append(result)
                finished.set()

            state = await controller.start("benchy", 1.0, None, completed)
            self.assertAlmostEqual(state.idle_threshold_watts or 0, 35.1)
            self.assertAlmostEqual(state.activity_threshold_watts or 0, 70.2)
            await asyncio.wait_for(finished.wait(), timeout=2)

            completed_result = result_holder[0]
            self.assertEqual(completed_result.stop_reason, "calibrated idle detected")
            self.assertTrue(completed_result.activity_detected)
            self.assertTrue(completed_result.metadata_path.exists())
            self.assertIsNone(controller.active)

    async def test_hard_timer_works_without_calibration_uncertainty(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manager = PowerSessionManager(
                reader=lambda: ups_sample(7),
                data_dir=Path(temporary),
                ups_watts=2700,
                baseline_watts=192.2,
                baseline_uncertainty_watts=0,
                sample_interval=0.01,
            )
            controller = PowerController(manager, 0.03, 3, 6, 3)
            finished = asyncio.Event()
            result_holder = []

            async def completed(result: object) -> None:
                result_holder.append(result)
                finished.set()

            state = await controller.start("timer", 0.04, None, completed)
            self.assertIsNone(state.idle_threshold_watts)
            await asyncio.wait_for(finished.wait(), timeout=2)
            self.assertEqual(result_holder[0].stop_reason, "maximum timer reached")


if __name__ == "__main__":
    unittest.main()
