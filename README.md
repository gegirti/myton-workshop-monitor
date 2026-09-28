# MYTON-3000 Workshop Power Monitor

A lightweight Raspberry Pi service that reads a MYTON-3000 UPS through Network UPS Tools (NUT), reports UPS state through Telegram, sends automatic mains-failure/recovery, low-battery, and overload alerts, and records named power-measurement sessions for workshop equipment such as 3D printers.

## Requirements

- Raspberry Pi OS or another Debian-based Linux distribution
- Python 3.10 or newer
- NUT configured with `usbhid-ups`
- A working `upsc myton@localhost` command
- A Telegram bot token and destination chat ID

The known UPS USB identity is PHOENIXTEC InnovaBasicG2, VID `06DA`, PID `FFFF`. Verify NUT independently before starting the monitor:

```bash
upsc myton@localhost
```

## Installation

```bash
sudo apt update
sudo apt install -y network-ups-tools python3 python3-venv
cd /opt/workshop-monitor
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
```

Matplotlib is preferred for completed-test graphs. If it is unavailable or fails at runtime, a local Pillow fallback creates the graph using Debian's DejaVu fonts. Both paths work headlessly and do not depend on an external chart service. No database or web framework is required.

## Configuration

Configuration is read directly from environment variables; the application deliberately does not load `.env` files and therefore needs no dotenv dependency. Copy `.env.example` to a protected location outside the repository:

```bash
sudo install -m 600 .env.example /etc/workshop-monitor.env
sudo editor /etc/workshop-monitor.env
```

