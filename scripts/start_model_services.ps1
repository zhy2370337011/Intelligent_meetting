param(
    [int]$Port = 8100,
    [string]$HostAddress = "127.0.0.1",
    [string]$MockMode = "false",
    [switch]$ForceRestart
)

$ErrorActionPreference = "Stop"

# Start the local model HTTP service used by the intelligent meeting backend.
# The service exposes VAD, voiceprint, diarization, and alignment proxy endpoints.
# Keep this script ASCII-only because Windows PowerShell 5 may parse UTF-8 files
# without BOM as the local ANSI code page, which can corrupt Chinese strings.
#
# Examples:
#   Real local models after dependencies and weights are installed:
#     .\scripts\start_model_services.ps1 -Port 8100 -MockMode false
#   Explicit mock diagnostics only (never a genuine enrollment runtime):
#     .\scripts\start_model_services.ps1 -Port 8100 -MockMode true

$RepoRoot = Split-Path -Parent $PSScriptRoot
$BackendDir = Join-Path $RepoRoot "backend"
$PythonExe = Join-Path $BackendDir ".venv\Scripts\python.exe"
$HealthUrl = "http://$HostAddress`:$Port/v1/health"
$ExpectedServiceIdentity = "intelligent-meeting-local-model-service"

function Get-ListeningPid {
    param(
        [int]$LocalPort
    )

    # Get-NetTCPConnection is available on the Windows Server versions used by
    # the target deployment and gives us the exact process that owns a port.
    # The function returns $null when the port is free, which lets the startup
    # script decide whether to launch uvicorn or reuse an existing service.
    $connection = Get-NetTCPConnection -LocalPort $LocalPort -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($connection) {
        return $connection.OwningProcess
    }
    return $null
}

function Test-ModelHealth {
    # A port can be occupied by the correct model service or by an unrelated
    # process. We only reuse the service when the health endpoint proves it is
    # the intelligent-meeting model service.
    try {
        return Invoke-RestMethod -Uri $HealthUrl -TimeoutSec 3
    }
    catch {
        return $null
    }
}

function Test-ExpectedModelHealth {
    param(
        $Health,
        [bool]$RequireRequestedMockMode = $true,
        [bool]$RequireEmbeddingCapability = $true
    )

    # A listening PID proves only port ownership. Reuse requires the stable model-service identity,
    # process health, and requested boolean mock mode so another local HTTP application is never
    # mistaken for this service. Force restart skips only the mode comparison because it replaces a
    # verified service with the requested mode; it never kills an unverified port owner.
    if ($null -eq $Health -or $Health.status -ne "ok" -or $Health.service -ne $ExpectedServiceIdentity) {
        return $false
    }
    if ($Health.mockMode -isnot [bool]) {
        return $false
    }
    if ($RequireRequestedMockMode) {
        $requestedMockMode = $MockMode -eq "true"
        if ($Health.mockMode -ne $requestedMockMode) {
            return $false
        }
    }

    # “CAM++ 模型已加载”并不等于业务后端依赖的 embedding 路由可用。旧版服务正是
    # 因为只报告模型 ready 而缺少 /v1/speakers/embedding，才会被启动脚本长期复用。
    # 正常复用必须看到新健康契约；ForceRestart 会把本参数设为 false，以便安全识别并
    # 替换同一产品的旧进程，而不是因为旧进程缺少新字段就无法升级。
    if ($RequireEmbeddingCapability) {
        $voiceprint = $Health.capabilities.voiceprint
        if ($null -eq $voiceprint -or $voiceprint.embeddingReady -ne $true) {
            return $false
        }
    }
    return $true
}

# Fail early with an ASCII error message so startup failures are readable even on
# Windows consoles whose code page is not UTF-8.
if (-not (Test-Path $PythonExe)) {
    throw "backend virtual environment not found. Run backend dependency installation first."
}

if ($MockMode -notin @("true", "false")) {
    throw "MockMode must be true or false. The default is false so normal startup never reports mock voiceprints as real registrations."
}

$existingPid = Get-ListeningPid -LocalPort $Port
if ($existingPid -and -not $ForceRestart) {
    $health = Test-ModelHealth
    if (Test-ExpectedModelHealth -Health $health -RequireRequestedMockMode $true -RequireEmbeddingCapability $true) {
        Write-Host "Model service already running on $HealthUrl, pid=$existingPid, mockMode=$($health.mockMode). Reusing it."
        Write-Host "Use -ForceRestart if you need to restart it with a different MockMode."
        exit 0
    }

    throw "port $Port is already used by pid=$existingPid, but $HealthUrl did not prove the expected service identity, status, and requested MockMode. Refusing reuse."
}

if ($existingPid -and $ForceRestart) {
    $health = Test-ModelHealth
    if (-not (Test-ExpectedModelHealth -Health $health -RequireRequestedMockMode $false -RequireEmbeddingCapability $false)) {
        throw "port $Port is owned by pid=$existingPid, but $HealthUrl is not a verified intelligent-meeting model service. Refusing to force-stop an unrelated process."
    }
    Write-Host "Stopping existing process pid=$existingPid on port $Port before restarting model service."
    Stop-Process -Id $existingPid -Force
    Start-Sleep -Seconds 1
}

$env:LOCAL_MODEL_MOCK_MODE = $MockMode
$env:PYTHONPATH = $BackendDir

Set-Location $BackendDir

& $PythonExe -m uvicorn model_services.local_models_api:app --host $HostAddress --port $Port
