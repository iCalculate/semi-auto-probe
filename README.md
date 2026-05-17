# Semi Auto Probe

Desktop control software for a semi-automatic probe platform that combines 3-axis motion control, RS-232 communication, live USB vision, optical calibration, autofocus, and stitched-field imaging.

The hardware controller exposes four axes, while this application intentionally operates the first three as `X`, `Y`, and `Z`.

## Highlights

- 3-axis controller integration over `115200, N, 8, 1`
- Live USB camera preview with focus-score overlays
- Manual protocol console with TX/RX history
- Interactive vision tools for centering and metrology
- Autofocus with coarse search, refinement, score history, and CSV export
- Image stitching with serpentine traversal, FFT phase-correlation registration, flat-field correction, and optional four-corner plane compensation
- Optical and motor calibration persisted to a local JSON config
- Home-signal polling, position reading, jogging, multi-axis moves, and emergency stop controls

## Requirements

### Hardware

- A compatible 4-axis motion controller connected through RS-232 or a USB-to-RS232 adapter
- A Windows-visible USB camera
- A probe stage wired so the first three controller axes map to application axes `X`, `Y`, and `Z`

### Software

- Python `>=3.10`
- Recommended dependency manager: `uv`
- Python packages:
  - `pyserial`
  - `opencv-python`
  - `numpy`

## Installation

Create the local environment and install dependencies:

```powershell
uv sync
```

Run the GUI:

```powershell
uv run python -m semi_auto_probe
```

Run the command-line communication test:

```powershell
uv run python -m semi_auto_probe.cli test --port COM3
```

The application prints a startup banner and colorized runtime logs in the terminal.

## First-Run Workflow

1. Connect the controller and camera.
2. Launch the app, select the correct serial port, and click `Connect`.
3. Click `Test` to verify controller feedback.
4. Open `Config`, confirm motor settings, select the active objective/eyepiece pair, and run pixel calibration if image-to-stage conversion is needed.
5. On `Main`, read the current position, verify axis direction, and use `Set New Zero` only after the stage is at the intended coordinate origin.
6. Use `AutoFocus` to find a usable Z position before imaging.
7. Use `ImgStitch` for raster acquisition after overlap, travel distance, and optional plane compensation have been selected.

## UI Overview

| Page | Purpose |
| --- | --- |
| `Main` | Live vision, visual measurement, image-point centering, position readout, jog controls, home-signal polling, zeroing |
| `Communication` | Raw command entry, communication-test frame loading, last TX/RX display, hex history |
| `AutoFocus` | Z autofocus, focus metric selection, score plots, manual Z jog, Z zeroing |
| `ImgStitch` | Serpentine mosaic capture, overlap settings, stitch preview, optional four-corner plane AF |
| `Config` | Objective/eyepiece selection, pixel calibration, motor mapping, conversion display |

## Main Page

### Motion controls

- Position cells show `X`, `Y`, and `Z`.
- Single-click a coordinate cell to enter a relative move.
- Double-click a coordinate cell to enter an absolute target.
- `Move` sends the requested move, `Read` refreshes current coordinates, and `Continue` enables realtime position display.
- Each axis row provides jog step size plus `Fwd`, `Rev`, and `Stop`.
- `Home Signals` polls the controller I/O status and lights the axis indicators when home inputs are active.
- `Set New Zero` clears the current `X/Y/Z` position to zero on the controller.
- `EMERGENCY STOP` sends a global emergency stop command.

### Vision tools

The live image panel provides:

- `Center +`: toggle a center crosshair
- `Point-Point`: measure distance between two points
- `Point-Line`: measure perpendicular distance from a point to a line
- `Polygon Area`: measure polygon area
- `Move Center`: double-click a visible point to move it to the image center with coordinated `X/Y` motion

Distance and area measurements are shown in pixels. When a calibration exists for the selected objective/eyepiece pair, the same readout also includes micrometers or square micrometers.

`Move Center` requires a valid pixel calibration because it converts image offset into physical stage travel.

## Communication Page

The communication page supports both `Hex` and `Text` entry modes.

- `Load Test` inserts the default communication-test frame:
  - TX: `3A 55 00 00 00 00 00 00 00 8F 0D 0A`
  - Expected RX: `A3 AA 00 00 00 00 00 00 00 4D 0D 0A`
