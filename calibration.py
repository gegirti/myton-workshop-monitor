"""Persistent background-load calibration for power measurements."""

from __future__ import annotations

import asyncio
import csv
import json
import logging
import math
import statistics
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from ups import load_to_watts, optional_float

LOGGER = logging.getLogger(__name__)

CALIBRATION_FIELDS = (
    "calibration_id",
    "timestamp",
    "elapsed_seconds",
    "ups.load",
    "total_watts",
    "ups.status",
)


class CalibrationActiveError(RuntimeError):
    pass


@dataclass(frozen=True)
class Calibration:
    calibration_id: str
    calibrated_at: str
    duration_seconds: float
    sample_count: int
    baseline_watts: float
    observed_stddev_watts: float
    quantization_uncertainty_watts: float
    uncertainty_watts: float
    min_watts: float
    max_watts: float
    average_load_percent: float
    ups_watts: float
    sample_interval_seconds: float

    @classmethod
    def from_dict(cls, values: dict[str, object]) -> "Calibration":
        return cls(
            calibration_id=str(values["calibration_id"]),
            calibrated_at=str(values["calibrated_at"]),
            duration_seconds=float(values["duration_seconds"]),
            sample_count=int(values["sample_count"]),
            baseline_watts=float(values["baseline_watts"]),
            observed_stddev_watts=float(values["observed_stddev_watts"]),
            quantization_uncertainty_watts=float(
                values["quantization_uncertainty_watts"]
            ),
            uncertainty_watts=float(values["uncertainty_watts"]),
            min_watts=float(values["min_watts"]),
            max_watts=float(values["max_watts"]),
            average_load_percent=float(values["average_load_percent"]),
            ups_watts=float(values["ups_watts"]),
            sample_interval_seconds=float(values["sample_interval_seconds"]),
        )


@dataclass
class CalibrationSession:
    calibration_id: str
    started_at: datetime
    start_monotonic: float
    csv_path: Path
    samples: list[dict[str, object]] = field(default_factory=list)
    task: asyncio.Task[None] | None = None


class CalibrationStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self, expected_ups_watts: float) -> Calibration | None:
        try:
            values = json.loads(self.path.read_text(encoding="utf-8"))
            calibration = Calibration.from_dict(values)
        except FileNotFoundError:
            return None
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            LOGGER.exception("Could not load calibration from %s", self.path)
            return None
        if not math.isclose(calibration.ups_watts, expected_ups_watts):
            LOGGER.warning(
                "Ignoring calibration for %.0f W UPS; configured UPS is %.0f W",
                calibration.ups_watts,
                expected_ups_watts,
            )
            return None
        return calibration

    def save(self, calibration: Calibration) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = self.path.with_suffix(".json.tmp")
        temporary_path.write_text(
            json.dumps(asdict(calibration), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary_path.replace(self.path)


def calibration_statistics(
    samples: list[dict[str, object]],
    calibration_id: str,
    calibrated_at: datetime,
    ups_watts: float,
    sample_interval: float,
) -> Calibration:
    if len(samples) < 2:
        raise ValueError("At least two valid samples are required for calibration")
    watts = [float(sample["total_watts"]) for sample in samples]
    loads = [float(sample["ups.load"]) for sample in samples]
    elapsed = [float(sample["elapsed_seconds"]) for sample in samples]
    duration = max(elapsed) - min(elapsed)
    if duration <= 0:
        raise ValueError("Calibration samples must span a positive duration")

    weighted_watt_seconds = 0.0
    for previous, current in zip(samples, samples[1:]):
        delta = float(current["elapsed_seconds"]) - float(
            previous["elapsed_seconds"]
        )
        if delta > 0:
            weighted_watt_seconds += (
                float(previous["total_watts"]) + float(current["total_watts"])
            ) * 0.5 * delta
    baseline = weighted_watt_seconds / duration
    observed_stddev = statistics.stdev(watts)
    quantization_uncertainty = (ups_watts / 100.0) / math.sqrt(12.0)
    combined_uncertainty = math.hypot(observed_stddev, quantization_uncertainty)

    return Calibration(
        calibration_id=calibration_id,
        calibrated_at=calibrated_at.isoformat(),
        duration_seconds=duration,
        sample_count=len(samples),
        baseline_watts=baseline,
        observed_stddev_watts=observed_stddev,
        quantization_uncertainty_watts=quantization_uncertainty,
        uncertainty_watts=combined_uncertainty,
        min_watts=min(watts),
        max_watts=max(watts),
        average_load_percent=statistics.fmean(loads),
        ups_watts=ups_watts,
        sample_interval_seconds=sample_interval,
    )


CompletionCallback = Callable[[Calibration | None, str | None], Awaitable[None]]


class CalibrationManager:
    def __init__(
        self,
        reader: Callable[[], dict[str, str]],
        store: CalibrationStore,
        data_dir: Path,
        ups_watts: float,
        sample_interval: float,
        duration: float,
        failure_warning_threshold: int = 3,
    ) -> None:
        self._reader = reader
        self.store = store
        self.data_dir = data_dir
        self.ups_watts = ups_watts
        self.sample_interval = sample_interval
        self.duration = duration
        self.failure_warning_threshold = failure_warning_threshold
        self._active: CalibrationSession | None = None
        self._lock = asyncio.Lock()

    @property
    def active(self) -> CalibrationSession | None:
        return self._active

    async def start(self, on_complete: CompletionCallback) -> CalibrationSession:
        async with self._lock:
            if self._active is not None:
                raise CalibrationActiveError("Calibration is already active")
            initial_data = await asyncio.to_thread(self._reader)
            if optional_float(initial_data, "ups.load") is None:
                raise ValueError("NUT data has no valid ups.load")

            now = datetime.now(timezone.utc)
            loop = asyncio.get_running_loop()
            calibration_id = f"{now:%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:8]}"
            self.data_dir.mkdir(parents=True, exist_ok=True)
            csv_path = self.data_dir / f"{calibration_id}-calibration.csv"
            with csv_path.open("x", newline="", encoding="utf-8") as handle:
                csv.DictWriter(handle, fieldnames=CALIBRATION_FIELDS).writeheader()

            session = CalibrationSession(
                calibration_id=calibration_id,
                started_at=now,
                start_monotonic=loop.time(),
                csv_path=csv_path,
            )
            self._record_sample(session, initial_data, 0.0, now)
            self._active = session
            session.task = asyncio.create_task(
                self._run(session, on_complete),
                name=f"calibration-{calibration_id}",
            )
            return session

    def status(self) -> dict[str, object] | None:
        session = self._active
        if session is None:
            return None
        elapsed = max(
            asyncio.get_running_loop().time() - session.start_monotonic, 0.0
        )
        return {
            "calibration_id": session.calibration_id,
            "elapsed_seconds": elapsed,
            "remaining_seconds": max(self.duration - elapsed, 0.0),
            "sample_count": len(session.samples),
            "latest": session.samples[-1] if session.samples else None,
        }

    async def shutdown(self) -> None:
        async with self._lock:
            session = self._active
            self._active = None
        if session is not None and session.task is not None:
            session.task.cancel()
            try:
                await session.task
            except asyncio.CancelledError:
                pass
            LOGGER.info("Cancelled calibration %s during shutdown", session.calibration_id)

    async def _run(
        self, session: CalibrationSession, on_complete: CompletionCallback
    ) -> None:
        consecutive_failures = 0
        error: str | None = None
        calibration: Calibration | None = None
        try:
            loop = asyncio.get_running_loop()
            deadline = session.start_monotonic + self.duration
            while loop.time() < deadline:
                await asyncio.sleep(min(self.sample_interval, deadline - loop.time()))
                try:
                    data = await asyncio.to_thread(self._reader)
                    elapsed = loop.time() - session.start_monotonic
                    self._record_sample(
                        session, data, elapsed, datetime.now(timezone.utc)
                    )
                    if consecutive_failures:
                        LOGGER.info("NUT calibration sampling recovered")
                    consecutive_failures = 0
                except asyncio.CancelledError:
                    raise
                except Exception:
                    consecutive_failures += 1
                    LOGGER.exception("Calibration sample failed; continuing")
                    if consecutive_failures >= self.failure_warning_threshold:
                        LOGGER.warning(
                            "Calibration sampling has failed %d consecutive times",
                            consecutive_failures,
                        )

            expected_intervals = max(int(self.duration / self.sample_interval), 1)
            minimum_samples = max(2, expected_intervals // 2)
            if len(session.samples) < minimum_samples:
                raise RuntimeError(
                    f"Only {len(session.samples)} valid samples were collected; "
                    f"at least {minimum_samples} are required"
                )
            covered_seconds = float(session.samples[-1]["elapsed_seconds"])
            if covered_seconds < self.duration * 0.8:
                raise RuntimeError(
                    "Valid calibration samples did not cover enough of the requested duration"
                )
            calibration = calibration_statistics(
                session.samples,
                session.calibration_id,
                datetime.now(timezone.utc),
                self.ups_watts,
                self.sample_interval,
            )
            await asyncio.to_thread(self.store.save, calibration)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            LOGGER.exception("Calibration failed")
            error = str(exc)
        finally:
            async with self._lock:
                if self._active is session:
                    self._active = None

        try:
            await on_complete(calibration, error)
        except Exception:
            LOGGER.exception("Could not send calibration completion notification")

    def _record_sample(
        self,
        session: CalibrationSession,
        data: dict[str, str],
        elapsed: float,
        timestamp: datetime,
    ) -> None:
        load = optional_float(data, "ups.load")
        if load is None:
            raise ValueError("NUT sample has no valid ups.load")
        sample: dict[str, object] = {
            "calibration_id": session.calibration_id,
            "timestamp": timestamp.isoformat(),
            "elapsed_seconds": round(elapsed, 3),
            "ups.load": load,
            "total_watts": load_to_watts(load, self.ups_watts),
            "ups.status": data.get("ups.status", ""),
        }
        session.samples.append(sample)
        try:
            with session.csv_path.open("a", newline="", encoding="utf-8") as handle:
                csv.DictWriter(handle, fieldnames=CALIBRATION_FIELDS).writerow(sample)
        except OSError:
            LOGGER.exception("Could not append calibration sample to %s", session.csv_path)
