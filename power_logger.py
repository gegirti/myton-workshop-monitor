"""Power measurement sessions, CSV persistence, statistics, and plots."""

from __future__ import annotations

import asyncio
import csv
import logging
import math
import re
import statistics
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from ups import load_to_watts, optional_float, printer_watts

LOGGER = logging.getLogger(__name__)

CSV_FIELDS = (
    "session_id",
    "test_name",
    "timestamp",
    "elapsed_seconds",
    "ups.load",
    "total_watts",
    "printer_watts",
    "baseline_watts",
    "baseline_uncertainty_watts",
    "baseline_source",
    "battery.charge",
    "battery.runtime",
    "battery.voltage",
    "output.voltage",
    "output.frequency",
    "ups.status",
)


class SessionAlreadyActiveError(RuntimeError):
    pass


@dataclass
class PowerSession:
    session_id: str
    name: str
    started_at: datetime
    start_monotonic: float
    starting_ups_state: str
    starting_battery_charge: float | None
    starting_battery_voltage: float | None
    csv_path: Path
    samples: list[dict[str, object]] = field(default_factory=list)
    task: asyncio.Task[None] | None = None


@dataclass(frozen=True)
class SessionResult:
    session: PowerSession
    finished_at: datetime
    statistics: dict[str, float | int | None]
    baseline_watts: float = 0.0
    baseline_uncertainty_watts: float = 0.0
    baseline_source: str = "configured fallback"


def integrate_energy_wh(
    samples: list[dict[str, object]], value_key: str
) -> float:
    """Trapezoidal integration over actual elapsed sample timestamps."""
    if len(samples) < 2:
        return 0.0
    energy_watt_seconds = 0.0
    for previous, current in zip(samples, samples[1:]):
        try:
            t0 = float(previous["elapsed_seconds"])
            t1 = float(current["elapsed_seconds"])
            v0 = float(previous[value_key])
            v1 = float(current[value_key])
        except (KeyError, TypeError, ValueError):
            continue
        if not all(math.isfinite(value) for value in (t0, t1, v0, v1)):
            continue
        if t1 > t0:
            energy_watt_seconds += (v0 + v1) * 0.5 * (t1 - t0)
    return energy_watt_seconds / 3600.0


def calculate_statistics(
    samples: list[dict[str, object]],
    starting_charge: float | None = None,
    starting_voltage: float | None = None,
) -> dict[str, float | int | None]:
    if not samples:
        return {
            "duration_seconds": 0.0,
            "sample_count": 0,
            "min_total_watts": None,
            "max_total_watts": None,
            "avg_total_watts": None,
            "min_printer_watts": None,
            "max_printer_watts": None,
            "avg_printer_watts": None,
            "avg_load_percent": None,
            "starting_battery_charge": starting_charge,
            "ending_battery_charge": None,
            "starting_battery_voltage": starting_voltage,
            "ending_battery_voltage": None,
            "total_energy_wh": 0.0,
            "printer_energy_wh": 0.0,
        }

    def numeric_values(key: str) -> list[float]:
        values: list[float] = []
        for sample in samples:
            try:
                value = float(sample[key])
            except (KeyError, TypeError, ValueError):
                continue
            if math.isfinite(value):
                values.append(value)
        return values

    def last_numeric(key: str) -> float | None:
        for sample in reversed(samples):
            try:
                value = float(sample[key])
            except (KeyError, TypeError, ValueError):
                continue
            if math.isfinite(value):
                return value
        return None

    total_values = numeric_values("total_watts")
    printer_values = numeric_values("printer_watts")
    load_values = numeric_values("ups.load")
    elapsed_values = numeric_values("elapsed_seconds")

    return {
        "duration_seconds": max(elapsed_values, default=0.0),
        "sample_count": len(samples),
        "min_total_watts": min(total_values, default=None),
        "max_total_watts": max(total_values, default=None),
        "avg_total_watts": statistics.fmean(total_values) if total_values else None,
        "min_printer_watts": min(printer_values, default=None),
        "max_printer_watts": max(printer_values, default=None),
        "avg_printer_watts": (
            statistics.fmean(printer_values) if printer_values else None
        ),
        "avg_load_percent": statistics.fmean(load_values) if load_values else None,
        "starting_battery_charge": starting_charge,
        "ending_battery_charge": last_numeric("battery.charge"),
        "starting_battery_voltage": starting_voltage,
        "ending_battery_voltage": last_numeric("battery.voltage"),
        "total_energy_wh": integrate_energy_wh(samples, "total_watts"),
        "printer_energy_wh": integrate_energy_wh(samples, "printer_watts"),
    }


