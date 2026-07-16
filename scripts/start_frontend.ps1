$ErrorActionPreference = "Stop"

# Frontend static server startup script.
# The frontend has no build step; Python's built-in static server is enough for local use.
$ProjectRoot = Resolve-Path "$PSScriptRoot\.."
$FrontendDir = Join-Path $ProjectRoot "frontend"
$VenvPython = Join-Path $ProjectRoot "backend\.venv\Scripts\python.exe"
$PythonExe = if (Test-Path $VenvPython) { $VenvPython } else { "python" }

Set-Location $FrontendDir
& $PythonExe -m http.server 5173 --bind 127.0.0.1