- `Read bytes` controls how many response bytes are requested for manual commands.
- `Send` transmits the payload.
- `Last TX`, `Last RX`, and the colorized history panel help with protocol debugging.

## AutoFocus

`AutoFocus` searches the Z axis around the current position.

- Available metrics: `Laplacian`, `Tenengrad`, `Brenner`
- Tunable parameters:
  - initial step
  - minimum step
  - search range
  - per-metric thresholds
- The workflow:
  1. sample the current center Z
  2. expand through coarse offsets
  3. refine around the best candidate
  4. return to the best Z if the result is usable, otherwise return to the original center

The page shows:

- live autofocus camera preview
- score-vs-Z chart
- rolling focus history

Each autofocus run writes:

```text
last_autofocus_history.csv
```

The CSV records sampled Z positions, focus scores, command frames, and reached-position feedback.

## Image Stitching

`ImgStitch` captures a grid of fields and builds a mosaic.

### Inputs

- `Rows`, `Cols`: grid size
- `Overlap X (px)`, `Overlap Y (px)`: expected overlap used for registration
- `Step X (um)`, `Step Y (um)`: physical travel between neighboring tiles; converted to controller pulses using the active motor mapping
- `Four-corner plane AF`: optional surface-plane compensation

### Behavior

- Traversal is serpentine: left-to-right on one row, right-to-left on the next.
- Each tile is flat-field corrected before stitching.
- Neighboring tiles are registered with FFT phase correlation.
- The mosaic preview updates during acquisition.
- The final image is written to:

```text
last_imgstitch.png
```

### Four-corner plane AF

When enabled:

1. The stage visits the four mosaic corners.
2. Autofocus runs at each corner.
3. The four Z values are used to fit a plane.
4. Each later tile uses the fitted plane to choose a corrected Z target during acquisition.

Plane AF requires at least a `2 x 2` grid.

## Configuration

The `Config` page stores local optical and motion settings in:

```text
probe_config.local.json
```

The file is local runtime state and is ignored by Git.

### Optical calibration

- Supported objective choices: `20x`, `10x`, `5x`
- Supported eyepiece choices: `1.0x` through `4.0x`
- Pixel calibration is stored per objective/eyepiece combination
- Calibration uses a three-point workflow:
  1. first two clicks define a reference line
  2. the third click defines a perpendicular distance to that line
  3. the known real distance converts pixels into `um/px`

### Motor mapping

The configuration page controls:

- microstep
- base motor angle
- `X/Y` lead
- `Z` lead
- coordinated-control speed
- coordinated-control acceleration/deceleration time

The UI also displays derived values such as steps per revolution and `um/pulse`.

### Example `probe_config.local.json`

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

## Supported Protocol Capabilities

The code currently implements:

- communication feedback test
- realtime position enable/disable
- single-axis position reads
- I/O status reads for home inputs
- clear-position commands
- single-axis relative and absolute moves
- 4-axis coordinated relative move command generation
- coordinated-move completion handling
- decelerated and emergency stops

## Project Layout

