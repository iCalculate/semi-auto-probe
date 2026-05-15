# Semi Auto Probe

Python control software scaffold for a 4-axis RS-232 controller used as a 3-axis motor controller, with live USB camera preview.

## Current Features

- RS-232 serial configuration: `115200, N, 8, 1`
- Protocol helpers for the controller's 12-byte command frames
- Communication feedback test:
  - TX: `3A 55 00 00 00 00 00 00 00 8F 0D 0A`
  - Expected RX: `A3 AA 00 00 00 00 00 00 00 4D 0D 0A`
- Tkinter desktop app with:
  - serial port selection
  - connect/disconnect
  - communication test button
  - live USB camera preview

## Setup

Create the local virtual environment and install dependencies with `uv`:

```powershell
uv sync
```

Run the app:

```powershell
uv run python -m semi_auto_probe
```

The command line prints a startup logo and colorized runtime logs with timestamps, levels, and event details.

Run the command-line communication test:

```powershell
uv run python -m semi_auto_probe.cli test --port COM3
```

## Notes

- Many USB-to-RS232 adapters show up as `COM3`, `COM4`, etc.
- If the controller is connected through a USB-to-serial adapter, select that COM port.
- The camera defaults to device index `0`; change it in the UI if Windows assigns a different index.
- The controller has four axes, but this project intentionally exposes only the first three axes for later motor-control work.

## Development

Run protocol tests:

```powershell
uv run python -m unittest discover -s tests
```
