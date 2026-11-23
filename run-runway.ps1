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

function Get-LaunchSummary([string]$Root, [bool]$Api, [bool]$Web, [int]$Timeout) {
    # One line naming the code and request behind every launch. The commit is
    # best-effort: a missing git never blocks starting Runway.
    $revision = 'unknown'
    try {
        $git = Get-Command git.exe -ErrorAction Stop
        $output = & $git.Source -C $Root rev-parse --short=12 HEAD 2>$null
        if ($LASTEXITCODE -eq 0 -and "$output".Trim()) { $revision = "$output".Trim() }
    } catch { }
    $components = @()
    if ($Api) { $components += 'api' }
    if ($Web) { $components += 'web' }
    return 'Launch requested: git={0} components={1} timeout={2}s pid={3}' -f $revision,
        ($components -join ','), $Timeout, $PID
}

function Get-Health([string]$Component) {
    try {
        $port = if ($Component -eq 'api') { 8000 } else { 3000 }
        return Invoke-RestMethod -Uri "http://127.0.0.1:$port/health" -TimeoutSec 2
    } catch { return $null }
}

function Get-HealthField($Health, [string]$Name) {
    # '' when the field is missing: older APIs lack the newer identity fields.
    if ($null -eq $Health) { return '' }
    if ($Health -is [System.Collections.IDictionary]) {
        if ($Health.Contains($Name)) { return "$($Health[$Name])" }
        return ''
    }
    if ($Health.PSObject.Properties[$Name]) { return "$($Health.$Name)" }
    return ''
}

function Test-Ready([string]$Component) {
    $health = Get-Health $Component
    try {
        return $health.product -eq 'Runway' -and $health.status -eq 'ok' -and (
            $Component -eq 'api' -or $health.component -eq 'web'
        )
    } catch { return $false }
}

