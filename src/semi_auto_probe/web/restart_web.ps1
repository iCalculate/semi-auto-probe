param(
    [switch]$Silent
)

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectDir = (Resolve-Path (Join-Path $ScriptDir "..\..\..")).Path
$PidFile = Join-Path $ProjectDir ".runtime\semi-auto-probe-web.pid"
$RuntimeDir = Split-Path -Parent $PidFile
$WebToken = [Environment]::GetEnvironmentVariable("SEMI_AUTO_PROBE_WEB_TOKEN", "User")
if ([string]::IsNullOrWhiteSpace($WebToken)) {
    $WebToken = "GEMsE70403"
}
$WebPort = 8000
$LogFile = Join-Path $RuntimeDir "restart_web.log"

if (-not $Silent) {
    New-Item -ItemType Directory -Path $RuntimeDir -Force | Out-Null
    Start-Process -FilePath "powershell" `
        -ArgumentList @("-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden", "-File", $MyInvocation.MyCommand.Path, "-Silent") `
        -WindowStyle Hidden `
        -WorkingDirectory $ProjectDir | Out-Null
    return
}

$ErrorActionPreference = "Stop"

function Write-Log {
    param(
        [string]$Level,
        [string]$Text
    )
    $line = "{0:yyyy-MM-dd HH:mm:ss} [{1}] {2}" -f (Get-Date), $Level, $Text
    Add-Content -LiteralPath $LogFile -Value $line -Encoding UTF8
}

function Write-Step {
    param(
        [string]$Number,
        [string]$Text
    )
    Write-Host ""
    Write-Host " [$Number] " -NoNewline -ForegroundColor Cyan
    Write-Host $Text -ForegroundColor White
    Write-Log "INFO" "[$Number] $Text"
}

function Write-Detail {
    param([string]$Text)
    Write-Host "      $Text" -ForegroundColor DarkGray
    Write-Log "INFO" $Text
}

New-Item -ItemType Directory -Path $RuntimeDir -Force | Out-Null
Set-Content -LiteralPath $LogFile -Value ("{0:yyyy-MM-dd HH:mm:ss} [INFO] Restart requested" -f (Get-Date)) -Encoding UTF8

Write-Detail "Project  $ProjectDir"
Write-Detail "PID file $PidFile"

Write-Step "1/4" "Checking existing web service"
$stoppedExisting = $false
if (Test-Path -LiteralPath $PidFile) {
    $webPid = (Get-Content -LiteralPath $PidFile -ErrorAction SilentlyContinue | Select-Object -First 1).Trim()
    if ($webPid) {
        $process = Get-Process -Id $webPid -ErrorAction SilentlyContinue
        if ($process) {
            Write-Detail "Found running process PID $webPid"
            Stop-Process -Id $webPid -Force
            Start-Sleep -Milliseconds 500
            Write-Host "      Stopped old web service" -ForegroundColor Green
            Write-Log "INFO" "Stopped old web service PID $webPid"
            $stoppedExisting = $true
        } else {
            Write-Detail "PID file exists, but process is not running"
        }
    } else {
        Write-Detail "PID file is empty"
    }
} else {
    Write-Detail "No existing PID file found"
}

if (-not $stoppedExisting) {
    $portOwners = @(Get-NetTCPConnection -LocalPort $WebPort -State Listen -ErrorAction SilentlyContinue |
        Select-Object -ExpandProperty OwningProcess -Unique)
    foreach ($ownerPid in $portOwners) {
        $process = Get-Process -Id $ownerPid -ErrorAction SilentlyContinue
        if ($process) {
            Write-Detail "Found process PID $ownerPid listening on port $WebPort"
            Stop-Process -Id $ownerPid -Force
            Start-Sleep -Milliseconds 500
            Write-Host "      Stopped port $WebPort process" -ForegroundColor Green
            Write-Log "INFO" "Stopped process PID $ownerPid listening on port $WebPort"
            $stoppedExisting = $true
        }
    }
}

if (-not $stoppedExisting) {
    Write-Detail "No running web service found"
}

Write-Step "2/4" "Preparing runtime directory"
New-Item -ItemType Directory -Path $RuntimeDir -Force | Out-Null
Write-Host "      Runtime directory ready" -ForegroundColor Green
Write-Log "INFO" "Runtime directory ready"

Write-Step "3/4" "Starting new web service"
$command = "Set-Location -LiteralPath '$ProjectDir'; `$env:SEMI_AUTO_PROBE_WEB_TOKEN='$WebToken'; `$env:SEMI_AUTO_PROBE_WEB_PORT='$WebPort'; uv run semi-auto-probe-web"
Start-Process -FilePath "powershell" `
    -ArgumentList @("-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", $command) `
    -WindowStyle Hidden `
    -WorkingDirectory $ProjectDir | Out-Null
Write-Host "      Launch requested" -ForegroundColor Green
Write-Log "INFO" "Launch requested"

Write-Step "4/4" "Waiting for service to write PID"
$deadline = (Get-Date).AddSeconds(8)
while ((Get-Date) -lt $deadline -and -not (Test-Path -LiteralPath $PidFile)) {
    Start-Sleep -Milliseconds 250
}

if (Test-Path -LiteralPath $PidFile) {
    $newPid = (Get-Content -LiteralPath $PidFile | Select-Object -First 1).Trim()
    Write-Host "      Running with PID $newPid" -ForegroundColor Green
    Write-Host "      Local URL http://127.0.0.1:$WebPort" -ForegroundColor Green
    Write-Host "      Token     $WebToken" -ForegroundColor Green
    Write-Log "INFO" "Running with PID $newPid"
    Write-Log "INFO" "Local URL http://127.0.0.1:$WebPort"
} else {
    Write-Host "      Started, but PID file was not created yet" -ForegroundColor Yellow
    Write-Detail "Check .runtime\restart_web.log if the service is not reachable"
    Write-Log "WARN" "Started, but PID file was not created yet"
}

Write-Log "INFO" "Restart script completed"
