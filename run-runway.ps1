param(
    [switch]$ApiOnly,
    [switch]$WebOnly
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Join-Path $root ".venv\Scripts\python.exe"

if (-not (Test-Path $python)) {
    throw "Missing .venv. Run: py -3.13 -m venv .venv; .\.venv\Scripts\python.exe -m pip install -e '.[dev]'"
}

if (-not $WebOnly) {
    Start-Process -FilePath $python -ArgumentList "-m", "uvicorn", "runway.api.app:app", "--host", "127.0.0.1", "--port", "8000" -WorkingDirectory $root -WindowStyle Hidden
}

if (-not $ApiOnly) {
    npm --prefix $root run web:dev
}
