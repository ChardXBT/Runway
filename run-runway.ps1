param(
    [switch]$ApiOnly,
    [switch]$WebOnly
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Join-Path $root ".venv\Scripts\python.exe"
$apiUrl = "http://127.0.0.1:8000/health"
$webUrl = "http://127.0.0.1:3000/review"

if (-not (Test-Path $python)) {
    throw "Missing .venv. Run: py -3.13 -m venv .venv; .\.venv\Scripts\python.exe -m pip install -e '.[dev]'"
}

function Test-RunwayApi {
    try {
        $health = Invoke-RestMethod -Uri $apiUrl -TimeoutSec 2
        return $health.product -eq "Runway"
    }
    catch {
        return $false
    }
}

function Test-RunwayWeb {
    try {
        return (Invoke-WebRequest -UseBasicParsing -Uri $webUrl -TimeoutSec 2).StatusCode -eq 200
    }
    catch {
        return $false
    }
}

function Test-PortInUse([int]$Port) {
    return $null -ne (
        Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue |
            Where-Object { $_.LocalPort -eq $Port } |
            Select-Object -First 1
    )
}

function Get-PortProcessId([int]$Port) {
    $listener = (
        Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue |
            Where-Object { $_.LocalPort -eq $Port } |
            Select-Object -First 1
    )
    return $listener.OwningProcess
}

function Test-RunwayWebProcess([int]$ProcessId) {
    if (-not $ProcessId) {
        return $false
    }
    try {
        $process = Get-CimInstance Win32_Process -Filter "ProcessId = $ProcessId"
        $webRoot = Join-Path $root "apps\web"
        return (
            $process.Name -eq "node.exe" -and
            $process.CommandLine -like "*$webRoot*"
        )
    }
    catch {
        return $false
    }
}

if (-not $WebOnly) {
    if (Test-RunwayApi) {
        Write-Host "Runway API is already running at http://127.0.0.1:8000"
    }
    elseif (Test-PortInUse 8000) {
        throw "Port 8000 is occupied by another program. Stop that program, then try again."
    }
    else {
        Start-Process -FilePath $python -ArgumentList "-m", "uvicorn", "runway.api.app:app", "--host", "127.0.0.1", "--port", "8000" -WorkingDirectory $root -WindowStyle Hidden
        for ($attempt = 0; $attempt -lt 30; $attempt++) {
            if (Test-RunwayApi) {
                break
            }
            Start-Sleep -Milliseconds 500
        }
        if (-not (Test-RunwayApi)) {
            throw "Runway API did not become ready at http://127.0.0.1:8000"
        }
        Write-Host "Runway API started at http://127.0.0.1:8000"
    }
}

if (-not $ApiOnly) {
    if (Test-RunwayWeb) {
        Write-Host "Runway is already running at $webUrl"
    }
    elseif (Test-PortInUse 3000) {
        $webProcessId = Get-PortProcessId 3000
        if (Test-RunwayWebProcess $webProcessId) {
            Write-Host "Runway web process is not responding; restarting it."
            Stop-Process -Id $webProcessId -Force
            Start-Sleep -Milliseconds 500
        }
        else {
            throw "Port 3000 is occupied by another program. Stop that program, then try again."
        }
    }
    if (-not (Test-RunwayWeb)) {
        Write-Host "Starting Runway at $webUrl"
        npm --prefix $root run web:dev
    }
}
