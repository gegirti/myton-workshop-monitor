"""Telegram entry point for the MYTON-3000 workshop monitor."""

from __future__ import annotations

import asyncio
import html
import logging
import signal

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from calibration import CalibrationManager, CalibrationStore
from config import Config
from plotting import generate_plot
from power_logger import (
    PowerSessionManager,
    SessionAlreadyActiveError,
    SessionResult,
    format_duration,
)
from telegram_calibration import baseline_command as handle_baseline_command
from ups import fmt_runtime, load_to_watts, read_ups, status_text

LOGGER = logging.getLogger(__name__)


def load_text(data: dict[str, str], ups_watts: float) -> str:
    raw_load = data.get("ups.load", "?")
    try:
        watts = round(load_to_watts(float(raw_load), ups_watts))
        return f"{html.escape(raw_load)}% (~{watts} W)"
    except (ValueError, TypeError):
        return f"{html.escape(raw_load)}%"


def ups_message(data: dict[str, str], ups_watts: float) -> str:
    value = lambda key: html.escape(data.get(key, "?"))
    return (
        "<b>MYTON-3000 UPS</b>\n\n"
        f"<b>Status:</b> {html.escape(status_text(data.get('ups.status', '?')))}\n"
        f"<b>Load:</b> {load_text(data, ups_watts)}\n\n"
        "<b>Battery</b>\n"
        f"Charge: {value('battery.charge')}%\n"
        f"Runtime: {fmt_runtime(data.get('battery.runtime'))}\n"
        f"Voltage: {value('battery.voltage')} V\n"
        f"Nominal: {value('battery.voltage.nominal')} V\n"
        f"Type: {value('battery.type')}\n\n"
        "<b>Output</b>\n"
        f"Voltage: {value('output.voltage')} V\n"
        f"Frequency: {value('output.frequency')} Hz\n\n"
        "<b>Device</b>\n"
        f"{value('ups.mfr')} {value('ups.model')}"
    )


def _number(value: object, suffix: str = "", decimals: int = 0) -> str:
    if value is None:
        return "Unknown"
    try:
        return f"{float(value):.{decimals}f}{suffix}"
    except (TypeError, ValueError):
        return "Unknown"


def session_summary(result: SessionResult) -> str:
    stats = result.statistics
    return (
        "POWER TEST COMPLETE\n\n"
        f"{result.session.name}\n"
        f"Session ID: {result.session.session_id}\n"
        f"Start: {result.session.started_at.astimezone():%Y-%m-%d %H:%M:%S %Z}\n"
        f"Finish: {result.finished_at.astimezone():%Y-%m-%d %H:%M:%S %Z}\n"
        f"Duration: {format_duration(float(stats['duration_seconds'] or 0))}\n"
        f"Samples: {stats['sample_count']}\n\n"
        "UPS total\n"
        f"Average: {_number(stats['avg_total_watts'], ' W')}\n"
        f"Minimum: {_number(stats['min_total_watts'], ' W')}\n"
        f"Maximum: {_number(stats['max_total_watts'], ' W')}\n"
        f"Average load: {_number(stats['avg_load_percent'], '%', 1)}\n"
        f"Energy: {_number(stats['total_energy_wh'], ' Wh', 1)}\n\n"
        "Printer contribution\n"
        f"Baseline: {result.baseline_watts:.1f} W"
        f" ± {result.baseline_uncertainty_watts:.1f} W\n"
        f"Calibration: {result.baseline_source}\n"
        f"Average: {_number(stats['avg_printer_watts'], ' W')}\n"
        f"Minimum: {_number(stats['min_printer_watts'], ' W')}\n"
        f"Maximum: {_number(stats['max_printer_watts'], ' W')}\n"
        f"Energy: {_number(stats['printer_energy_wh'], ' Wh', 1)}\n\n"
        "Battery\n"
        f"Start: {_number(stats['starting_battery_charge'], '%')}\n"
        f"End: {_number(stats['ending_battery_charge'], '%')}\n"
        f"Start voltage: {_number(stats['starting_battery_voltage'], ' V', 1)}\n"
        f"End voltage: {_number(stats['ending_battery_voltage'], ' V', 1)}"
    )


