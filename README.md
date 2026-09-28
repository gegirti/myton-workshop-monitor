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

Matplotlib is used only when a completed test graph is generated. No database or web framework is required.

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
| `BASELINE_WATTS` | `27` | Non-printer load subtracted from total |
| `POWER_SAMPLE_INTERVAL` | `5` | Active-session sampling seconds |
| `UPS_MONITOR_INTERVAL` | `5` | Outage-monitor polling seconds (existing behavior) |
| `NUT_TIMEOUT` | `5` | Timeout for each `upsc` call |
| `NUT_FAILURE_WARNING_THRESHOLD` | `3` | Consecutive failures before explicit warning |
| `DATA_DIR` | `data` | CSV and PNG output directory |

Relative `DATA_DIR` paths are relative to the service `WorkingDirectory`.

## Telegram commands

- `/ups` — current UPS details (existing behavior)
- `/powerstart <name>` — start one named measurement session
- `/powerstatus` — latest measurement values and sample count
- `/powerstop` — stop, calculate statistics/energy, and send a PNG graph

Example workflow:

1. Leave the Raspberry Pi and modem on their normal UPS sockets.
2. Ensure no printer test is currently active with `/powerstatus`.
3. Send `/powerstart Creality_Hi_Benchy`.
4. Run the print.
5. Send `/powerstatus` at any time without interrupting collection.
6. Send `/powerstop`; the summary and graph are returned to Telegram.

Only one measurement can run at a time. The initial sample records UPS state, battery charge, and battery voltage. Later samples are appended immediately to a unique UTF-8 CSV in `data/`; generated graphs are stored beside their CSV. Those generated files are ignored by Git while `data/.gitkeep` retains the directory.

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
python3 -m compileall -q monitor.py config.py ups.py power_logger.py tests
```

## Measurement limitations

The UPS reports load as integer percentage points. On a 2700 W UPS, one percentage point represents approximately 27 W, so measurements are approximate and quantized. They are suitable for workshop trends and comparisons, not billing-grade measurement.

The default 27 W baseline is an observed Raspberry Pi + modem baseline. It is configurable with `BASELINE_WATTS` and can be calibrated later by recording the stable load with the printer switched off. Printer power is estimated as `max(total watts - baseline watts, 0)`.

An abrupt power loss or forced process kill leaves the already-written CSV intact, but an in-progress session is not automatically resumed after restart. Telegram command access is governed by control of the bot; if the bot is exposed more broadly, add chat/user authorization before treating commands as private.
