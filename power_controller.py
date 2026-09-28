"""Timed and calibration-aware orchestration around power sessions."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from power_logger import PowerSession, PowerSessionManager, SessionResult
from settings import ElectricityPrice

LOGGER = logging.getLogger(__name__)
DURATION_RE = re.compile(
    r"^(?:(?P<hours>\d+(?:\.\d+)?)h)?(?:(?P<minutes>\d+(?:\.\d+)?)m)?$",
    re.IGNORECASE,
)


def parse_duration(value: str) -> float:
    """Parse durations such as 4h, 90m, or 2h30m."""
    match = DURATION_RE.fullmatch(value.strip())
    if match is None or not any(match.groupdict().values()):
        raise ValueError("Duration must look like 4h, 90m, or 2h30m")
    hours = float(match.group("hours") or 0)
    minutes = float(match.group("minutes") or 0)
    seconds = hours * 3600 + minutes * 60
    if seconds <= 0:
        raise ValueError("Duration must be greater than zero")
    if seconds > 30 * 24 * 3600:
        raise ValueError("Duration cannot exceed 30 days")
    return seconds


def parse_powerstart_args(args: list[str]) -> tuple[str, float | None]:
    if not args:
        raise ValueError("Usage: /powerstart <name> [duration]")
    duration: float | None = None
    name_parts = args
    try:
        duration = parse_duration(args[-1])
        name_parts = args[:-1]
    except ValueError:
        pass
    name = " ".join(name_parts).replace("_", " ").strip()
    if not name:
        raise ValueError("A test name is required before the duration")
    return name, duration


@dataclass
class AutomationState:
    session: PowerSession
    started_monotonic: float
    max_duration_seconds: float | None
    idle_duration_seconds: float
    idle_threshold_watts: float | None
    activity_threshold_watts: float | None
    activity_confirm_samples: int
    electricity_price: ElectricityPrice | None
    activity_streak: int = 0
    activity_detected: bool = False
    idle_since_elapsed: float | None = None
    last_sample_count: int = 0
    task: asyncio.Task[None] | None = None


@dataclass(frozen=True)
class CompletedPowerTest:
    result: SessionResult
    stop_reason: str
    max_duration_seconds: float | None
    idle_duration_seconds: float
    idle_threshold_watts: float | None
    activity_threshold_watts: float | None
    activity_detected: bool
    electricity_price: ElectricityPrice | None
    metadata_path: Path


CompletionCallback = Callable[[CompletedPowerTest], Awaitable[None]]


class PowerController:
    def __init__(
        self,
        manager: PowerSessionManager,
        idle_duration_seconds: float,
        idle_uncertainty_multiplier: float,
        activity_uncertainty_multiplier: float,
        activity_confirm_samples: int,
    ) -> None:
        self.manager = manager
        self.idle_duration_seconds = idle_duration_seconds
        self.idle_uncertainty_multiplier = idle_uncertainty_multiplier
        self.activity_uncertainty_multiplier = activity_uncertainty_multiplier
        self.activity_confirm_samples = activity_confirm_samples
        self._active: AutomationState | None = None
        self._lock = asyncio.Lock()

    @property
    def active(self) -> AutomationState | None:
        return self._active

    async def start(
        self,
        name: str,
        max_duration_seconds: float | None,
        electricity_price: ElectricityPrice | None,
        on_automatic_complete: CompletionCallback,
    ) -> AutomationState:
        async with self._lock:
            session = await self.manager.start(name)
            uncertainty = self.manager.baseline_uncertainty_watts
            smart_enabled = max_duration_seconds is not None and uncertainty > 0
            state = AutomationState(
                session=session,
                started_monotonic=asyncio.get_running_loop().time(),
                max_duration_seconds=max_duration_seconds,
                idle_duration_seconds=self.idle_duration_seconds,
                idle_threshold_watts=(
                    uncertainty * self.idle_uncertainty_multiplier
                    if smart_enabled
                    else None
                ),
                activity_threshold_watts=(
                    uncertainty * self.activity_uncertainty_multiplier
                    if smart_enabled
                    else None
                ),
                activity_confirm_samples=self.activity_confirm_samples,
                electricity_price=electricity_price,
            )
            self._active = state
            if max_duration_seconds is not None:
                state.task = asyncio.create_task(
                    self._watch(state, on_automatic_complete),
                    name=f"power-automation-{session.session_id}",
                )
            return state

    def status(self) -> dict[str, object] | None:
        state = self._active
        manager_status = self.manager.status()
        if state is None or manager_status is None:
            return None
        elapsed = float(manager_status["elapsed_seconds"])
        idle_elapsed = (
            max(elapsed - state.idle_since_elapsed, 0.0)
            if state.idle_since_elapsed is not None
            else None
        )
        return {
            **manager_status,
            "max_duration_seconds": state.max_duration_seconds,
            "remaining_seconds": (
                max(state.max_duration_seconds - elapsed, 0.0)
                if state.max_duration_seconds is not None
                else None
            ),
            "smart_stop_enabled": state.idle_threshold_watts is not None,
            "activity_detected": state.activity_detected,
            "idle_threshold_watts": state.idle_threshold_watts,
            "activity_threshold_watts": state.activity_threshold_watts,
            "idle_elapsed_seconds": idle_elapsed,
            "idle_remaining_seconds": (
                max(state.idle_duration_seconds - idle_elapsed, 0.0)
                if idle_elapsed is not None
                else None
            ),
        }

    async def stop(self, reason: str = "manual") -> CompletedPowerTest | None:
        return await self._finish(self._active, reason)

    async def shutdown(self) -> None:
        state = self._active
        if state is None:
            await self.manager.shutdown()
            return
        current = asyncio.current_task()
        if state.task is not None and state.task is not current:
            state.task.cancel()
            try:
                await state.task
            except asyncio.CancelledError:
                pass
        self._active = None
        await self.manager.shutdown()

    async def _watch(
        self, state: AutomationState, on_complete: CompletionCallback
    ) -> None:
        try:
            while self._active is state:
                await asyncio.sleep(min(self.manager.sample_interval, 1.0))
                status = self.manager.status()
                if status is None:
                    return
                elapsed = float(status["elapsed_seconds"])
                if (
                    state.max_duration_seconds is not None
                    and elapsed >= state.max_duration_seconds
                ):
                    completed = await self._finish(state, "maximum timer reached")
                    if completed is not None:
                        await on_complete(completed)
                    return

                sample_count = int(status["sample_count"])
                if sample_count == state.last_sample_count:
                    continue
                state.last_sample_count = sample_count
                latest = status.get("latest")
                if not isinstance(latest, dict):
                    continue
                self._observe_sample(state, latest)
                if state.idle_since_elapsed is not None:
                    sample_elapsed = float(latest["elapsed_seconds"])
                    if sample_elapsed - state.idle_since_elapsed >= state.idle_duration_seconds:
                        completed = await self._finish(
                            state, "calibrated idle detected"
                        )
                        if completed is not None:
                            await on_complete(completed)
                        return
        except asyncio.CancelledError:
            raise
        except Exception:
            LOGGER.exception("Power automation failed; manual measurement remains available")

    def _observe_sample(
        self, state: AutomationState, sample: dict[str, object]
    ) -> None:
        if state.idle_threshold_watts is None or state.activity_threshold_watts is None:
            return
        printer_watts = float(sample["printer_watts"])
        elapsed = float(sample["elapsed_seconds"])
        if not state.activity_detected:
            if printer_watts > state.activity_threshold_watts:
                state.activity_streak += 1
                if state.activity_streak >= state.activity_confirm_samples:
                    state.activity_detected = True
            else:
                state.activity_streak = 0
            return

        if printer_watts <= state.idle_threshold_watts:
            if state.idle_since_elapsed is None:
                state.idle_since_elapsed = elapsed
        else:
            state.idle_since_elapsed = None

    async def _finish(
        self, state: AutomationState | None, reason: str
    ) -> CompletedPowerTest | None:
        if state is None:
            return None
        async with self._lock:
            if self._active is not state:
                return None
            self._active = None
            current = asyncio.current_task()
            if state.task is not None and state.task is not current:
                state.task.cancel()
                try:
                    await state.task
                except asyncio.CancelledError:
                    pass
            result = await self.manager.stop()
            if result is None:
                return None
            metadata_path = result.session.csv_path.with_suffix(".json")
            completed = CompletedPowerTest(
                result=result,
                stop_reason=reason,
                max_duration_seconds=state.max_duration_seconds,
                idle_duration_seconds=state.idle_duration_seconds,
                idle_threshold_watts=state.idle_threshold_watts,
                activity_threshold_watts=state.activity_threshold_watts,
                activity_detected=state.activity_detected,
                electricity_price=state.electricity_price,
                metadata_path=metadata_path,
            )
            await asyncio.to_thread(self._write_metadata, completed)
            return completed

    @staticmethod
    def _write_metadata(completed: CompletedPowerTest) -> None:
        price = completed.electricity_price
        values = {
            "session_id": completed.result.session.session_id,
            "stop_reason": completed.stop_reason,
            "max_duration_seconds": completed.max_duration_seconds,
            "idle_duration_seconds": completed.idle_duration_seconds,
            "idle_threshold_watts": completed.idle_threshold_watts,
            "activity_threshold_watts": completed.activity_threshold_watts,
            "activity_detected": completed.activity_detected,
            "baseline_watts": completed.result.baseline_watts,
            "baseline_uncertainty_watts": completed.result.baseline_uncertainty_watts,
            "electricity_price_per_kwh": price.amount_per_kwh if price else None,
            "currency": price.currency if price else None,
        }
        temporary = completed.metadata_path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(values, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        temporary.replace(completed.metadata_path)