```text
src/semi_auto_probe/
  app.py                  Tkinter application and workflow orchestration
  camera.py               USB camera capture, overlays, focus metrics
  config.py               Persistent optical/motor configuration
  protocol.py             Frame builders and response parsers
  serial_client.py        Thread-safe serial transport helpers
  img_stitch.py           Stitching, flat-field correction, plane fitting
  ui/vision.py            Main-page visual tools
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

Reference controller documentation is stored under:

```text
refs/
```

## Troubleshooting

### No serial ports appear

- Confirm the adapter is visible in Windows Device Manager.
- Install the USB-to-RS232 driver if required.
- Click `Refresh` after connecting the adapter.

### Communication test fails

- Confirm the selected COM port.
- Confirm controller power and RS-232 wiring.
- Verify the controller uses `115200, N, 8, 1`.

### Camera preview is unavailable

- Try another camera index.
- Close other applications already using the camera.
- Click `Restart`.

### Vision move is disabled

- Run pixel calibration for the currently selected objective/eyepiece pair.
- Confirm the stage conversion settings are correct before using image-to-stage moves.

### Stitching quality is poor

- Verify overlap values match the actual field overlap.
- Recheck flat-field behavior under the current illumination.
- Confirm the configured physical step sizes match the current optical calibration and motor mapping.
- Enable plane AF when the sample surface is tilted across the stitched area.

## Safety Notes

- Verify axis directions at low jog distances before using large moves.
- Confirm the coordinate origin before using `Set New Zero`.
- Keep the emergency-stop path accessible during any automated motion.
- Use conservative Z ranges until sample clearance is known.

---

# 中文说明

## 项目简介

`Semi Auto Probe` 是一套用于半自动探针平台的桌面控制软件，集成了三轴运动控制、RS-232 通信、USB 实时视觉、光学校准、自动聚焦和拼场成像。

硬件控制器本身支持四轴，但当前软件只把前三轴作为 `X`、`Y`、`Z` 使用。

## 主要功能

- 通过 `115200, N, 8, 1` 控制三轴运动
- USB 相机实时预览和焦点评分叠加
- 手动协议调试窗口与 TX/RX 历史
- 图像测量与图像点自动居中
- 自动聚焦、评分曲线和 CSV 记录
- 蛇形路径拼场、FFT 相位相关配准、平场校正、四角平面补偿
- 本地 JSON 配置保存光学与电机参数
- Home 信号轮询、坐标读取、点动、联动和急停

## 环境要求

### 硬件

- 兼容的四轴运动控制器
- RS-232 或 USB 转 RS-232 连接
- Windows 可识别的 USB 相机
- 已正确映射为 `X/Y/Z` 的前三轴

### 软件

- Python `>=3.10`
- 推荐使用 `uv`
- 依赖：
  - `pyserial`
  - `opencv-python`
  - `numpy`

## 安装与启动

安装依赖：

```powershell
uv sync
```

启动图形界面：

```powershell
uv run python -m semi_auto_probe
```

运行命令行通信测试：

```powershell
uv run python -m semi_auto_probe.cli test --port COM3
```

## 首次使用流程

1. 连接控制器和相机。
2. 启动程序，选择串口并点击 `Connect`。
3. 点击 `Test` 验证通信。
4. 进入 `Config`，确认电机参数，选择镜头组合，并按需完成像素标定。
5. 在 `Main` 页读取当前坐标，确认轴向，只有在平台处于目标原点时才使用 `Set New Zero`。
6. 在成像前先执行 `AutoFocus`。
7. 设置重叠、步距和是否启用平面补偿后，再使用 `ImgStitch` 进行拼场。

## 页面总览

| 页面 | 用途 |
| --- | --- |
| `Main` | 实时视觉、图像测量、点动、坐标读取、Home 信号、零点设置 |
| `Communication` | 原始命令输入、测试帧、TX/RX、通信历史 |
| `AutoFocus` | Z 自动聚焦、焦点评分、手动 Z 控制 |
| `ImgStitch` | 蛇形扫描拼场、重叠设置、预览、四角 AF 平面补偿 |
| `Config` | 镜头组合、像素标定、电机映射、换算显示 |

## Main 页面

### 运动控制

- 单击坐标框可输入相对移动。
- 双击坐标框可输入绝对目标位置。
- `Move` 发送移动，`Read` 读取当前位置，`Continue` 开启实时坐标显示。
- 每个轴都提供步距、正向、反向和停止。
- `Home Signals` 会轮询控制器 I/O，并在输入触发时点亮对应轴指示。
- `Set New Zero` 会把当前 `X/Y/Z` 清零。
- `EMERGENCY STOP` 会发送全局急停命令。

### 图像工具

- `Center +`：显示中心十字
- `Point-Point`：两点距离
- `Point-Line`：点到直线距离
- `Polygon Area`：多边形面积
- `Move Center`：双击图像中的点，将其移动到视野中心

未标定时显示像素值；对应镜头组合完成标定后，会同时显示微米或平方微米。

`Move Center` 依赖像素标定，因为它需要把图像偏移换算成平台物理位移。

## Communication 页面

支持 `Hex` 与 `Text` 两种输入模式。

- `Load Test` 会载入默认通信测试帧：
  - TX: `3A 55 00 00 00 00 00 00 00 8F 0D 0A`
  - 期望 RX: `A3 AA 00 00 00 00 00 00 00 4D 0D 0A`
- `Read bytes` 控制要读取的响应字节数。
- `Send` 发送命令。
- 页面会显示最近一次 TX/RX 和彩色历史记录。

## AutoFocus 页面

自动聚焦围绕当前 Z 位置执行搜索。

- 支持指标：`Laplacian`、`Tenengrad`、`Brenner`
- 可设置初始步距、最小步距、搜索范围和阈值
- 逻辑包括：
  1. 在中心点采样
  2. 进行粗搜索
  3. 在最佳点附近细化
  4. 若结果可用则回到最佳 Z，否则返回初始中心点

每次运行会生成：

```text
last_autofocus_history.csv
```

其中记录了采样 Z、焦点评分、发送命令和到位反馈。

## ImgStitch 页面

`ImgStitch` 用于采集阵列图像并生成拼场图。

### 输入参数

- `Rows`、`Cols`：阵列规模
- `Overlap X (px)`、`Overlap Y (px)`：用于配准的预期重叠
- `Step X (um)`、`Step Y (um)`：相邻图块之间的平台物理移动距离
- `Four-corner plane AF`：是否启用四角自动聚焦平面补偿

### 工作方式

- 采用蛇形遍历路径。
- 每张图先做平场校正。
- 相邻图块使用 FFT 相位相关进行配准。
- 采集过程中实时更新拼图预览。
- 最终输出：

```text
last_imgstitch.png
```

### 四角平面 AF

启用后会：

1. 移动到拼场区域四角
2. 在每个角点执行自动聚焦
3. 用四个 Z 值拟合样品平面
4. 后续拍摄时按位置自动修正 Z

此功能要求阵列至少为 `2 x 2`。

## Config 页面

本地配置文件：

```text
probe_config.local.json
```

### 光学校准

- 物镜选项：`20x`、`10x`、`5x`
- 目镜选项：`1.0x` 到 `4.0x`
- 每组镜头组合单独保存像素标定
- 三点标定方式：
  1. 前两点定义参考直线
  2. 第三点定义到该直线的垂距
  3. 输入真实距离后得到 `um/px`

### 电机映射

可配置：

- 细分
- 基本步距角
- `X/Y` 导程
- `Z` 导程
- 联动速度
- 加减速时间

界面会显示步数/圈、`um/pulse` 等换算结果。

### 配置示例

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

## 当前已实现的协议能力

- 通信回环测试
- 实时坐标开关
- 单轴位置读取
- I/O 状态读取
- 清零
- 单轴相对/绝对移动
- 四轴联动相对移动帧生成
- 联动完成反馈处理
- 减速停止与急停

## 项目结构

```text
src/semi_auto_probe/
  app.py
  camera.py
  config.py
  protocol.py
  serial_client.py
  img_stitch.py
  ui/vision.py
  ui/calibration_dialog.py