class WorkshopBot:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.reader = lambda: read_ups(config.ups_name, config.nut_timeout)
        self.calibration_store = CalibrationStore(config.data_dir / "baseline.json")
        saved_calibration = self.calibration_store.load(config.ups_watts)
        baseline_watts = (
            saved_calibration.baseline_watts
            if saved_calibration is not None
            else config.baseline_watts
        )
        baseline_uncertainty = (
            saved_calibration.uncertainty_watts
            if saved_calibration is not None
            else 0.0
        )
        baseline_source = (
            f"calibration {saved_calibration.calibration_id}"
            if saved_calibration is not None
            else "configured fallback"
        )
        self.manager = PowerSessionManager(
            reader=self.reader,
            data_dir=config.data_dir,
            ups_watts=config.ups_watts,
            baseline_watts=baseline_watts,
            sample_interval=config.power_sample_interval,
            baseline_uncertainty_watts=baseline_uncertainty,
            baseline_source=baseline_source,
            failure_warning_threshold=config.nut_failure_warning_threshold,
        )
        self.calibration = CalibrationManager(
            reader=self.reader,
            store=self.calibration_store,
            data_dir=config.data_dir,
            ups_watts=config.ups_watts,
            sample_interval=config.calibration_sample_interval,
            duration=config.calibration_duration,
            failure_warning_threshold=config.nut_failure_warning_threshold,
        )

    async def ups_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        del context
        if update.effective_message is None:
            return
        try:
            data = await asyncio.to_thread(
                read_ups, self.config.ups_name, self.config.nut_timeout
            )
            await update.effective_message.reply_text(
                ups_message(data, self.config.ups_watts), parse_mode="HTML"
            )
        except Exception as exc:
            LOGGER.exception("UPS command failed")
            await update.effective_message.reply_text(f"UPS error:\n{exc}")

    async def baseline_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        await handle_baseline_command(self, update, context)

    async def power_start_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if update.effective_message is None:
            return
        name = " ".join(context.args).replace("_", " ").strip()
        if self.calibration.active is not None:
            await update.effective_message.reply_text(
                "A baseline calibration is active. Wait for it to finish before starting a power test."
            )
            return
        if not name:
            await update.effective_message.reply_text("Usage: /powerstart <name>")
            return
        try:
            session = await self.manager.start(name)
        except SessionAlreadyActiveError as exc:
            await update.effective_message.reply_text(str(exc))
            return
        except Exception as exc:
            LOGGER.exception("Could not start power session")
            await update.effective_message.reply_text(f"Could not start power test: {exc}")
            return

        await update.effective_message.reply_text(
            "POWER TEST STARTED\n\n"
            f"{session.name}\n"
            f"Session ID: {session.session_id}\n"
            f"UPS state: {status_text(session.starting_ups_state) or 'Unknown'}\n"
            f"Battery: {_number(session.starting_battery_charge, '%')}\n"
            f"Battery voltage: {_number(session.starting_battery_voltage, ' V', 1)}\n"
            f"Baseline: {self.manager.baseline_watts:.1f} W"
            f" ± {self.manager.baseline_uncertainty_watts:.1f} W\n"
            f"Sampling every {self.config.power_sample_interval:g} seconds."
        )

    async def power_status_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        del context
        if update.effective_message is None:
            return
        status = self.manager.status()
        if status is None:
            await update.effective_message.reply_text("No power test is active.")
            return
        latest = status["latest"]
        if not isinstance(latest, dict):
            await update.effective_message.reply_text(
                f"Power test '{status['name']}' is active but has no samples yet."
            )
            return
        await update.effective_message.reply_text(
            "POWER TEST ACTIVE\n\n"
            f"{status['name']}\n"
            f"Session ID: {status['session_id']}\n"
            f"Elapsed: {format_duration(float(status['elapsed_seconds']))}\n"
            f"Load: {_number(latest.get('ups.load'), '%')}\n"
            f"UPS total: {_number(latest.get('total_watts'), ' W')}\n"
            f"Printer estimate: {_number(latest.get('printer_watts'), ' W')}\n"
            f"Battery: {_number(latest.get('battery.charge'), '%')}\n"
            f"Runtime: {fmt_runtime(latest.get('battery.runtime'))}\n"
            f"Samples: {status['sample_count']}"
        )

    async def power_stop_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        del context
        if update.effective_message is None:
            return
        try:
            result = await self.manager.stop()
        except Exception as exc:
            LOGGER.exception("Could not stop power session")
            await update.effective_message.reply_text(f"Could not stop power test: {exc}")
            return
        if result is None:
            await update.effective_message.reply_text("No power test is active.")
            return

        await update.effective_message.reply_text(session_summary(result))
        try:
            plot_path = await asyncio.to_thread(
                generate_plot, result, result.baseline_watts
            )
            with plot_path.open("rb") as plot:
                await update.effective_message.reply_photo(
                    photo=plot, caption=f"Power graph · {result.session.name}"
                )
        except Exception:
            LOGGER.exception("Could not generate or send power plot")
            try:
                await update.effective_message.reply_text(
                    "The test data was saved, but the graph could not be sent."
                )
            except Exception:
                LOGGER.exception("Could not report plot failure to Telegram")


