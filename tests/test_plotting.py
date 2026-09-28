import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image

from plotting import generate_pillow_plot
from power_logger import PowerSession, SessionResult, calculate_statistics


class PlottingTests(unittest.TestCase):
    def test_pillow_fallback_creates_valid_png(self):
        samples = [
            {
                "elapsed_seconds": 0,
                "ups.load": 7,
                "total_watts": 189,
                "printer_watts": 162,
                "battery.charge": 100,
                "battery.voltage": 82.2,
            },
            {
                "elapsed_seconds": 30,
                "ups.load": 8,
                "total_watts": 216,
                "printer_watts": 189,
                "battery.charge": 100,
                "battery.voltage": 82.2,
            },
        ]
        with tempfile.TemporaryDirectory() as directory:
            now = datetime.now(timezone.utc)
            session = PowerSession(
                session_id="test-session",
                name="Pi fallback test",
                started_at=now,
                start_monotonic=0,
                starting_ups_state="OL CHRG",
                starting_battery_charge=100,
                starting_battery_voltage=82.2,
                csv_path=Path(directory) / "test.csv",
                samples=samples,
            )
            result = SessionResult(
                session=session,
                finished_at=now,
                statistics=calculate_statistics(samples, 100, 82.2),
            )

            png_path = generate_pillow_plot(result, baseline_watts=27)

            self.assertTrue(png_path.exists())
            with Image.open(png_path) as image:
                self.assertEqual(image.format, "PNG")
                self.assertEqual(image.size, (1200, 700))


if __name__ == "__main__":
    unittest.main()
