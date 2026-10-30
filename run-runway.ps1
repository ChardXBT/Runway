param(
    [switch]$ApiOnly,
    [switch]$WebOnly,
    [ValidateRange(5, 300)][int]$TimeoutSeconds = 90
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$root = $PSScriptRoot
$python = Join-Path $root '.venv\Scripts\python.exe'
$webRoot = Join-Path $root 'apps\web'
$next = Join-Path $webRoot 'node_modules\next\dist\bin\next'
$runtime = Join-Path $root 'data\runtime'
$null = New-Item -ItemType Directory -Force -Path $runtime
$lock = $null
$started = @()

function Save-PreviousLog([string]$Name, [long]$MaxBytes = 0) {
    # Keep one previous generation so a restart never erases the evidence of a crash.
    $path = Join-Path $runtime "$Name.log"
    try {
        $item = Get-Item -LiteralPath $path -ErrorAction Stop
        if ($item.Length -gt $MaxBytes) {
            Move-Item -LiteralPath $path -Destination (Join-Path $runtime "$Name.previous.log") -Force
        }
    } catch [System.Management.Automation.ItemNotFoundException] {
    } catch {
        Write-Host "Could not rotate $Name.log: $($_.Exception.Message)"
    }
}

function Write-Diagnostic([string]$Message) {
    $line = '{0:o} {1}' -f (Get-Date), $Message
    Add-Content -LiteralPath (Join-Path $runtime 'launcher.log') -Value $line
    Write-Host $line
}

function Test-Ready([string]$Component) {
    try {
        $port = if ($Component -eq 'api') { 8000 } else { 3000 }
        $health = Invoke-RestMethod -Uri "http://127.0.0.1:$port/health" -TimeoutSec 2
        return $health.product -eq 'Runway' -and $health.status -eq 'ok' -and (
            $Component -eq 'api' -or $health.component -eq 'web'
        )
    } catch { return $false }
}

function Get-Listener([int]$Port) {
    # A failed process/port query must not be mistaken for a free port.
    # Since KB5127936/KB5132402 (2026-10-29) Windows reports listening sockets
    # with a blank State, so -State Listen finds nothing. A listener is an
    # endpoint with no remote port whose State is blank or Listen; Bound and
    # closing rows are other apps' outbound sockets.
    $notListening = @('Bound', 'Closed', 'SynSent', 'SynReceived', 'Established', 'FinWait1',
        'FinWait2', 'CloseWait', 'Closing', 'LastAck', 'TimeWait', 'DeleteTCB')
    return @(Get-NetTCPConnection -ErrorAction Stop |
        Where-Object {
            $_.LocalPort -eq $Port -and [int]$_.RemotePort -eq 0 -and "$($_.State)" -notin $notListening
        })
}

function Test-Contains([string]$Text, [string]$Value) {
    # Windows paths are case-insensitive; a caller may spell this checkout differently.
    return $Text.IndexOf($Value, [System.StringComparison]::OrdinalIgnoreCase) -ge 0
}

function Test-Owned($Process, [string]$Component) {
    if (-not $Process -or -not $Process.CommandLine) { return $false }
    if ($Component -eq 'api') {
        return (Test-Contains $Process.CommandLine $python) -and
            (Test-Contains $Process.CommandLine 'runway.api.app:app')
    }
    return $Process.Name -eq 'node.exe' -and (Test-Contains $Process.CommandLine $next)
}

function Stop-StartedChild($Child) {
    # The venv python.exe is a redirector that spawns the real interpreter, which
    # owns the port. Kill() alone would orphan it, so stop this child's whole tree.
    $Child.Refresh()
    if ($Child.HasExited) { return }
    $taskkill = Join-Path $env:SystemRoot 'System32\taskkill.exe'
    & $taskkill /PID $Child.Id /T /F 2>&1 | Out-Null
    if (-not $Child.WaitForExit(10000)) { $Child.Kill() }
}

function Assert-PortOwner([string]$Component, [int]$Port) {
    foreach ($listener in (Get-Listener $Port)) {
        $owner = Get-CimInstance Win32_Process -Filter "ProcessId = $($listener.OwningProcess)"
        if (-not (Test-Owned $owner $Component)) {
            throw "Port $Port is occupied by an unrelated or unverifiable process ($($listener.OwningProcess)). Nothing was killed."
        }
    }
}

function Start-Component([string]$Component, [int]$Port) {
    Assert-PortOwner $Component $Port
    if (Test-Ready $Component) {
        Write-Diagnostic "$Component already ready on port $Port."
        return
    }
    if (@(Get-Listener $Port).Count -gt 0) {
        # An existing owned server may still be starting. Never kill it based on one failed probe.
        $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
        while ((Get-Date) -lt $deadline) {
            if (Test-Ready $Component) { return }
            Start-Sleep -Milliseconds 500
        }
        throw "$Component owns port $Port but is unhealthy. Inspect data/runtime/$Component.stderr.log before stopping it; active work is not terminated automatically."
    }
    $arguments = if ($Component -eq 'api') {
        '-m uvicorn runway.api.app:app --host 127.0.0.1 --port 8000'
    } else {
        '"{0}" start "{1}" --hostname 127.0.0.1 --port 3000' -f $next, $webRoot
    }
    $executable = if ($Component -eq 'api') { $python } else { $script:node }
    Save-PreviousLog "$Component.stdout"
    Save-PreviousLog "$Component.stderr"
    Write-Diagnostic "Starting $Component using $executable on port $Port."
    # Start-Process -Redirect* would create the server with handle inheritance on,
    # handing it this launcher's stdout/stderr and any handle a concurrent caller
    # leaked in. A caller capturing launcher output then blocked until Runway
    # stopped. Without -Redirect* Start-Process uses ShellExecute, which inherits
    # nothing; cmd.exe writes the component logs instead.
    $command = '/d /s /c ""{0}" {1} 1>"{2}" 2>"{3}""' -f $executable, $arguments,
        (Join-Path $runtime "$Component.stdout.log"), (Join-Path $runtime "$Component.stderr.log")
    $child = Start-Process -FilePath (Join-Path $env:SystemRoot 'System32\cmd.exe') `
        -ArgumentList $command -WorkingDirectory $root -WindowStyle Hidden -PassThru
    $null = $child.Handle # Retain the handle so rapid exits still have an exit code.
    $script:started += $child
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        $child.Refresh()
        if ($child.HasExited) {
            $child.WaitForExit()
            throw "$Component exited with code $($child.ExitCode). Inspect data/runtime/$Component.stderr.log."
        }
        if (Test-Ready $Component) {
            Assert-PortOwner $Component $Port
            Write-Diagnostic "$Component ready on port $Port (PID $($child.Id))."
            return
        }
        Start-Sleep -Milliseconds 500
    }
    throw "$Component readiness timed out after $TimeoutSeconds seconds. Inspect data/runtime/$Component.stderr.log."
}

try {
    if ($ApiOnly -and $WebOnly) { throw 'ApiOnly and WebOnly are mutually exclusive.' }
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds * 2 + 10)
    do {
        try {
            # An exclusive file handle works across interactive and scheduled Windows sessions.
            $lock = [System.IO.File]::Open((Join-Path $runtime 'launcher.lock'), 'OpenOrCreate', 'ReadWrite', 'None')
        } catch [System.IO.IOException] {
            if ((Get-Date) -ge $deadline) { throw 'Another Runway launch is still in progress.' }
            Start-Sleep -Milliseconds 300
        }
    } while ($null -eq $lock)
    # Scheduled watchdog runs append a line every few minutes; cap the log at about 1 MB.
    Save-PreviousLog 'launcher' 1MB
    if (-not $WebOnly -and -not (Test-Path -LiteralPath $python -PathType Leaf)) {
        throw 'Missing .venv/Scripts/python.exe. Install the documented Python environment first.'
    }
    if (-not $ApiOnly) {
        $nodeCommand = Get-Command node.exe -ErrorAction SilentlyContinue
        $script:node = if ($env:RUNWAY_NODE_EXE) { $env:RUNWAY_NODE_EXE }
            elseif ($nodeCommand) { $nodeCommand.Source }
            else { Join-Path $env:ProgramFiles 'nodejs\node.exe' }
        if (-not (Test-Path -LiteralPath $script:node -PathType Leaf)) {
            throw 'Node was not found. Install Node or set RUNWAY_NODE_EXE to its absolute executable path.'
        }
        if (-not (Test-Path -LiteralPath $next -PathType Leaf) -or
            -not (Test-Path -LiteralPath (Join-Path $webRoot '.next\BUILD_ID') -PathType Leaf)) {
            throw 'Production web build missing. Run npm ci and npm run web:build before launching.'
        }
    }
    # Check both ports before creating either child; never disturb another application.
    if (-not $WebOnly) { Assert-PortOwner 'api' 8000 }
    if (-not $ApiOnly) { Assert-PortOwner 'web' 3000 }
    if (-not $WebOnly) { Start-Component 'api' 8000 }
    if (-not $ApiOnly) { Start-Component 'web' 3000 }
    if ((-not $WebOnly -and -not (Test-Ready 'api')) -or
        (-not $ApiOnly -and -not (Test-Ready 'web'))) { throw 'Stack readiness was lost during startup.' }
    Write-Diagnostic 'Requested Runway services are ready. http://127.0.0.1:3000/review'
} catch {
    Write-Diagnostic "Launch failed: $($_.Exception.Message)"
    # Only roll back children created by this invocation, never pre-existing services.
    foreach ($child in $started) { Stop-StartedChild $child }
    exit 1
} finally {
    if ($null -ne $lock) { $lock.Dispose() }
}