async def monitor_ups(app: Application, bot: WorkshopBot) -> None:
    last_status: str | None = None
    outage_started: float | None = None
    consecutive_failures = 0
    loop = asyncio.get_running_loop()

    while True:
        try:
            data = await asyncio.to_thread(
                read_ups, bot.config.ups_name, bot.config.nut_timeout
            )
            status = data.get("ups.status", "")
            previous = set(last_status.split()) if last_status is not None else set()
            current = set(status.split())
            notifications: list[str] = []

            if last_status is not None and "OB" in current and "OB" not in previous:
                outage_started = loop.time()
                notifications.append(
                    "<b>POWER FAILURE</b>\n\nUPS switched to battery.\n\n"
                    f"<b>Status:</b> {html.escape(status_text(status))}\n"
                    f"<b>Load:</b> {load_text(data, bot.config.ups_watts)}\n"
                    f"<b>Battery:</b> {html.escape(data.get('battery.charge', '?'))}%\n"
                    f"<b>Runtime:</b> {fmt_runtime(data.get('battery.runtime'))}\n"
                    f"<b>Battery voltage:</b> {html.escape(data.get('battery.voltage', '?'))} V"
                )
            if last_status is not None and "OL" in current and "OB" in previous:
                duration = (
                    format_duration(loop.time() - outage_started)
                    if outage_started is not None
                    else "Unknown"
                )
                notifications.append(
                    "<b>POWER RESTORED</b>\n\nUPS is back on mains power.\n\n"
                    f"<b>Outage duration:</b> {duration}\n"
                    f"<b>Status:</b> {html.escape(status_text(status))}\n"
                    f"<b>Load:</b> {load_text(data, bot.config.ups_watts)}\n"
                    f"<b>Battery:</b> {html.escape(data.get('battery.charge', '?'))}%"
                )
                outage_started = None
            if last_status is not None and "LB" in current and "LB" not in previous:
                notifications.append(
                    "<b>UPS LOW BATTERY</b>\n\n"
                    f"<b>Battery:</b> {html.escape(data.get('battery.charge', '?'))}%\n"
                    f"<b>Runtime:</b> {fmt_runtime(data.get('battery.runtime'))}\n"
                    f"<b>Load:</b> {load_text(data, bot.config.ups_watts)}"
                )
            if last_status is not None and "OVER" in current and "OVER" not in previous:
                notifications.append(
                    "<b>UPS OVERLOAD</b>\n\n"
                    f"<b>Load:</b> {load_text(data, bot.config.ups_watts)}"
                )

            all_sent = True
            for message in notifications:
                try:
                    await app.bot.send_message(
                        chat_id=bot.config.telegram_chat_id,
                        text=message,
                        parse_mode="HTML",
                    )
                except Exception:
                    all_sent = False
                    LOGGER.exception("Could not send UPS state notification")
            if all_sent:
                last_status = status
            if consecutive_failures:
                LOGGER.info("NUT outage monitoring recovered")
            consecutive_failures = 0
        except asyncio.CancelledError:
            raise
        except Exception:
            consecutive_failures += 1
            LOGGER.exception("UPS monitoring cycle failed")
            if consecutive_failures >= bot.config.nut_failure_warning_threshold:
                LOGGER.warning(
                    "NUT outage monitoring has failed %d consecutive times",
                    consecutive_failures,
                )
        await asyncio.sleep(bot.config.monitor_interval)


async def telegram_error_handler(
    update: object, context: ContextTypes.DEFAULT_TYPE
) -> None:
    del update
    LOGGER.error("Telegram update failed", exc_info=context.error)


async def main() -> None:
    config = Config.from_env()
    config.data_dir.mkdir(parents=True, exist_ok=True)
    bot = WorkshopBot(config)
    app = Application.builder().token(config.telegram_bot_token).build()
    app.add_handler(CommandHandler("ups", bot.ups_command))
    app.add_handler(CommandHandler("powerstart", bot.power_start_command))
    app.add_handler(CommandHandler("powerstop", bot.power_stop_command))
    app.add_handler(CommandHandler("powerstatus", bot.power_status_command))
    app.add_handler(CommandHandler("baseline", bot.baseline_command))
    app.add_handler(CommandHandler("calibrate", bot.baseline_command))
    app.add_error_handler(telegram_error_handler)

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, stop_event.set)

    monitor_task: asyncio.Task[None] | None = None
    try:
        await app.initialize()
        await app.start()
        if app.updater is None:
            raise RuntimeError("Telegram updater is unavailable")
        await app.updater.start_polling()
        monitor_task = asyncio.create_task(
            monitor_ups(app, bot), name="ups-outage-monitor"
        )
        LOGGER.info("Workshop monitor started")
        await stop_event.wait()
    finally:
        await bot.calibration.shutdown()
        await bot.manager.shutdown()
        if monitor_task is not None:
            monitor_task.cancel()
            try:
                await monitor_task
            except asyncio.CancelledError:
                pass
        if app.updater is not None and app.updater.running:
            await app.updater.stop()
        if app.running:
            await app.stop()
        await app.shutdown()
        LOGGER.info("Workshop monitor stopped")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
