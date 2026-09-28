import sys
import tempfile
import types
import unittest
from pathlib import Path

try:
    import telegram  # noqa: F401
except ModuleNotFoundError:
    telegram_module = types.ModuleType("telegram")
    telegram_module.Update = object
    telegram_ext_module = types.ModuleType("telegram.ext")
    telegram_ext_module.Application = object
    telegram_ext_module.CommandHandler = object
    telegram_ext_module.ContextTypes = types.SimpleNamespace(DEFAULT_TYPE=object)
    sys.modules["telegram"] = telegram_module
    sys.modules["telegram.ext"] = telegram_ext_module

from config import Config
from monitor import WorkshopBot
from power_controller import PowerController
from settings import SettingsStore


class MonitorStartupTests(unittest.TestCase):
    def test_workshop_bot_constructs_with_all_runtime_components(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = Config(
                telegram_bot_token="test-token",
                telegram_chat_id=123,
                ups_name="myton@localhost",
                ups_watts=2700,
                baseline_watts=0,
                power_sample_interval=5,
                calibration_duration=300,
                calibration_sample_interval=5,
                auto_stop_idle_duration=300,
                auto_stop_idle_uncertainty_multiplier=3,
                auto_stop_activity_uncertainty_multiplier=6,
                auto_stop_activity_confirm_samples=3,
                monitor_interval=5,
                nut_timeout=5,
                data_dir=Path(temporary),
                nut_failure_warning_threshold=3,
            )

            bot = WorkshopBot(config)

            self.assertIsInstance(bot.settings_store, SettingsStore)
            self.assertIsInstance(bot.power, PowerController)
            self.assertIs(bot.power.manager, bot.manager)


if __name__ == "__main__":
    unittest.main()
