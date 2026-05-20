<p align="center">
  <img src="assets/logo-system-diagram.svg" alt="Semi Auto Probe logo" width="120" />
</p>

<h1 align="center">Semi Auto Probe</h1>

<p align="center">
  <a href="#overview"><img alt="Project type" src="https://img.shields.io/badge/project-open--hardware-2563eb"></a>
  <a href="#hardware"><img alt="Hardware" src="https://img.shields.io/badge/hardware-documented-0f766e"></a>
  <a href="#software"><img alt="Software" src="https://img.shields.io/badge/software-python-3776ab"></a>
  <a href="#cost-summary"><img alt="Build cost" src="https://img.shields.io/badge/build_cost-SGD_3%2C477.79-f59e0b"></a>
  <a href="#development"><img alt="Tests" src="https://img.shields.io/badge/tests-unittest-16a34a"></a>
</p>

<p align="center">
  An open-source semi-automatic probe station that combines a motorized XYZ stage, microscope vision, probe manipulation, autofocus, and stitched-field imaging in one reproducible desktop workflow.
</p>

<p align="center">
  <a href="#overview">Overview</a> |
  <a href="#hardware">Hardware</a> |
  <a href="#software">Software</a> |
  <a href="#getting-started">Getting Started</a> |
  <a href="#development">Development</a>
</p>

## Overview

`Semi Auto Probe` is an open-source probe-station project with both hardware and software layers:

- **Hardware stack:** a motorized `XYZ` micro-positioning stage, 5-phase stepper motor drive electronics, a 4-axis motion-control board, microscope optics, a USB camera, probe arms, tungsten probes, and optical-platform fixtures.
- **Software stack:** a Python desktop application that controls motion over RS-232, displays live microscope video, supports visual metrology, performs autofocus, and captures stitched mosaics.
- **System goal:** make a compact semi-automatic probing workflow reproducible enough that another lab or maker can understand what to buy, how the pieces fit together, and what the software contributes.

The physical build uses a 4-axis controller, but the application intentionally operates the first three axes as `X`, `Y`, and `Z`. The stage provides precise motion, the microscope stack provides visual feedback, and the software ties them together into a practical workflow for device probing and imaging.

### What the system can do

- Drive a 3-axis probe stage over `115200, N, 8, 1`
- Show live USB microscope video with focus overlays
- Move visible image points to the field center after calibration
- Run autofocus on the `Z` axis with multiple focus metrics
- Capture serpentine mosaics with FFT-based image registration
- Use optional four-corner autofocus plane fitting for tilted samples
- Expose raw communication tools for controller debugging

### Strategic roadmap

The project roadmap focuses on three core technological enhancements:

- **Mechanical micro-nanofabrication:** develop an automated mobile platform for non-chemical-contact pattern processing, enabling precision patterning of 2D materials inside a controlled glovebox environment while reducing chemical contamination and interface damage.
- **Integrated optical systems:** upgrade the platform with multi-channel monochromatic light sources and DMD-based spatial light modulation, expanding the system toward PL in-situ testing and high-resolution photocurrent mapping.
- **High-stability electrical control:** transition toward a robust modular architecture with standardized wire-bonding array boards, moving beyond traditional probe-based workflows to improve testing efficiency and signal stability for multi-array devices.

## Hardware

The tables below list the hardware actually used in this build. Purchase links are intentionally omitted; the goal is to document the bill of materials and the rough cost of reproducing a comparable setup.

### Cost summary

| Subsystem | Main hardware | Cost |
| --- | --- | ---: |
| Microscope system | `sanqtid` coaxial microscope lens, focusing stand, USB microscope camera | `SGD 770.98` |
| Probe system | Probe holders, 3-axis probe fixtures, probe stage, tungsten probes | `SGD 1,219.51` |
| Motion and control | `KOHZU` motorized XYZ stage, `KOHZU MD-355F` driver, 4-axis controller, RS-232 cable | `SGD 1,161.93` |
| Optical-platform accessories | Magnetic base plates and M6 optical posts/adapters | `SGD 325.37` |
| **Total** |  | **`SGD 3,477.79`** |

> The total above is based on the saved order pages and their displayed `SGD` paid amounts. Some orders note that consolidated cross-border shipping can be paid separately, so any later standalone forwarding fees are not included here.

### Microscope system

| Item | Model / specification | Notes |
| --- | --- | --- |
| Coaxial microscope lens | `sanqtid` `3200x` coaxial-light lens | Listed as a 400-3600x industrial electronic microscope lens |
| Focusing support | `sanqtid` stereo fine-focus stand | 76 mm support with fine adjustment |
| Microscope camera | USB2.0 microscope camera, `5.1 MP` | Used for live vision, focus scoring, and mosaic capture |

### Probe system

