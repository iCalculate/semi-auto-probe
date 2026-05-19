# Web Deployment

This project now has a browser-based monitoring surface. The web page is
intentionally read-only for external users: camera preview, serial status, and
XYZ position monitoring.

The local Tkinter desktop app has camera priority. When it is running, it owns
the USB camera and publishes the latest frame from memory on a localhost-only
port. The web service proxies that frame at 1 FPS.

When the desktop app is not running, the web service opens the camera directly
and streams at 10 FPS. If the desktop app starts later and cannot open the
camera, it asks the local web service to release the fallback camera and retries.

## Run Locally

Install dependencies:

```powershell
uv sync
```

Start the web server:

```powershell
$env:SEMI_AUTO_PROBE_WEB_TOKEN="change-this-token"
.\.venv\Scripts\python.exe -m semi_auto_probe.web_app
```

The web service writes its current process ID to:

```text
D:\Project\semi-auto-probe\.runtime\semi-auto-probe-web.pid
```

Stop only the current web service process:

```powershell
$pidPath = "D:\Project\semi-auto-probe\.runtime\semi-auto-probe-web.pid"
$webPid = Get-Content $pidPath
Stop-Process -Id $webPid
```

Restart after editing web files:

```powershell
$pidPath = "D:\Project\semi-auto-probe\.runtime\semi-auto-probe-web.pid"
if (Test-Path $pidPath) {
  Stop-Process -Id (Get-Content $pidPath) -ErrorAction SilentlyContinue
}
$env:SEMI_AUTO_PROBE_WEB_TOKEN="GEMsE70403"
.\.venv\Scripts\python.exe -m semi_auto_probe.web_app
```

Or double-click:

```text
D:\Project\semi-auto-probe\src\semi_auto_probe\web\restart_web.ps1
```

For a fully hidden restart with no PowerShell window, double-click:

```text
D:\Project\semi-auto-probe\src\semi_auto_probe\web\restart_web_silent.vbs
```

For a persistent tray icon with right-click controls, double-click:

```text
D:\Project\semi-auto-probe\src\semi_auto_probe\web\web_tray_silent.vbs
```

The tray menu supports:

- Open Dashboard
- Restart Web Service
- Stop Running
- Web Settings > Update Token
- Web Settings > Check Connections opens a lightweight connection dashboard
  with source IPs, active camera streams, active requests, totals, last path,
  and user-agent details. It refreshes every 5 seconds only while the window is
  open.

Restart logs are written to:

```text
D:\Project\semi-auto-probe\.runtime\restart_web.log
```

Tray logs are written to:

```text
D:\Project\semi-auto-probe\.runtime\web_tray.log
```

Open:

```text
http://127.0.0.1:8000
```

The server listens on `127.0.0.1` by default. To listen on all LAN interfaces:

```powershell
$env:SEMI_AUTO_PROBE_WEB_HOST="0.0.0.0"
.\.venv\Scripts\python.exe -m semi_auto_probe.web_app
```

To monitor XYZ positions, configure the serial port before startup:

```powershell
$env:SEMI_AUTO_PROBE_WEB_SERIAL_PORT="COM3"
.\.venv\Scripts\python.exe -m semi_auto_probe.web_app
```

Start the local desktop app to publish camera frames:

```powershell
uv run python -m semi_auto_probe
```

The desktop app publishes frames on `127.0.0.1:8765` by default. To change the
local-only publisher port, set the same value for both processes:

```powershell
$env:SEMI_AUTO_PROBE_PUBLISHER_PORT="8765"
```

The desktop monitor publisher is throttled to 1 FPS by default so it does not compete
with the local UI. To adjust it:

```powershell
$env:SEMI_AUTO_PROBE_PUBLISHER_FPS="1"
```

The web fallback camera stream uses 10 FPS by default:

```powershell
$env:SEMI_AUTO_PROBE_WEB_DIRECT_CAMERA_FPS="10"
```

If you run the web service on a non-default port, set the same
`SEMI_AUTO_PROBE_WEB_PORT` for the desktop app process too. The desktop app uses
that local port when it asks the web fallback stream to release the camera.

## Fixed Public Link With Cloudflare Tunnel

Cloudflare Tunnel is the recommended option for a stable external URL because
the local machine does not need a public IP or router port forwarding.

1. Install `cloudflared` on the machine connected to the controller and camera.
2. Log in:

   ```powershell
   cloudflared tunnel login
   ```

3. Create a tunnel:

   ```powershell
   cloudflared tunnel create semi-auto-probe
   ```

4. Route a fixed hostname to the tunnel:

   ```powershell
   cloudflared tunnel route dns semi-auto-probe probe.example.com
   ```

5. Create a Cloudflare tunnel config, usually at
   `%USERPROFILE%\.cloudflared\config.yml`:

   ```yaml
   tunnel: semi-auto-probe
   credentials-file: C:\Users\YOUR_USER\.cloudflared\TUNNEL_ID.json

   ingress:
     - hostname: probe.example.com
       service: http://127.0.0.1:8000
     - service: http_status:404
   ```

6. Start the local web app and tunnel:

   ```powershell
   $env:SEMI_AUTO_PROBE_WEB_TOKEN="change-this-token"
   $env:SEMI_AUTO_PROBE_WEB_SERIAL_PORT="COM3"
   .\.venv\Scripts\python.exe -m semi_auto_probe.web_app
   cloudflared tunnel run semi-auto-probe
   ```

External users can then open:

```text
https://probe.example.com
```

## Safety Notes

- Always set `SEMI_AUTO_PROBE_WEB_TOKEN` before exposing the app externally.
- The web page does not include motion-control buttons or emergency-stop
  controls. Keep all motion control local.
- The web service opens the USB camera only as a fallback while the desktop app
  is not publishing frames. The desktop app can request release and retake the
  camera.
- Only one process should control the serial port at a time. Do not run the
  Tkinter app and the web app against the same COM port simultaneously.
- Keep emergency stop access physically available near the machine.
