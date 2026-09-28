"""Environment-backed configuration for the workshop monitor."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _float_env(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return float(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc


def _positive_float_env(name: str, default: float) -> float:
    value = _float_env(name, default)
    if value <= 0:
        raise ValueError(f"{name} must be greater than zero")
    return value


@dataclass(frozen=True)
class Config:
    telegram_bot_token: str
    telegram_chat_id: int
    ups_name: str
    ups_watts: float
    baseline_watts: float
    power_sample_interval: float
    calibration_duration: float
    calibration_sample_interval: float
    monitor_interval: float
    nut_timeout: float
    data_dir: Path
    nut_failure_warning_threshold: int

    @classmethod
    def from_env(cls) -> "Config":
        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        if not token:
            raise ValueError("TELEGRAM_BOT_TOKEN is required")

        raw_chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
        if not raw_chat_id:
            raise ValueError("TELEGRAM_CHAT_ID is required")
        try:
            chat_id = int(raw_chat_id)
        except ValueError as exc:
            raise ValueError("TELEGRAM_CHAT_ID must be an integer") from exc

        warning_threshold = int(os.getenv("NUT_FAILURE_WARNING_THRESHOLD", "3"))
        if warning_threshold < 1:
            raise ValueError("NUT_FAILURE_WARNING_THRESHOLD must be at least 1")

        return cls(
            telegram_bot_token=token,
            telegram_chat_id=chat_id,
            ups_name=os.getenv("UPS_NAME", "myton@localhost"),
            ups_watts=_positive_float_env("UPS_WATTS", 2700.0),
            baseline_watts=max(_float_env("BASELINE_WATTS", 27.0), 0.0),
            power_sample_interval=_positive_float_env(
                "POWER_SAMPLE_INTERVAL", 5.0
            ),
            calibration_duration=_positive_float_env("CALIBRATION_DURATION", 300.0),
            calibration_sample_interval=_positive_float_env(
                "CALIBRATION_SAMPLE_INTERVAL", 5.0
            ),
            monitor_interval=_positive_float_env("UPS_MONITOR_INTERVAL", 5.0),
            nut_timeout=_positive_float_env("NUT_TIMEOUT", 5.0),
            data_dir=Path(os.getenv("DATA_DIR", "data")).expanduser(),
            nut_failure_warning_threshold=warning_threshold,
        )