| Item | Model / specification | Notes |
| --- | --- | --- |
| Left probe holder + 3-axis fixture | `JY050-12-L + JY800-1.5-TRB` | Manual precision positioning |
| Right probe holder + 3-axis fixture | `JY050-12-R + JY800-1.5-TRB` | Manual precision positioning |
| Additional probe stage | 3-axis probe sliding fixture | Listed as probe holder plus 3-axis clamp |
| Tungsten probes | `WG-38-0.5`, `WG-38-1.0`, `WG-38-2.0`, `WG-38-5.0` | Probe-tip sizes span roughly 1-10 micrometers across purchased variants |

### Motion and control system

| Item | Model / specification | Notes |
| --- | --- | --- |
| Motorized stage | `KOHZU` electric `XYZ` precision stage | Listed travel: `20 x 20 x 9 mm` |
| Motor driver | `KOHZU MD-355F` | 3-axis driver for 5-phase stepper motors; up to 250 microstep divisions |
| Motor type | 5-phase stepper motor | Driver datasheet uses a `0.72 deg` basic step angle |
| Motion controller | 4-axis controller module | RS-232 by default, with 16 NPN inputs and 16 transistor outputs |
| Serial adapter | USB to RS-232 cable | Used between the PC and controller |

Local reference documents for this subsystem are kept in [`refs/`](refs/):

- [`MotorDriverDatasheet.pdf`](refs/MotorDriverDatasheet.pdf)
- [`ControlUnitDatasheet.pdf`](refs/ControlUnitDatasheet.pdf)
- [`4-Axis Controller Communication Protoco·.pdf`](refs/4-Axis%20Controller%20Communication%20Protoco%C2%B7.pdf)
- [`Comm Protocal.txt`](refs/Comm%20Protocal.txt)

### Optical-platform accessories

| Item | Model / specification | Quantity |
| --- | --- | ---: |
| Magnetic optical base plate | `LPTP20080` | `3` |
| M6 optical post / adapter | `LPMP125`, 25 mm | `12` |
| M6 optical post / adapter | `LPMP1100`, 100 mm | `12` |
| M6 optical post / adapter | `LPMP1150`, 150 mm | `12` |

## Software

The software is a Python desktop application for operating the semi-automatic station. It combines motion control, microscope vision, calibration, autofocus, and imaging workflows in one interface.

### Core capabilities

- 3-axis controller integration over serial communication
- Live USB camera preview with focus-score overlays
- Visual tools for point-to-point distance, point-to-line distance, polygon area, and image-point centering
- Autofocus with coarse search, refinement, focus-history plots, and CSV export
- Serpentine image stitching with flat-field correction and FFT phase-correlation registration
- Read-only GDS layout viewing, affine GDS-to-stage calibration, live FOV overlay, and two-step click-to-move navigation
- Optional four-corner plane compensation for tilted samples
- Persistent local configuration for optical calibration and motor mapping
- Raw TX/RX communication console for protocol debugging

### Application pages

| Page | Purpose |
| --- | --- |
| `Main` | Live vision, visual measurement, image-point centering, position readout, jog controls, home-signal polling, zeroing |
| `Communication` | Raw command entry, communication-test frame loading, last TX/RX display, hex history |
| `AutoFocus` | Z autofocus, focus metric selection, score plots, manual Z jog, Z zeroing |
| `LayoutBond` | Read-only GDS viewer, layer toggles, GDS/stage calibration, current FOV overlay, selected-target movement |
| `ImgStitch` | Serpentine mosaic capture, overlap settings, stitch preview, optional four-corner plane AF |
| `Config` | Objective/eyepiece selection, pixel calibration, motor mapping, conversion display |

### Supported protocol capabilities

- Communication feedback test
- Realtime position enable/disable
- Single-axis position reads
- I/O status reads for home inputs
- Clear-position commands
- Single-axis relative and absolute moves
- 4-axis coordinated relative move command generation
- Coordinated-move completion handling
- Decelerated and emergency stops

## Getting Started

### Requirements

#### Hardware

- A compatible 4-axis motion controller connected through RS-232 or a USB-to-RS232 adapter
- A Windows-visible USB microscope camera
- A probe stage wired so the first three controller axes map to application axes `X`, `Y`, and `Z`

#### Software

- Python `>=3.10`
- Recommended dependency manager: `uv`
- Python packages are declared in `pyproject.toml` and mirrored in `requirements.txt` for pip-based installs.
- GDS layout loading requires `gdstk`. If it is missing, the application still starts, but the `LayoutBond` page will ask you to install it.

### Installation

Create the local environment and install dependencies:

```powershell
uv sync
```

After dependency changes, refresh the uv lockfile and environment:

```powershell
uv lock
uv sync
```

If you only need to add the GDS dependency in an existing checkout, use:

```powershell
uv add gdstk
```

Run the GUI:

```powershell
uv run python -m semi_auto_probe
```

Run the command-line communication test:

```powershell
uv run python -m semi_auto_probe.cli test --port COM3
```

### First-run workflow

1. Connect the controller and camera.
2. Launch the app, select the correct serial port, and click `Connect`.
3. Click `Test` to verify controller feedback.
4. Open `Config`, confirm motor settings, select the active objective/eyepiece pair, and run pixel calibration if image-to-stage conversion is needed.
5. On `Main`, read the current position, verify axis direction, and use `Set New Zero` only after the stage is at the intended coordinate origin.
6. Use `AutoFocus` to find a usable `Z` position before imaging.
7. Use `ImgStitch` for raster acquisition after overlap, travel distance, and optional plane compensation have been selected.

