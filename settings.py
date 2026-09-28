"""Persistent user settings that are safe to keep in the data directory."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

LOGGER = logging.getLogger(__name__)
CURRENCY_RE = re.compile(r"^[A-Z][A-Z0-9]{2,7}$")


@dataclass(frozen=True)
class ElectricityPrice:
    amount_per_kwh: str
    currency: str

    @property
    def amount(self) -> Decimal:
        return Decimal(self.amount_per_kwh)


def parse_electricity_price(amount: str, currency: str) -> ElectricityPrice:
    try:
        parsed = Decimal(amount)
    except InvalidOperation as exc:
        raise ValueError("Price must be a number, for example 2.75") from exc
    if not parsed.is_finite() or parsed <= 0:
        raise ValueError("Price must be greater than zero")

    normalized_currency = currency.strip().upper()
    if not CURRENCY_RE.fullmatch(normalized_currency):
        raise ValueError("Currency must be a 3-8 character code such as TRY or EUR")
    return ElectricityPrice(format(parsed.normalize(), "f"), normalized_currency)


class SettingsStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def load_price(self) -> ElectricityPrice | None:
        try:
            values = json.loads(self.path.read_text(encoding="utf-8"))
            price = values.get("electricity_price")
            if price is None:
                return None
            if not isinstance(price, dict):
                raise ValueError("electricity_price must be an object")
            return parse_electricity_price(
                str(price["amount_per_kwh"]), str(price["currency"])
            )
        except FileNotFoundError:
            return None
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            LOGGER.exception("Could not load settings from %s", self.path)
            return None

    def save_price(self, price: ElectricityPrice | None) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        values: dict[str, object] = {}
        try:
            current = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(current, dict):
                values.update(current)
        except (FileNotFoundError, OSError, ValueError, json.JSONDecodeError):
            pass

        if price is None:
            values.pop("electricity_price", None)
        else:
            values["electricity_price"] = asdict(price)

        temporary_path = self.path.with_suffix(".json.tmp")
        temporary_path.write_text(
            json.dumps(values, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary_path.replace(self.path)