class PowerSessionManager:
    def __init__(
        self,
        reader: Callable[[], dict[str, str]],
        data_dir: Path,
        ups_watts: float,
        baseline_watts: float,
        sample_interval: float,
        baseline_uncertainty_watts: float = 0.0,
        baseline_source: str = "configured fallback",
        failure_warning_threshold: int = 3,
    ) -> None:
        self._reader = reader
        self.data_dir = data_dir
        self.ups_watts = ups_watts
        self.baseline_watts = baseline_watts
        self.sample_interval = sample_interval
        self.baseline_uncertainty_watts = baseline_uncertainty_watts
        self.baseline_source = baseline_source
        self.failure_warning_threshold = failure_warning_threshold
        self._active: PowerSession | None = None
        self._lock = asyncio.Lock()

    @property
    def active(self) -> PowerSession | None:
        return self._active

    def set_baseline(
        self, watts: float, uncertainty_watts: float, source: str
    ) -> None:
        if self._active is not None:
            raise RuntimeError("Cannot change calibration during a power test")
        self.baseline_watts = max(float(watts), 0.0)
        self.baseline_uncertainty_watts = max(float(uncertainty_watts), 0.0)
        self.baseline_source = source

    async def start(self, name: str) -> PowerSession:
        clean_name = " ".join(name.split()).strip()
        if not clean_name:
            raise ValueError("A test name is required")
        if len(clean_name) > 120:
            raise ValueError("Test name must be 120 characters or fewer")

        async with self._lock:
            if self._active is not None:
                raise SessionAlreadyActiveError(
                    f"Power test '{self._active.name}' is already active"
                )

            initial_data = await asyncio.to_thread(self._reader)
            if optional_float(initial_data, "ups.load") is None:
                raise ValueError("NUT data has no valid ups.load")
            now = datetime.now(timezone.utc)
            loop = asyncio.get_running_loop()
            session_id = f"{now:%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:8]}"
            safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", clean_name).strip("._")
            safe_name = safe_name[:60] or "power-test"
            self.data_dir.mkdir(parents=True, exist_ok=True)
            csv_path = self.data_dir / f"{session_id}-{safe_name}.csv"
            with csv_path.open("x", newline="", encoding="utf-8") as handle:
                csv.DictWriter(handle, fieldnames=CSV_FIELDS).writeheader()

            session = PowerSession(
                session_id=session_id,
                name=clean_name,
                started_at=now,
                start_monotonic=loop.time(),
                starting_ups_state=initial_data.get("ups.status", ""),
                starting_battery_charge=optional_float(initial_data, "battery.charge"),
                starting_battery_voltage=optional_float(initial_data, "battery.voltage"),
                csv_path=csv_path,
            )
            self._active = session
            self._record_sample(session, initial_data, 0.0, now)
            session.task = asyncio.create_task(
                self._sampling_loop(session), name=f"power-session-{session_id}"
            )
            return session

    async def stop(self) -> SessionResult | None:
        async with self._lock:
            session = self._active
            if session is None:
                return None
            self._active = None
            task = session.task

        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        return SessionResult(
            session=session,
            finished_at=datetime.now(timezone.utc),
            statistics=calculate_statistics(
                session.samples,
                session.starting_battery_charge,
                session.starting_battery_voltage,
            ),
            baseline_watts=self.baseline_watts,
            baseline_uncertainty_watts=self.baseline_uncertainty_watts,
            baseline_source=self.baseline_source,
        )

    async def shutdown(self) -> None:
        result = await self.stop()
        if result is not None:
            LOGGER.info(
                "Stopped active power session %s during shutdown",
                result.session.session_id,
            )

    def status(self) -> dict[str, object] | None:
        session = self._active
        if session is None:
            return None
        latest = session.samples[-1] if session.samples else None
        elapsed = asyncio.get_running_loop().time() - session.start_monotonic
        return {
            "session_id": session.session_id,
            "name": session.name,
            "elapsed_seconds": max(elapsed, 0.0),
            "sample_count": len(session.samples),
            "latest": latest,
        }

    async def _sampling_loop(self, session: PowerSession) -> None:
        consecutive_failures = 0
        while True:
            await asyncio.sleep(self.sample_interval)
            if self._active is not session:
                return
            try:
                data = await asyncio.to_thread(self._reader)
                elapsed = asyncio.get_running_loop().time() - session.start_monotonic
                self._record_sample(
                    session, data, elapsed, datetime.now(timezone.utc)
                )
                if consecutive_failures:
                    LOGGER.info("NUT power sampling recovered")
                consecutive_failures = 0
            except asyncio.CancelledError:
                raise
            except Exception:
                consecutive_failures += 1
                LOGGER.exception("Power sample failed; the session will continue")
                if consecutive_failures >= self.failure_warning_threshold:
                    LOGGER.warning(
                        "NUT power sampling has failed %d consecutive times",
                        consecutive_failures,
                    )

    def _record_sample(
        self,
        session: PowerSession,
        data: dict[str, str],
        elapsed: float,
        timestamp: datetime,
    ) -> None:
        load = optional_float(data, "ups.load")
        if load is None:
            raise ValueError("NUT sample has no valid ups.load")
        total = load_to_watts(load, self.ups_watts)
        sample: dict[str, object] = {
            "session_id": session.session_id,
            "test_name": session.name,
            "timestamp": timestamp.isoformat(),
            "elapsed_seconds": round(elapsed, 3),
            "ups.load": load,
            "total_watts": total,
            "printer_watts": printer_watts(total, self.baseline_watts),
            "baseline_watts": self.baseline_watts,
            "baseline_uncertainty_watts": self.baseline_uncertainty_watts,
            "baseline_source": self.baseline_source,
            "battery.charge": optional_float(data, "battery.charge"),
            "battery.runtime": optional_float(data, "battery.runtime"),
            "battery.voltage": optional_float(data, "battery.voltage"),
            "output.voltage": optional_float(data, "output.voltage"),
            "output.frequency": optional_float(data, "output.frequency"),
            "ups.status": data.get("ups.status", ""),
        }
        session.samples.append(sample)
        try:
            with session.csv_path.open("a", newline="", encoding="utf-8") as handle:
                csv.DictWriter(handle, fieldnames=CSV_FIELDS).writerow(sample)
        except OSError:
            LOGGER.exception("Could not append power sample to %s", session.csv_path)


