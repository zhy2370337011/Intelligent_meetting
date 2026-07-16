$ErrorActionPreference = "Stop"

# Backend startup script.
# It always uses backend\.venv so the server does not depend on global Python packages.
$ProjectRoot = Resolve-Path "$PSScriptRoot\.."
$BackendDir = Join-Path $ProjectRoot "backend"
$PythonExe = Join-Path $BackendDir ".venv\Scripts\python.exe"

# Fail early with a clear message if dependencies have not been installed.
if (-not (Test-Path $PythonExe)) {
    throw "backend virtual environment not found. Run: python -m venv backend\.venv; backend\.venv\Scripts\python.exe -m pip install -r backend\requirements.txt"
}

Set-Location $BackendDir
$env:PYTHONPATH = $BackendDir

# Use 8001 by default because 8000 is often occupied by other local services.
# You can override it with MEETING_BACKEND_PORT when needed.
$Port = if ($env:MEETING_BACKEND_PORT) { $env:MEETING_BACKEND_PORT } else { "8001" }
& $PythonExe -m uvicorn app.main:app --host 0.0.0.0 --port $Port
