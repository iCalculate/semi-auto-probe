param(
    [switch]$Silent
)

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectDir = (Resolve-Path (Join-Path $ScriptDir "..\..\..")).Path
$RuntimeDir = Join-Path $ProjectDir ".runtime"
$PidFile = Join-Path $RuntimeDir "semi-auto-probe-web.pid"
$RestartScript = Join-Path $ScriptDir "restart_web.ps1"
$WebPort = 8000
$DefaultToken = "GEMsE70403"
$LogFile = Join-Path $RuntimeDir "web_tray.log"

if (-not $Silent) {
    New-Item -ItemType Directory -Path $RuntimeDir -Force | Out-Null
    Start-Process -FilePath "powershell" `
        -ArgumentList @("-NoProfile", "-ExecutionPolicy", "Bypass", "-STA", "-WindowStyle", "Hidden", "-File", $MyInvocation.MyCommand.Path, "-Silent") `
        -WindowStyle Hidden `
        -WorkingDirectory $ProjectDir | Out-Null
    return
}

New-Item -ItemType Directory -Path $RuntimeDir -Force | Out-Null

function Write-TrayLog {
    param([string]$Text)
    Add-Content -LiteralPath $LogFile -Value ("{0:yyyy-MM-dd HH:mm:ss} {1}" -f (Get-Date), $Text) -Encoding UTF8
}

function Get-WebToken {
    $token = [Environment]::GetEnvironmentVariable("SEMI_AUTO_PROBE_WEB_TOKEN", "User")
    if ([string]::IsNullOrWhiteSpace($token)) {
        return $DefaultToken
    }
    return $token
}

function Stop-WebService {
    if (Test-Path -LiteralPath $PidFile) {
        $webPid = (Get-Content -LiteralPath $PidFile -ErrorAction SilentlyContinue | Select-Object -First 1).Trim()
        if ($webPid) {
            Stop-Process -Id $webPid -Force -ErrorAction SilentlyContinue
        }
    }

    $portOwners = @(Get-NetTCPConnection -LocalPort $WebPort -State Listen -ErrorAction SilentlyContinue |
        Select-Object -ExpandProperty OwningProcess -Unique)
    foreach ($ownerPid in $portOwners) {
        Stop-Process -Id $ownerPid -Force -ErrorAction SilentlyContinue
    }
    Write-TrayLog "Stopped web service"
}

function Restart-WebService {
    & $RestartScript -Silent
    Write-TrayLog "Restart requested"
}

function Show-Balloon {
    param(
        [string]$Title,
        [string]$Message,
        [System.Windows.Forms.ToolTipIcon]$Icon = [System.Windows.Forms.ToolTipIcon]::Info
    )
    $notify.BalloonTipTitle = $Title
    $notify.BalloonTipText = $Message
    $notify.BalloonTipIcon = $Icon
    $notify.ShowBalloonTip(5000)
}

function Update-Token {
    $current = Get-WebToken
    $newToken = [Microsoft.VisualBasic.Interaction]::InputBox(
        "Enter the new web access token. The web service will restart after saving.",
        "Update Semi Auto Probe Token",
        $current
    )
    if ([string]::IsNullOrWhiteSpace($newToken)) {
        Show-Balloon "Semi Auto Probe Web" "Token update cancelled."
        return
    }

    [Environment]::SetEnvironmentVariable("SEMI_AUTO_PROBE_WEB_TOKEN", $newToken, "User")
    Restart-WebService
    Show-Balloon "Semi Auto Probe Web" "Token updated and web service restarted."
}

function Check-Connections {
    Show-ConnectionsWindow
}

function Get-ConnectionSnapshot {
    $token = Get-WebToken
    $headers = @{ "X-Access-Token" = $token }
    return Invoke-RestMethod -Uri "http://127.0.0.1:$WebPort/api/connections" -Headers $headers -TimeoutSec 5
}

function Show-ConnectionsWindow {
    $form = New-Object System.Windows.Forms.Form
    $form.Text = "Semi Auto Probe Web Connections"
    $form.Size = New-Object System.Drawing.Size(760, 430)
    $form.StartPosition = "CenterScreen"
    $form.FormBorderStyle = "FixedDialog"
    $form.MaximizeBox = $false
    $form.MinimizeBox = $false
    $form.BackColor = [System.Drawing.Color]::FromArgb(18, 24, 30)
    $form.ForeColor = [System.Drawing.Color]::White

    $summary = New-Object System.Windows.Forms.Label
    $summary.AutoSize = $false
    $summary.Location = New-Object System.Drawing.Point(16, 14)
    $summary.Size = New-Object System.Drawing.Size(710, 46)
    $summary.Font = New-Object System.Drawing.Font("Segoe UI", 10, [System.Drawing.FontStyle]::Regular)
    $summary.ForeColor = [System.Drawing.Color]::Gainsboro
    $form.Controls.Add($summary)

    $grid = New-Object System.Windows.Forms.ListView
    $grid.Location = New-Object System.Drawing.Point(16, 70)
    $grid.Size = New-Object System.Drawing.Size(710, 255)
    $grid.View = [System.Windows.Forms.View]::Details
    $grid.FullRowSelect = $true
    $grid.GridLines = $true
    $grid.BackColor = [System.Drawing.Color]::FromArgb(22, 30, 38)
    $grid.ForeColor = [System.Drawing.Color]::White
    [void]$grid.Columns.Add("IP", 130)
    [void]$grid.Columns.Add("Streams", 70)
    [void]$grid.Columns.Add("Active Req", 80)
    [void]$grid.Columns.Add("Total Req", 80)
    [void]$grid.Columns.Add("Last Seen", 80)
    [void]$grid.Columns.Add("Last Path", 140)
    [void]$grid.Columns.Add("User Agent", 260)
    $form.Controls.Add($grid)

    $refreshLabel = New-Object System.Windows.Forms.Label
    $refreshLabel.AutoSize = $false
    $refreshLabel.Location = New-Object System.Drawing.Point(16, 338)
    $refreshLabel.Size = New-Object System.Drawing.Size(470, 24)
    $refreshLabel.ForeColor = [System.Drawing.Color]::DarkGray
    $form.Controls.Add($refreshLabel)

    $refreshButton = New-Object System.Windows.Forms.Button
    $refreshButton.Text = "Refresh"
    $refreshButton.Location = New-Object System.Drawing.Point(540, 334)
    $refreshButton.Size = New-Object System.Drawing.Size(86, 30)
    $form.Controls.Add($refreshButton)

    $closeButton = New-Object System.Windows.Forms.Button
    $closeButton.Text = "Close"
    $closeButton.Location = New-Object System.Drawing.Point(640, 334)
    $closeButton.Size = New-Object System.Drawing.Size(86, 30)
    $closeButton.Add_Click({ $form.Close() })
    $form.Controls.Add($closeButton)

    $refreshAction = {
        try {
            $data = Get-ConnectionSnapshot
            $summary.Text = "Active camera streams: $($data.active_camera_streams)    Active HTTP requests: $($data.active_http_requests)    Total requests: $($data.total_http_requests)    Sources: $($data.client_count)"
            $grid.Items.Clear()
            foreach ($client in $data.clients) {
                $item = New-Object System.Windows.Forms.ListViewItem([string]$client.ip)
                [void]$item.SubItems.Add([string]$client.active_camera_streams)
                [void]$item.SubItems.Add([string]$client.active_requests)
                [void]$item.SubItems.Add([string]$client.total_requests)
                [void]$item.SubItems.Add(("{0}s" -f $client.last_seen_seconds_ago))
                [void]$item.SubItems.Add([string]$client.last_path)
                [void]$item.SubItems.Add([string]$client.user_agent)
                [void]$grid.Items.Add($item)
            }
            $refreshLabel.Text = "Last refresh: $(Get-Date -Format 'HH:mm:ss')    Auto-refresh: 5s while this window is open"
        } catch {
            $summary.Text = "Unable to read connection status: $($_.Exception.Message)"
            $refreshLabel.Text = "Last refresh failed: $(Get-Date -Format 'HH:mm:ss')"
        }
    }

    $refreshButton.Add_Click($refreshAction)

    $timer = New-Object System.Windows.Forms.Timer
    $timer.Interval = 5000
    $timer.Add_Tick($refreshAction)
    $form.Add_Shown({
        & $refreshAction
        $timer.Start()
    })
    $form.Add_FormClosed({
        $timer.Stop()
        $timer.Dispose()
    })
    [void]$form.ShowDialog()
}

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
Add-Type -AssemblyName Microsoft.VisualBasic

$notify = New-Object System.Windows.Forms.NotifyIcon
$notify.Icon = [System.Drawing.SystemIcons]::Application
$notify.Text = "Semi Auto Probe Web"
$notify.Visible = $true

$menu = New-Object System.Windows.Forms.ContextMenuStrip

$openItem = $menu.Items.Add("Open Dashboard")
$openItem.add_Click({
    Start-Process "http://127.0.0.1:8000"
})

$restartItem = $menu.Items.Add("Restart Web Service")
$restartItem.add_Click({
    Restart-WebService
    Show-Balloon "Semi Auto Probe Web" "Restart requested."
})

$stopItem = $menu.Items.Add("Stop Running")
$stopItem.add_Click({
    Stop-WebService
    Show-Balloon "Semi Auto Probe Web" "Web service stopped."
})

[void]$menu.Items.Add("-")

$settingsMenu = New-Object System.Windows.Forms.ToolStripMenuItem("Web Settings")
$updateTokenItem = New-Object System.Windows.Forms.ToolStripMenuItem("Update Token")
$updateTokenItem.add_Click({ Update-Token })
$checkConnectionsItem = New-Object System.Windows.Forms.ToolStripMenuItem("Check Connections")
$checkConnectionsItem.add_Click({ Check-Connections })
[void]$settingsMenu.DropDownItems.Add($updateTokenItem)
[void]$settingsMenu.DropDownItems.Add($checkConnectionsItem)
[void]$menu.Items.Add($settingsMenu)

[void]$menu.Items.Add("-")

$exitItem = $menu.Items.Add("Exit Tray")
$exitItem.add_Click({
    $notify.Visible = $false
    $notify.Dispose()
    [System.Windows.Forms.Application]::Exit()
})

$notify.ContextMenuStrip = $menu
$notify.add_DoubleClick({
    Start-Process "http://127.0.0.1:8000"
})

Restart-WebService
Show-Balloon "Semi Auto Probe Web" "Tray manager is running."
Write-TrayLog "Tray manager started"

[System.Windows.Forms.Application]::Run()
