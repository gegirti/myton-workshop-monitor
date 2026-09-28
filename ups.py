"""Small, dependency-free interface to Network UPS Tools."""

from __future__ import annotations

import subprocess
from collections.abc import Mapping


def parse_ups_output(output: str) -> dict[str, str]:
    """Parse ``upsc`` key/value output, ignoring malformed lines."""
    data: dict[str, str] = {}
    for raw_line in output.splitlines():
        if ":" not in raw_line:
            continue
        key, value = raw_line.split(":", 1)
        key = key.strip()
        if key:
            data[key] = value.strip()
    return data


def read_ups(ups_name: str, timeout: float = 5.0) -> dict[str, str]:
    """Read a UPS through ``upsc`` with a bounded execution time."""
    try:
        result = subprocess.run(
            ["upsc", ups_name],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"upsc timed out after {timeout:g} seconds") from exc
    except OSError as exc:
        raise RuntimeError(f"could not run upsc: {exc}") from exc

    if result.returncode != 0:
        detail = result.stderr.strip() or f"exit status {result.returncode}"
        raise RuntimeError(f"upsc failed: {detail}")

    data = parse_ups_output(result.stdout)
    if not data:
        raise RuntimeError("upsc returned no structured data")
    return data


def optional_float(data: Mapping[str, str], key: str) -> float | None:
    value = data.get(key)
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def load_to_watts(load_percent: float, ups_watts: float) -> float:
    return float(load_percent) * float(ups_watts) / 100.0


def printer_watts(total_watts: float, baseline_watts: float) -> float:
    return max(float(total_watts) - float(baseline_watts), 0.0)


def fmt_runtime(seconds: object) -> str:
    try:
        seconds_int = max(int(float(seconds)), 0)
    except (ValueError, TypeError):
        return "Unknown"

    hours, remainder = divmod(seconds_int, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    return f"{minutes}m {secs}s"


def status_text(status: str) -> str:
    translations = {
        "OL": "Online",
        "OB": "On battery",
        "CHRG": "Charging",
        "DISCHRG": "Discharging",
        "LB": "Low battery",
        "OVER": "Overload",
        "BYPASS": "Bypass",
        "OFF": "Output off",
    }
    return ", ".join(translations.get(code, code) for code in status.split())
