$ErrorActionPreference = "Stop"

# Run backend unit tests with the same virtual environment used by the API server.
# Keep this script ASCII-only. Windows PowerShell 5 can parse UTF-8 files without
# a BOM as the local ANSI code page, which may corrupt non-ASCII comments and
# accidentally hide the next PowerShell statement.
$ProjectRoot = Resolve-Path "$PSScriptRoot\.."
$BackendDir = Join-Path $ProjectRoot "backend"
$PythonExe = Join-Path $BackendDir ".venv\Scripts\python.exe"

if (-not (Test-Path $PythonExe)) {
    throw "backend virtual environment not found."
}

Set-Location $ProjectRoot
$env:PYTHONPATH = $BackendDir

# Unit tests use fake audio bytes and fake HTTP responses. They verify API
# contracts, persistence, and job state transitions, not real model inference.
# These overrides prevent backend/.env from making tests call real CAM++ / VAD /
# ASR services when local integration is configured for real model mode.
$env:MODEL_MOCK_MODE = "true"
$env:LOCAL_MODEL_MOCK_MODE = "true"
$env:ASR_GATEWAY_MODE = "mock"
$env:LLM_WORKFLOW_MODE = "mock"
# ``llm_workflow.py`` gates the current DeepSeek client on AI_MOCK_MODE rather than the legacy
# LLM_WORKFLOW_MODE alias.  Set both names because backend/.env may deliberately enable a real
# key for local product work, while this offline regression command must never wait for or bill an
# external model provider.
$env:AI_MOCK_MODE = "true"
# Unit fixtures contain intentionally fake audio bytes. Explicitly blank every live model endpoint
# so a developer's running CAM++/VAD service cannot consume those bytes and make test outcomes depend
# on local process state; real-service behavior is covered by smoke_verify_system.py instead.
$env:VAD_GATEWAY_BASE_URL = ""
$env:VOICEPRINT_GATEWAY_BASE_URL = ""
$env:ALIGNMENT_GATEWAY_BASE_URL = ""

Write-Host "Test model mode: MODEL_MOCK_MODE=$env:MODEL_MOCK_MODE ASR_GATEWAY_MODE=$env:ASR_GATEWAY_MODE AI_MOCK_MODE=$env:AI_MOCK_MODE"
# Discovering the full backend suite includes model-client contract tests and the smoke reporter's
# pure unit tests.  The environment overrides above keep every one of those tests offline: a mock
# response can prove API behavior, but the dedicated smoke capability test proves it never reports
# a real CAM++ or ForcedAligner deployment as ready.
$FailureExitCode = 0
& $PythonExe -m unittest discover -s backend/tests
if ($LASTEXITCODE -ne 0) {
    # PowerShell does not turn a native process's non-zero exit code into a terminating error by
    # itself. Preserve it explicitly so CI and callers cannot mistake a failing unittest suite for
    # a successful regression run while we still continue to execute independent frontend checks.
    $FailureExitCode = $LASTEXITCODE
}

# This repository deliberately keeps the frontend dependency-free.  Parse each browser script and
# run its DOM/static contract suite directly with Node instead of starting Vite, Chrome, ASR, or a
# local-model service.  That keeps ``run_tests.ps1`` deterministic on a fresh developer machine.
$NodeCommand = Get-Command node -ErrorAction SilentlyContinue
if (-not $NodeCommand) {
    throw "Node.js is required for frontend static verification."
}

Write-Host "Running frontend syntax and static product contracts"
& $NodeCommand.Source --check frontend/app.js
if ($LASTEXITCODE -ne 0 -and $FailureExitCode -eq 0) { $FailureExitCode = $LASTEXITCODE }
& $NodeCommand.Source --check frontend/prototype_spec_test.mjs
if ($LASTEXITCODE -ne 0 -and $FailureExitCode -eq 0) { $FailureExitCode = $LASTEXITCODE }
& $NodeCommand.Source frontend/prototype_spec_test.mjs
if ($LASTEXITCODE -ne 0 -and $FailureExitCode -eq 0) { $FailureExitCode = $LASTEXITCODE }

if ($FailureExitCode -ne 0) {
    exit $FailureExitCode
}
