import tempfile
import unittest
from pathlib import Path

from settings import SettingsStore, parse_electricity_price


class ElectricityPriceTests(unittest.TestCase):
    def test_parse_normalizes_amount_and_currency(self) -> None:
        price = parse_electricity_price("2.750", "try")
        self.assertEqual(price.amount_per_kwh, "2.75")
        self.assertEqual(price.currency, "TRY")

    def test_rejects_invalid_prices_and_currency(self) -> None:
        for amount in ("0", "-1", "nan", "not-a-number"):
            with self.subTest(amount=amount), self.assertRaises(ValueError):
                parse_electricity_price(amount, "TRY")
        with self.assertRaises(ValueError):
            parse_electricity_price("2.75", "$?")

    def test_store_round_trip_and_clear(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = SettingsStore(Path(temporary) / "settings.json")
            price = parse_electricity_price("3.10", "EUR")
            store.save_price(price)
            self.assertEqual(store.load_price(), price)
            store.save_price(None)
            self.assertIsNone(store.load_price())


if __name__ == "__main__":
    unittest.main()