function Get-ApiRestartReason($Health, $Status) {
    # Why a healthy API on port 8000 must be replaced before the worker starts
    # and before the database is migrated, or '' when it is current.
    if ((Get-HealthField $Health 'sync_scheduler') -ne 'worker') {
        return 'it is older code that runs the in-process sync scheduler'
    }
    $running = Get-HealthField $Health 'revision'
    # "unknown" (no git) on either side proves nothing; the schema check below still applies.
    if ($running -and $running -ne 'unknown' -and $Status.revision -ne 'unknown' -and
        $running -ne $Status.revision) {
        return "it runs revision $running and the checkout is at $($Status.revision)"
    }
    $schema = Get-HealthField $Health 'migration_head'
    if ($schema -ne $Status.head) {
        return "it expects schema $schema and the checkout's migration head is $($Status.head)"
    }
    if (-not $Status.up_to_date) {
        return "the database is at $($Status.current), behind the migration head $($Status.head)"
    }
    return ''
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

function Get-ParentProcess($Process) {
    if (-not $Process.PSObject.Properties['ParentProcessId']) { return $null }
    return Get-CimInstance Win32_Process -Filter "ProcessId = $($Process.ParentProcessId)" -ErrorAction SilentlyContinue
}

function Test-Owned($Process, [string]$Component) {
    if (-not $Process -or -not $Process.CommandLine) { return $false }
    if ($Component -eq 'api') {
        $module = 'runway.api.app:app'
        if (-not (Test-Contains $Process.CommandLine $module)) { return $false }
        if (Test-Contains $Process.CommandLine $python) { return $true }
        # The venv python.exe is a redirector. On python.org installs it starts the
        # base interpreter, whose command line names the base path, not this
        # checkout. That process is ours only if its parent is our venv redirector
        # running the same module.
        $parent = Get-ParentProcess $Process
        if (-not $parent -or -not $parent.PSObject.Properties['CommandLine'] -or -not $parent.CommandLine) {
            return $false
        }
        return (Test-Contains $parent.CommandLine $python) -and
            (Test-Contains $parent.CommandLine $module)
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

function Start-HiddenCommand([string]$Command) {
    # Every child goes through cmd.exe, which writes its logs. Start-Process
    # -Redirect* would create the child with handle inheritance on, handing it
    # this launcher's stdout/stderr and any handle a concurrent caller leaked in;
    # a caller capturing launcher output then blocked until Runway stopped.
    # Without -Redirect* Start-Process uses ShellExecute, which inherits nothing.
    # Each child gets its own hidden console, so it can be asked to stop with
    # Ctrl+Break later.
    $child = Start-Process -FilePath (Join-Path $env:SystemRoot 'System32\cmd.exe') `
        -ArgumentList $Command -WorkingDirectory $root -WindowStyle Hidden -PassThru
    $null = $child.Handle # Retain the handle so rapid exits still have an exit code.
    return $child
}

function Get-RuntimeStatus {
    # One read-only Python query: the database revision against this checkout's
    # migration head, the checkout revision, and where the worker keeps its lock
    # and PID record. A failed query stops the launch; it never looks like
    # "nothing to do".
    $out = Join-Path $runtime 'status.json'
    $err = Join-Path $runtime 'status.stderr.log'
    Remove-Item -LiteralPath $out -Force -ErrorAction SilentlyContinue
    $command = '/d /s /c ""{0}" -m runway.operations.launch_status 1>"{1}" 2>"{2}""' -f $python, $out, $err
    $query = Start-HiddenCommand $command
    if (-not $query.WaitForExit(120000)) {
        throw 'The runtime status query did not finish within 2 minutes. Inspect data/runtime/status.stderr.log.'
    }
    $query.WaitForExit()
    if ($query.ExitCode -ne 0) {
        throw "The runtime status query failed with code $($query.ExitCode). Inspect data/runtime/status.stderr.log."
    }
    $status = Get-Content -LiteralPath $out -Raw | ConvertFrom-Json
    foreach ($name in 'current', 'head', 'up_to_date', 'revision', 'worker_lock', 'worker_pid') {
        if (-not $status.PSObject.Properties[$name]) { throw "The runtime status lacks '$name'." }
    }
    return $status
}

function Request-ConsoleStop([int]$ProcessId) {
    # Ctrl+Break on the process's own hidden console (Windows has no SIGTERM;
    # Ctrl+C can be disabled by a parent, Ctrl+Break cannot): uvicorn and
    # `runway worker` handle SIGBREAK and shut down gracefully.
    # It must run in a separate process because attaching to another console
    # detaches the caller from its own.
    & $python -m runway.operations.console_stop $ProcessId 2>&1 | Out-Null
    return $LASTEXITCODE -eq 0
}

function Test-ProcessAlive([int]$ProcessId) {
    return $null -ne (Get-Process -Id $ProcessId -ErrorAction SilentlyContinue)
}

function Stop-ByConsole([int]$ProcessId, [string]$Name, [int[]]$WaitSeconds = @(45, 15)) {
    # Ask, wait, ask again. uvicorn bounds its graceful shutdown (open event
    # streams) with --timeout-graceful-shutdown. Nothing is ever force-killed.
    # Returns $true once it exited.
    foreach ($wait in $WaitSeconds) {
        if (-not (Test-ProcessAlive $ProcessId)) { return $true }
        Write-Diagnostic "Asking $Name (PID $ProcessId) to stop with Ctrl+Break."
        if (-not (Request-ConsoleStop $ProcessId)) {
            Write-Diagnostic "Could not deliver Ctrl+Break to $Name (PID $ProcessId)."
            return $false
        }
        $deadline = (Get-Date).AddSeconds($wait)
        while ((Get-Date) -lt $deadline) {
            if (-not (Test-ProcessAlive $ProcessId)) {
                Write-Diagnostic "$Name (PID $ProcessId) stopped."
                return $true
            }
            Start-Sleep -Milliseconds 250
        }
    }
    return -not (Test-ProcessAlive $ProcessId)
}

function Invoke-DatabaseUpgrade {
    # The API refuses to serve a database behind the migration head and never
    # migrates at import, so the launcher upgrades it explicitly first.
    Save-PreviousLog 'db-upgrade'
    $log = Join-Path $runtime 'db-upgrade.log'
    Write-Diagnostic "Upgrading the database using $python."
    $command = '/d /s /c ""{0}" -m runway.cli.db_commands upgrade 1>"{1}" 2>&1"' -f $python, $log
    $upgrade = Start-HiddenCommand $command
    # A migration is never killed midway; a slow one is reported and left to finish.
    if (-not $upgrade.WaitForExit(900000)) {
        throw 'Database upgrade is still running after 15 minutes. Inspect data/runtime/db-upgrade.log; the API was not started.'
    }
    $upgrade.WaitForExit()
    if ($upgrade.ExitCode -ne 0) {
        throw "Database upgrade failed with code $($upgrade.ExitCode). Inspect data/runtime/db-upgrade.log; the API was not started."
    }
    Write-Diagnostic 'Database is at the migration head.'
}

function Test-WorkerLockHeld([string]$LockPath) {
    # `runway worker` holds a byte-range lock on worker.lock for its whole life
    # and the OS drops it when the process exits, so a held lock is a live
    # worker however it was started (venv python, runway.exe, by hand). A failed
    # check throws: it must never look like "no worker", or a second one starts.
    if (-not (Test-Path -LiteralPath $LockPath -PathType Leaf)) { return $false }
    $stream = [System.IO.File]::Open($LockPath, 'Open', 'ReadWrite', 'ReadWrite')
    try {
        try { $stream.Lock(0, 1) } catch [System.IO.IOException] { return $true }
        $stream.Unlock(0, 1)
        return $false
    } finally {
        $stream.Dispose()
    }
}

function Get-WorkerRecord([string]$PidPath) {
    # data/runtime/worker.pid: {"pid", "revision", "started_at"}, written by the
    # worker while it holds the lock and removed when it exits.
    try {
        $record = Get-Content -LiteralPath $PidPath -Raw -ErrorAction Stop | ConvertFrom-Json
        if ($record -and $record.PSObject.Properties['pid']) { return $record }
    } catch { }
    return $null
}

function Start-Worker([string]$LockPath, [string]$PidPath, [double]$GraceSeconds = 5) {
    # The worker runs scheduled platform sync and queued jobs. It has no port or
    # health endpoint, and it refuses to run twice for one data directory.
    if (Test-WorkerLockHeld $LockPath) {
        $record = Get-WorkerRecord $PidPath
        $id = if ($record) { $record.pid } else { 'unknown' }
        Write-Diagnostic "worker already running (PID $id)."
        return
    }
    Save-PreviousLog 'worker'
    $log = Join-Path $runtime 'worker.log'
    Write-Diagnostic "Starting worker using $python."
    $command = '/d /s /c ""{0}" -m runway.cli.worker_commands 1>"{1}" 2>&1"' -f $python, $log
    $child = Start-HiddenCommand $command
    $script:started += $child
    # A worker that is still running after the grace period passed its startup
    # checks (database at head, single-instance lock).
    $deadline = (Get-Date).AddSeconds($GraceSeconds)
    while ((Get-Date) -lt $deadline) {
        $child.Refresh()
        if ($child.HasExited) {
            $child.WaitForExit()
            throw "worker exited with code $($child.ExitCode). Inspect data/runtime/worker.log."
        }
        Start-Sleep -Milliseconds 250
    }
    Write-Diagnostic "worker running (PID $($child.Id))."
}

function Stop-WorkerForRestart($Status) {
    # The worker is identified by its lock and PID record only. Returns $true
    # when no worker runs any more.
    if (-not (Test-WorkerLockHeld $Status.worker_lock)) { return $true }
    $record = Get-WorkerRecord $Status.worker_pid
    if (-not $record -or -not (Test-ProcessAlive ([int]$record.pid))) {
        Write-Diagnostic 'worker.lock is held but worker.pid names no running process; stop that worker by hand.'
        return $false
    }
    if (-not (Stop-ByConsole ([int]$record.pid) 'worker')) {
        Write-Diagnostic "worker (PID $($record.pid)) did not stop after Ctrl+Break; nothing was killed."
        return $false
    }
    # The OS releases the lock with the process.
    return -not (Test-WorkerLockHeld $Status.worker_lock)
}

function Stop-StaleApi($Status) {
    # Deploy: a healthy API that is older code (or serves a database behind
    # head) is stopped cleanly, worker first, so the upgrade runs with nothing
    # attached and the old in-process scheduler never runs beside the worker.
    # Returns $false when it had to be left running (degraded: no upgrade, no
    # worker); throws when it neither stopped nor stayed healthy.
    $health = Get-Health 'api'
    if (-not (Test-Ready 'api')) { return $true }
    $reason = Get-ApiRestartReason $health $Status
    if (-not $reason) { return $true }
    Write-Diagnostic "The running API must restart: $reason."
    if (-not (Stop-WorkerForRestart $Status)) {
        Write-Diagnostic 'Deploy incomplete: the worker did not stop, so the API was left running on its older code, the database was not upgraded and no worker was started.'
        return $false
    }
    Assert-PortOwner 'api' 8000
    foreach ($listener in (Get-Listener 8000)) {
        $owner = [int]$listener.OwningProcess
        if (Stop-ByConsole $owner 'api') { continue }
        if (Test-Ready 'api') {
            Write-Diagnostic "Deploy incomplete: the API (PID $owner) did not stop, so it was left running on its older code, the database was not upgraded and no worker was started. Stop it by hand, then run the launcher again."
            return $false
        }
        throw "The API (PID $owner) neither stopped nor stayed healthy after Ctrl+Break; nothing was killed. Inspect data/runtime/api.stderr.log."
    }
    return $true
}

function Test-WorkerStale($Status) {
    if (-not (Test-WorkerLockHeld $Status.worker_lock)) { return $false }
    $record = Get-WorkerRecord $Status.worker_pid
    if (-not $record -or -not $record.PSObject.Properties['revision']) { return $false }
    $running = "$($record.revision)"
    return [bool]($running -and $running -ne 'unknown' -and $Status.revision -ne 'unknown' -and
        $running -ne $Status.revision)
}

function Start-ApiAndWorker($Status) {
    if (-not (Stop-StaleApi $Status)) { return }
    if (-not $Status.up_to_date) {
        # `runway db upgrade` refuses to migrate under a running worker.
        if (-not (Stop-WorkerForRestart $Status)) {
            throw 'The database must be upgraded but the running worker did not stop; nothing was killed. Stop it by hand (PID in data/runtime/worker.pid).'
        }
    } elseif (Test-WorkerStale $Status) {
        Write-Diagnostic "The running worker is older code than revision $($Status.revision); restarting it."
        if (-not (Stop-WorkerForRestart $Status)) { Write-Diagnostic 'The older worker was left running.' }
    }
    Start-Component 'api' 8000
    # The old in-process scheduler and the worker must never both sync: the
    # worker starts only beside an API that reports sync_scheduler=worker.
    if ((Get-HealthField (Get-Health 'api') 'sync_scheduler') -ne 'worker') {
        Write-Diagnostic 'worker not started: the API on port 8000 still runs the in-process sync scheduler.'
        return
    }
    Start-WorkerBesideApi $Status.worker_lock $Status.worker_pid
}

function Start-WorkerBesideApi([string]$LockPath, [string]$PidPath) {
    # The API and web are what the Supervisor health-checks. A worker that
    # cannot start is logged and left for the next launcher run to retry; it
    # never fails the launch or stops a healthy API.
    try {
        Start-Worker $LockPath $PidPath
    } catch {
        Write-Diagnostic "worker did not start: $($_.Exception.Message) The API stays up; the next launcher run retries the worker."
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
        # The graceful-shutdown bound keeps an open event stream from holding a
        # Ctrl+Break stop (deploy restart) forever.
        '-m uvicorn runway.api.app:app --host 127.0.0.1 --port 8000 --timeout-graceful-shutdown 20'
    } else {
        '"{0}" start "{1}" --hostname 127.0.0.1 --port 3000' -f $next, $webRoot
    }
    $executable = if ($Component -eq 'api') { $python } else { $script:node }
    if ($Component -eq 'api') { Invoke-DatabaseUpgrade }
    Save-PreviousLog "$Component.stdout"
    Save-PreviousLog "$Component.stderr"
    Write-Diagnostic "Starting $Component using $executable on port $Port."
    $command = '/d /s /c ""{0}" {1} 1>"{2}" 2>"{3}""' -f $executable, $arguments,
        (Join-Path $runtime "$Component.stdout.log"), (Join-Path $runtime "$Component.stderr.log")
    $child = Start-HiddenCommand $command
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
    Write-Diagnostic (Get-LaunchSummary $root (-not $WebOnly) (-not $ApiOnly) $TimeoutSeconds)
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
    if (-not $WebOnly) { Start-ApiAndWorker (Get-RuntimeStatus) }
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
