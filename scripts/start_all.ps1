param(
    [string]$ModelMockMode = "false",
    [switch]$ReuseExisting
)

$ErrorActionPreference = "Stop"

# Start model service, backend, and frontend in separate PowerShell windows.
# Model service: http://127.0.0.1:8100
# Backend: http://127.0.0.1:8001
# Frontend: http://127.0.0.1:5173
#
# By default this script restarts the three fixed local ports, because during
# development a healthy old process can still serve stale Python/JS code. Pass
# -ReuseExisting only when you intentionally want to keep the old processes.
$ProjectRoot = Resolve-Path "$PSScriptRoot\.."
$ModelScript = Join-Path $ProjectRoot "scripts\start_model_services.ps1"
$BackendScript = Join-Path $ProjectRoot "scripts\start_backend.ps1"
$FrontendScript = Join-Path $ProjectRoot "scripts\start_frontend.ps1"

function Test-HttpEndpoint {
    param(
        [string]$Url
    )

    try {
        Invoke-RestMethod -Uri $Url -TimeoutSec 3 | Out-Null
        return $true
    }
    catch {
        return $false
    }
}

function Stop-MeetingServiceOnPort {
    param(
        [int]$Port
    )

    $listeners = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue
    foreach ($listener in $listeners) {
        try {
            Stop-Process -Id $listener.OwningProcess -Force -ErrorAction Stop
            Write-Host "Stopped existing service on port ${Port}: pid=$($listener.OwningProcess)"
        }
        catch {
            Write-Host "Skip stopping port ${Port}: $($_.Exception.Message)"
        }
    }
}

function Start-MeetingServiceWindow {
    param(
        [string]$ScriptPath,
        [string]$ExtraArguments = ""
    )

    # Arguments are quoted manually because the project path contains spaces.
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = "powershell.exe"
    $psi.Arguments = "-NoExit -ExecutionPolicy Bypass -File `"$ScriptPath`" $ExtraArguments"
    $psi.WorkingDirectory = $ProjectRoot
    $psi.UseShellExecute = $true
    [System.Diagnostics.Process]::Start($psi) | Out-Null
}

if (-not $ReuseExisting) {
    Stop-MeetingServiceOnPort -Port 5173
    Stop-MeetingServiceOnPort -Port 8001
    Stop-MeetingServiceOnPort -Port 8100
    Start-Sleep -Seconds 1
}

if (Test-HttpEndpoint -Url "http://127.0.0.1:8100/v1/health") {
    Write-Host "Model service already healthy: http://127.0.0.1:8100"
}
else {
    Start-MeetingServiceWindow -ScriptPath $ModelScript -ExtraArguments "-Port 8100 -MockMode $ModelMockMode"
    Start-Sleep -Seconds 4
}

if (Test-HttpEndpoint -Url "http://127.0.0.1:8001/api/health") {
    Write-Host "Backend already healthy: http://127.0.0.1:8001"
}
else {
    Start-MeetingServiceWindow -ScriptPath $BackendScript
    Start-Sleep -Seconds 2
}

if (Test-HttpEndpoint -Url "http://127.0.0.1:5173") {
    Write-Host "Frontend already healthy: http://127.0.0.1:5173"
}
else {
    Start-MeetingServiceWindow -ScriptPath $FrontendScript
}

Write-Host "Model service: http://127.0.0.1:8100"
Write-Host "Backend: http://127.0.0.1:8001"
Write-Host "Frontend: http://127.0.0.1:5173"
