import unittest

from ups import (
    fmt_runtime,
    load_to_watts,
    optional_float,
    parse_ups_output,
    printer_watts,
)


REALISTIC_OUTPUT = """battery.charge: 98
battery.runtime: 3231
battery.type: PbAc
battery.voltage: 82.00
output.frequency: 49.90
output.voltage: 230.2
ups.load: 10
ups.status: OL CHRG
ups.mfr: PHOENIXTEC
ups.model: InnovaBasicG2
"""


class UpsTests(unittest.TestCase):
    def test_parse_realistic_output(self):
        data = parse_ups_output(REALISTIC_OUTPUT)
        self.assertEqual(data["ups.load"], "10")
        self.assertEqual(data["ups.status"], "OL CHRG")
        self.assertEqual(data["ups.model"], "InnovaBasicG2")

    def test_parse_ignores_invalid_lines_and_accepts_no_space(self):
        data = parse_ups_output("invalid\nups.load:10\n: ignored\nbattery.charge: n/a")
        self.assertEqual(data["ups.load"], "10")
        self.assertNotIn("", data)
        self.assertIsNone(optional_float(data, "battery.charge"))
        self.assertIsNone(optional_float(data, "missing"))

    def test_load_to_watts(self):
        self.assertEqual(load_to_watts(1, 2700), 27)
        self.assertEqual(load_to_watts(10, 2700), 270)

    def test_baseline_subtraction_never_goes_negative(self):
        self.assertEqual(printer_watts(270, 27), 243)
        self.assertEqual(printer_watts(0, 27), 0)

    def test_runtime_formatting(self):
        self.assertEqual(fmt_runtime("3231"), "53m 51s")
        self.assertEqual(fmt_runtime(3661), "1h 1m 1s")
        self.assertEqual(fmt_runtime("bad"), "Unknown")
        self.assertEqual(fmt_runtime(None), "Unknown")


if __name__ == "__main__":
    unittest.main()
