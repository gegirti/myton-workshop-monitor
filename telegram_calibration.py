"""Telegram command flow for background-load calibration."""

from __future__ import annotations

import logging
from typing import Any

from telegram import Update
from telegram.ext import ContextTypes

from calibration import Calibration, CalibrationActiveError
from power_logger import format_duration

LOGGER = logging.getLogger(__name__)


def _watts(value: object) -> str:
    try:
        return f"{float(value):.0f} W"
    except (TypeError, ValueError):
        return "Unknown"


async def baseline_command(
    workshop: Any,
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Start calibration, or report the active/saved calibration."""
    if update.effective_message is None or update.effective_chat is None:
        return

    status = workshop.calibration.status()
    requested_status = bool(context.args) and context.args[0].lower() in {
        "show",
        "status",
    }
    if status is not None:
        latest = status.get("latest")
        latest_watts = latest.get("total_watts") if isinstance(latest, dict) else None
        await update.effective_message.reply_text(
            "BASELINE CALIBRATION ACTIVE\n\n"
            f"ID: {status['calibration_id']}\n"
            f"Elapsed: {format_duration(float(status['elapsed_seconds']))}\n"
            f"Remaining: {format_duration(float(status['remaining_seconds']))}\n"
            f"Latest: {_watts(latest_watts)}\n"
            f"Samples: {status['sample_count']}"
        )
        return

    if requested_status:
        await update.effective_message.reply_text(
            "BASELINE CALIBRATION\n\n"
            f"Current: {workshop.manager.baseline_watts:.1f} W"
            f" ± {workshop.manager.baseline_uncertainty_watts:.1f} W\n"
            f"Source: {workshop.manager.baseline_source}\n\n"
            "Send /baseline to run a new five-minute calibration."
        )
        return

    if workshop.manager.active is not None:
        await update.effective_message.reply_text(
            "A power test is active. Stop it before starting calibration."
        )
        return

    chat_id = update.effective_chat.id

    async def on_complete(
        calibration: Calibration | None, error: str | None
    ) -> None:
        if calibration is None:
            await context.bot.send_message(
                chat_id=chat_id,
                text=(
                    "BASELINE CALIBRATION FAILED\n\n"
                    f"{error or 'No valid calibration was produced.'}\n"
                    "The previous baseline is still active."
                ),
            )
            return

        workshop.manager.set_baseline(
            calibration.baseline_watts,
            calibration.uncertainty_watts,
            f"calibration {calibration.calibration_id}",
        )
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                "BASELINE CALIBRATION COMPLETE\n\n"
                f"ID: {calibration.calibration_id}\n"
                f"Duration: {format_duration(calibration.duration_seconds)}\n"
                f"Samples: {calibration.sample_count}\n"
                f"Average: {calibration.baseline_watts:.1f} W\n"
                f"Uncertainty: ±{calibration.uncertainty_watts:.1f} W\n"
                f"Observed variation: ±{calibration.observed_stddev_watts:.1f} W\n"
                f"Minimum: {calibration.min_watts:.0f} W\n"
                f"Maximum: {calibration.max_watts:.0f} W\n\n"
                "Saved globally. Future power tests will use this calibration."
            ),
        )

    try:
        session = await workshop.calibration.start(on_complete)
    except CalibrationActiveError as exc:
        await update.effective_message.reply_text(str(exc))
        return
    except Exception as exc:
        LOGGER.exception("Could not start calibration")
        await update.effective_message.reply_text(
            f"Could not start baseline calibration: {exc}"
        )
        return

    await update.effective_message.reply_text(
        "BASELINE CALIBRATION STARTED\n\n"
        "Keep printers and other measured equipment off.\n"
        f"Duration: {format_duration(workshop.config.calibration_duration)}\n"
        f"Sampling every {workshop.config.calibration_sample_interval:g} seconds.\n"
        f"ID: {session.calibration_id}\n\n"
        "Send /baseline again to see progress."
    )