Set the real `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in that file. Never commit either value.

| Variable | Default | Purpose |
| --- | ---: | --- |
| `TELEGRAM_BOT_TOKEN` | required | Telegram bot token |
| `TELEGRAM_CHAT_ID` | required | Chat receiving automatic UPS alerts |
| `UPS_NAME` | `myton@localhost` | NUT UPS identifier |
| `UPS_WATTS` | `2700` | Rated real output power |
| `BASELINE_WATTS` | `0` | Uncalibrated fallback; run `/baseline` before measuring |
| `POWER_SAMPLE_INTERVAL` | `5` | Active-session sampling seconds |
| `CALIBRATION_DURATION` | `300` | Baseline calibration duration in seconds |
| `CALIBRATION_SAMPLE_INTERVAL` | `5` | Calibration sampling seconds |
| `AUTO_STOP_IDLE_DURATION` | `300` | Continuous calibrated-idle seconds before early stop |
| `AUTO_STOP_IDLE_UNCERTAINTY_MULTIPLIER` | `3` | Idle band as a multiple of calibration uncertainty |
| `AUTO_STOP_ACTIVITY_UNCERTAINTY_MULTIPLIER` | `6` | Activity threshold as a multiple of calibration uncertainty |
| `AUTO_STOP_ACTIVITY_CONFIRM_SAMPLES` | `3` | Consecutive active samples needed to arm early stop |
| `UPS_MONITOR_INTERVAL` | `5` | Outage-monitor polling seconds (existing behavior) |
| `NUT_TIMEOUT` | `5` | Timeout for each `upsc` call |
| `NUT_FAILURE_WARNING_THRESHOLD` | `3` | Consecutive failures before explicit warning |
| `DATA_DIR` | `data` | CSV and PNG output directory |

Relative `DATA_DIR` paths are relative to the service `WorkingDirectory`.

## Telegram commands

- `/help` — list commands and timer syntax
- `/ups` — current UPS details (existing behavior)
- `/baseline` or `/calibrate` — start calibration; call again for progress
- `/baseline status` — show the active or currently saved calibration
- `/price` — show the saved electricity price
- `/price <amount> <currency>` — save a price per kWh, for example `/price 2.75 TRY`
- `/price clear` — remove the saved price
- `/powerstart <name> [duration]` — start a test, optionally with `4h`, `90m`, or `2h30m`
- `/powerstatus` — latest values, samples, timer, and smart-finish state
- `/powerstop` — stop, calculate statistics/energy, and send a PNG graph

Example workflow:

1. Put the system in its normal background state, with measured equipment off.
2. Send `/baseline` and keep that state unchanged for five minutes.
3. Wait for completion, then send `/price 2.75 TRY` if cost reporting is wanted.
4. Send `/powerstart Creality_Hi_Benchy` for a manual test, or `/powerstart Creality_Hi_Benchy 4h` for a smart timed test.
5. Run the printer or other measured equipment.
6. Send `/powerstatus` at any time without interrupting collection.
7. Send `/powerstop` for a manual test; a timed test can also finish itself. The calibrated summary, graph, and configured cost estimate are returned to Telegram.

Calibration is local to each installation. It measures the complete background load rather than assuming every Raspberry Pi, modem, and workshop has the same consumption. Valid samples are time-weighted, and the completed calibration is saved atomically to `data/baseline.json`; its raw samples remain in a timestamped calibration CSV. A new successful run replaces the saved value globally for future measurements. A failed or interrupted run leaves the previous value untouched.

The reported uncertainty combines observed baseline variation with the UPS load reading's quantization uncertainty. It is shown as a shaded band around the printer estimate. It describes baseline and measurement resolution, not laboratory-grade instrument accuracy or a confidence interval based on independent samples.

A calibration and power test cannot run simultaneously. Each power-session CSV records the exact calibration value, uncertainty, and source, so later recalibration does not make old results ambiguous.

A timed test uses its duration as a hard maximum. Smart early stop is enabled only when a saved calibration has non-zero uncertainty. It first requires the configured number of consecutive readings above the activity threshold (by default, six times calibration uncertainty). Once armed, it finishes only after the printer contribution remains at or below the idle threshold (by default, three times uncertainty) continuously for five minutes. The thresholds are relative to the active calibrated baseline—not a fixed watt value. If calibration uncertainty is unavailable, the hard timer still works and smart early stop stays disabled.

Only one measurement can run at a time. The initial sample records UPS state, battery charge, and battery voltage. Later samples are appended immediately to a unique UTF-8 CSV in `data/`; generated graphs and per-run timer/price metadata JSON are stored beside their CSV. The current baseline is in `data/baseline.json`, and `/price` persists to `data/settings.json`. These generated files are ignored by Git while `data/.gitkeep` retains the directory. The electricity price is snapshotted when a run starts, so changing it mid-run does not rewrite that run’s result.

Energy is calculated with trapezoidal integration over actual sample timestamps. A failed NUT sample is logged and skipped; the next scheduled sample is still attempted. Telegram failures are logged without stopping local sampling or outage monitoring.

## systemd

An example unit is provided. Adjust `User`, `WorkingDirectory`, and `ExecStart` if the repository is installed elsewhere. To install it for the paths shown above:

```bash
sudo cp workshop-monitor.service.example /etc/systemd/system/workshop-monitor.service
sudo systemctl daemon-reload
sudo systemctl enable --now workshop-monitor.service
sudo systemctl status workshop-monitor.service
journalctl -u workshop-monitor.service -f
```

If an existing working unit already runs `monitor.py`, keep it and add `EnvironmentFile=/etc/workshop-monitor.env` plus the correct virtual-environment `ExecStart`. The process handles SIGTERM, cancels its owned background tasks, and closes Telegram cleanly during restart or shutdown.

## Tests

Tests use the Python standard library and do not contact a UPS or Telegram:

```bash
python3 -m unittest discover -v
```

For a syntax check:

```bash
python3 -m compileall -q monitor.py config.py ups.py power_logger.py power_controller.py settings.py calibration.py plotting.py telegram_calibration.py tests
```

## Measurement limitations

The UPS reports load as integer percentage points. On a 2700 W UPS, one percentage point represents approximately 27 W, so measurements are approximate and quantized. They are suitable for workshop trends and comparisons, not billing-grade measurement.

There is no universal background-load assumption. A fresh installation starts with a 0 W uncalibrated fallback; every installation should run `/baseline` with its normal background devices on and the measured equipment off. Recalibrate whenever that setup changes. Printer power is estimated as `max(total watts - calibrated baseline watts, 0)`.

An abrupt power loss or forced process kill leaves the already-written CSV intact, but an in-progress session is not automatically resumed after restart. Telegram command access is governed by control of the bot; if the bot is exposed more broadly, add chat/user authorization before treating commands as private.