def generate_plot(result: SessionResult, baseline_watts: float) -> Path:
    """Generate a compact mobile-readable PNG beside the session CSV."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    samples = result.session.samples
    if not samples:
        raise ValueError("Cannot plot a session with no samples")
    elapsed_minutes = [float(row["elapsed_seconds"]) / 60.0 for row in samples]
    total = [float(row["total_watts"]) for row in samples]
    printer = [float(row["printer_watts"]) for row in samples]
    uncertainty = result.baseline_uncertainty_watts
    printer_lower = [max(value - uncertainty, 0.0) for value in printer]
    printer_upper = [value + uncertainty for value in printer]
    stats = result.statistics

    figure, axis = plt.subplots(figsize=(9, 5), dpi=140)
    axis.plot(elapsed_minutes, total, label="UPS total", linewidth=2)
    axis.plot(elapsed_minutes, printer, label="Printer estimate", linewidth=2)
    if uncertainty > 0:
        axis.fill_between(
            elapsed_minutes, printer_lower, printer_upper, alpha=0.18,
            label="Calibration uncertainty",
        )
    peak_index = max(range(len(printer)), key=printer.__getitem__)
    axis.scatter(
        elapsed_minutes[peak_index], printer[peak_index], color="crimson", s=35, zorder=3
    )
    duration = format_duration(float(stats["duration_seconds"] or 0))
    average = float(stats["avg_printer_watts"] or 0)
    peak = float(stats["max_printer_watts"] or 0)
    axis.set_title(
        f"{result.session.name}\n{duration} · printer avg {average:.0f} W · peak {peak:.0f} W"
    )
    axis.set_xlabel("Elapsed time (minutes)")
    axis.set_ylabel("Power (W)")
    axis.set_ylim(bottom=0)
    axis.grid(alpha=0.25)
    axis.legend()
    figure.text(
        0.99,
        0.01,
        f"Baseline: {baseline_watts:.1f} ± {uncertainty:.1f} W",
        ha="right",
        fontsize=8,
    )
    figure.tight_layout(rect=(0, 0.03, 1, 1))

    png_path = result.session.csv_path.with_suffix(".png")
    figure.savefig(png_path)
    plt.close(figure)
    return png_path


def format_duration(seconds: float) -> str:
    seconds_int = max(int(round(seconds)), 0)
    hours, remainder = divmod(seconds_int, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    return f"{minutes}m {secs}s"