```

## 本地生成文件

| 文件 | 含义 |
| --- | --- |
| `probe_config.local.json` | 本地光学/电机配置 |
| `last_autofocus_history.csv` | 最近一次自动聚焦记录 |
| `last_imgstitch.png` | 最近一次拼场结果 |

这些文件都已加入 `.gitignore`。

## 开发与测试

运行全部测试：

```powershell
uv run python -m unittest discover -s tests
```

如果脱离项目环境直接运行测试，当前解释器仍需要安装 `opencv-python`、`numpy` 和 `pyserial`。由于 GUI 导入链会带入拼场模块，因此某些非相机测试也依赖 OpenCV。

控制器资料位于：

```text
refs/
```

## 常见问题

### 看不到串口

- 检查设备管理器
- 安装 USB 转串口驱动
- 重新点击 `Refresh`

### 通信测试失败

- 确认 COM 口
- 检查控制器供电与 RS-232 接线
- 确认协议参数为 `115200, N, 8, 1`

### 相机不可用

- 切换相机编号
- 关闭占用相机的软件
- 点击 `Restart`

### 图像居中不可用

- 先为当前镜头组合完成像素标定
- 再确认电机映射参数是否正确

### 拼场效果不好

- 检查实际重叠与设置是否一致
- 检查照明条件和平场校正效果
- 检查平台物理步距和当前电机映射是否匹配
- 样品有倾斜时启用四角平面 AF

## 安全提示

- 大步距移动前先用小步距确认方向。
- 使用 `Set New Zero` 前先确认真实原点。
- 自动运动期间保持急停可用。
- 在样品间隙未知前，先使用保守的 Z 搜索范围。