## Workflow Details

### Main page

- Position cells show `X`, `Y`, and `Z`
- Single-click a coordinate cell to enter a relative move
- Double-click a coordinate cell to enter an absolute target
- `Move`, `Read`, `Continue`, jog controls, home-signal polling, zeroing, and emergency stop are all available from the main motion panel
- Vision tools include `Center +`, `Point-Point`, `Point-Line`, `Polygon Area`, and `Move Center`

### LayoutBond

- Load read-only `.gds` files, display the selected top cell, toggle layers, zoom, pan, and fit the layout to view
- Cursor coordinates are snapped to a selectable GDS grid: `100 nm`, `1 um`, `5 um`, or `10 um`
- Calibration points `P1` to `P4` can be typed manually, set from the current stage position, or picked from the layout
- Click `Set GDS` to enter pick mode; the active button turns amber, then double-click a snapped layout point to fill the GDS coordinate and restore the button color
- After fitting the affine mapping, LayoutBond previews selected GDS targets in stage micrometers and moves only after `Move to Selected Target`

### AutoFocus

- Available metrics: `Laplacian`, `Tenengrad`, `Brenner`
- Search flow: center sample -> coarse sweep -> local refinement -> return to best usable `Z`
- Each run writes `last_autofocus_history.csv`

### Image stitching

- Traversal is serpentine
- Neighboring fields are registered with FFT phase correlation
- Flat-field correction is applied before stitching
- Final output is written to `last_imgstitch.png`
- Four-corner plane AF fits a tilted sample plane from four autofocus measurements

### Configuration

Local settings are stored in:

```text
probe_config.local.json
```

The configuration page controls optical calibration, active objective/eyepiece selection, motor microstep settings, lead values, speed, acceleration, and derived `um/pulse` conversions.

Example:

```json
{
  "base_angle_deg": 0.72,
  "calibrations": {
    "objective_20__eyepiece_1.5": 0.42
  },
  "cc_accel_time_s": 0.1,
  "cc_speed_percent": 100,
  "eyepiece": 1.5,
  "lead_xy_mm": 1.0,
  "lead_z_mm": 0.5,
  "microstep": 2,
  "objective": 20
}
```

## Project Layout

```text
src/semi_auto_probe/
  app.py                   Tkinter application and workflow orchestration
  camera.py                USB camera capture, overlays, focus metrics
  config.py                Persistent optical/motor configuration
  protocol.py              Frame builders and response parsers
  serial_client.py         Thread-safe serial transport helpers
  img_stitch.py            Stitching, flat-field correction, plane fitting
  ui/vision.py             Main-page visual tools
  ui/calibration_dialog.py Pixel-calibration dialog
```

## Generated and Local Files

| File | Meaning |
| --- | --- |
| `probe_config.local.json` | Local optical/motor configuration |
| `last_autofocus_history.csv` | Most recent autofocus sampling history |
| `last_imgstitch.png` | Most recent stitched mosaic |

These files are ignored by Git and are safe to keep local.

## Development

Run the full test suite:

```powershell
uv run python -m unittest discover -s tests
```

If you run tests outside the project environment, make sure the active interpreter has `opencv-python`, `numpy`, and `pyserial` installed. Importing the GUI stack also imports stitching code, so OpenCV is required even for some non-camera tests.
For GDS viewer tests or manual GDS loading, the interpreter also needs `gdstk`.

## Troubleshooting

### No serial ports appear

- Confirm the adapter is visible in Windows Device Manager
- Install the USB-to-RS232 driver if required
- Click `Refresh` after connecting the adapter

### Communication test fails

- Confirm the selected COM port
- Confirm controller power and RS-232 wiring
- Verify the controller uses `115200, N, 8, 1`

### Camera preview is unavailable

- Try another camera index
- Close other applications already using the camera
- Click `Restart`

### GDS loading says gdstk is missing

- If you use `uv`, run `uv sync` after pulling the latest dependency files.
- If the lockfile has not been updated yet, run `uv lock` and then `uv sync`.
- For a local one-off fix, run `uv add gdstk`; this writes the dependency to `pyproject.toml` and updates the uv environment.

### Vision move is disabled

- Run pixel calibration for the currently selected objective/eyepiece pair
- Confirm the stage conversion settings are correct before using image-to-stage moves

### Stitching quality is poor

- Verify overlap values match the actual field overlap
- Recheck flat-field behavior under the current illumination
- Confirm the configured physical step sizes match the current optical calibration and motor mapping
- Enable plane AF when the sample surface is tilted across the stitched area

## Safety Notes

- Verify axis directions at low jog distances before using large moves
- Confirm the coordinate origin before using `Set New Zero`
- Keep the emergency-stop path accessible during any automated motion
- Use conservative `Z` ranges until sample clearance is known
