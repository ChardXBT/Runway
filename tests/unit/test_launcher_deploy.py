# ruff: noqa: E501  (embedded PowerShell scripts)
"""Behaviour of run-runway.ps1 on deploy, with a fake process runner.

The launcher's functions are loaded from its AST (as in test_windows_launcher.py)
and every process start, Ctrl+C request, port and health probe is replaced by a
fake "world", so these tests bind no port and start no Runway process. They check
what the launcher does, in which order: stop an older API (and the worker) before
`runway db upgrade`, start the worker only beside an API that runs no in-process
scheduler, and never fail the launch or stop the API for a worker that cannot start.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

LAUNCHER = Path(__file__).resolve().parents[2] / "run-runway.ps1"

HARNESS = r"""
param([string]$Launcher, [string]$Runtime, [string]$Scenario)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$ast = [System.Management.Automation.Language.Parser]::ParseFile($Launcher, [ref]$null, [ref]$null)
$ast.FindAll({ param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst]
}, $false) | ForEach-Object { Invoke-Expression $_.Extent.Text }
$realGetWorkerRecord = ${function:Get-WorkerRecord}
function Assert($Value, $Label) { if (-not $Value) { throw "$Label. events: $($world.events -join ' | '). diagnostics: $($diagnostics -join ' | ')" } }

$root = $Runtime
$runtime = $Runtime
$python = 'C:\repo\.venv\Scripts\python.exe'
$TimeoutSeconds = 10
$started = @()
$diagnostics = New-Object System.Collections.Generic.List[string]
function Write-Diagnostic([string]$Message) { $script:diagnostics.Add($Message) }

# ---- the fake world ---------------------------------------------------------
# api: 'down' | 'legacy' (pre-expansion: no identity fields, in-process scheduler)
#      | 'previous' (expansion code at an older revision) | 'current'
$world = @{
    api = 'down'; apiStops = $true; db = 'head'
    worker = $false; workerPid = 77; workerRevision = 'abc123def456'; workerStops = $true
    workerFails = $false; workerIsRunway = $true; portOwnerOk = $true; events = New-Object System.Collections.Generic.List[string]
}
$status = [pscustomobject]@{
    current = '0016_shared_contracts'; head = '0016_shared_contracts'; up_to_date = $true
    revision = 'abc123def456'; worker_lock = 'worker.lock'; worker_pid = 'worker.pid'
    known_revision = $true
}
function Set-DatabaseAhead {
    $status.current = '0099_from_newer_code'
    $status.up_to_date = $false
    $status.known_revision = $false
}
function Set-DatabaseBehind {
    $world.db = 'behind'
    $status.current = '0012_creator_platforms'
    $status.up_to_date = $false
}
# Ownership rules are covered in test_windows_launcher.py; here only the order matters.
function Assert-PortOwner([string]$Component, [int]$Port) {
    $world.events.Add("check owner $Port")
    if (-not $world.portOwnerOk) { throw "port $Port is owned by another program" }
}
function Test-RunwayWorkerProcess([int]$ProcessId) { return $world.workerIsRunway }
function Get-Listener([int]$Port) {
    if ($Port -eq 8000 -and $world.api -ne 'down') { return @([pscustomobject]@{ OwningProcess = 500 }) }
    return @()
}
function Invoke-RestMethod([string]$Uri, [int]$TimeoutSec) {
    switch ($world.api) {
        'legacy' { return @{ product = 'Runway'; status = 'ok'; platform_provider = 'mock' } }
        'previous' { return @{ product = 'Runway'; status = 'ok'; revision = '0ld0ld0ld0ld'; migration_head = '0016_shared_contracts'; sync_scheduler = 'worker' } }
        'current' { return @{ product = 'Runway'; status = 'ok'; revision = 'abc123def456'; migration_head = '0016_shared_contracts'; sync_scheduler = 'worker' } }
        default { throw 'connection refused' }
    }
}
function Test-WorkerLockHeld([string]$LockPath) { return $world.worker }
function Get-WorkerRecord([string]$PidPath) {
    if (-not $world.worker) { return $null }
    return [pscustomobject]@{ pid = $world.workerPid; revision = $world.workerRevision; started_at = 'x' }
}
function Test-ProcessAlive([int]$ProcessId) {
    if ($ProcessId -eq 500) { return $world.api -ne 'down' }
    if ($ProcessId -eq $world.workerPid) { return $world.worker }
    return $false
}
function Request-ConsoleStop([int]$ProcessId) {
    $world.events.Add("ctrl-break $ProcessId")
    if ($ProcessId -eq 500 -and $world.apiStops) { $world.api = 'down' }
    if ($ProcessId -eq $world.workerPid -and $world.workerStops) { $world.worker = $false }
    return $true
}
function New-FakeProcess([bool]$Exited, [int]$ExitCode) {
    $process = [pscustomobject]@{ Id = 900 + $world.events.Count; HasExited = $Exited; ExitCode = $ExitCode; Handle = 1 }
    $process | Add-Member -MemberType ScriptMethod -Name Refresh -Value { }
    # Like Process.WaitForExit: the timeout overload returns a bool, the other nothing.
    $process | Add-Member -MemberType ScriptMethod -Name WaitForExit -Value { param($Milliseconds) if ($null -ne $Milliseconds) { return $true } }
    $process | Add-Member -MemberType ScriptMethod -Name Kill -Value { }
    return $process
}
# The fake process runner: every child the launcher starts goes through here.
function Start-HiddenCommand([string]$Command) {
    if ($Command -match 'runway\.cli\.db_commands upgrade') {
        if ($world.worker) { $world.events.Add('upgrade refused: worker running'); return New-FakeProcess $true 3 }
        $world.events.Add('db upgrade'); $world.db = 'head'
        return New-FakeProcess $true 0
    }
    if ($Command -match 'uvicorn runway\.api\.app:app') {
        if ($world.db -ne 'head') { $world.events.Add('api refused: database behind'); return New-FakeProcess $true 1 }
        $world.events.Add('start api'); $world.api = 'current'
        return New-FakeProcess $false 0
    }
    if ($Command -match 'runway\.cli\.worker_commands') {
        $world.events.Add('start worker')
        if ($world.workerFails) { return New-FakeProcess $true 3 }
        $world.worker = $true; $world.workerPid = 78; $world.workerRevision = 'abc123def456'
        return New-FakeProcess $false 0
    }
    throw "unexpected command: $Command"
}
function Stop-StartedChild($Child) { $world.events.Add('rollback') }
# Real Stop-ByConsole, with short waits.
$realStopByConsole = ${function:Stop-ByConsole}
function Stop-ByConsole([int]$ProcessId, [string]$Name) { & $realStopByConsole $ProcessId $Name @(1, 1) }
function Events { return ($world.events -join ' | ') }

switch ($Scenario) {
    'deploy-over-legacy' {
        # The first deploy: the pre-expansion API is healthy on the old schema.
        $world.api = 'legacy'; Set-DatabaseBehind
        Start-ApiAndWorker $status
        Assert ((Events) -eq 'check owner 8000 | check owner 8000 | ctrl-break 500 | check owner 8000 | db upgrade | start api | check owner 8000 | start worker') 'deploy order'
        Assert (@($diagnostics) -like 'The running API must restart: it is older code that runs the in-process sync scheduler*') 'restart reason'
    }
    'deploy-over-previous-revision' {
        # A later deploy: API and worker are expansion code at an older revision.
        $world.api = 'previous'; $world.worker = $true; $world.workerRevision = '0ld0ld0ld0ld'
        Start-ApiAndWorker $status
        Assert ((Events) -eq 'check owner 8000 | ctrl-break 77 | check owner 8000 | ctrl-break 500 | check owner 8000 | db upgrade | start api | check owner 8000 | start worker') 'worker stops before the API'
    }
    'behind-with-api-down-and-worker-up' {
        $world.worker = $true; Set-DatabaseBehind
        Start-ApiAndWorker $status
        Assert ((Events) -eq 'ctrl-break 77 | check owner 8000 | db upgrade | start api | check owner 8000 | start worker') 'worker stops before the upgrade'
    }
    'current-stack-is-left-alone' {
        $world.api = 'current'; $world.worker = $true
        Start-ApiAndWorker $status
        Assert ((Events) -eq 'check owner 8000') 'nothing is started or stopped'
        Assert (@($diagnostics) -contains 'api already ready on port 8000.') 'api kept'
        Assert (@($diagnostics) -contains 'worker already running (PID 77).') 'worker kept'
    }
    'worker-failure-keeps-the-api' {
        $world.api = 'legacy'; Set-DatabaseBehind; $world.workerFails = $true
        Start-ApiAndWorker $status
        Assert ((Events) -eq 'check owner 8000 | check owner 8000 | ctrl-break 500 | check owner 8000 | db upgrade | start api | check owner 8000 | start worker') 'order'
        Assert ($world.api -eq 'current') 'the API stays up'
        Assert (@($diagnostics) -like 'worker did not start: worker exited with code 3*The API stays up*') 'worker failure logged'
    }
    'stubborn-legacy-api-is-left-running' {
        $world.api = 'legacy'; Set-DatabaseBehind; $world.apiStops = $false
        Start-ApiAndWorker $status
        Assert ((Events) -eq 'check owner 8000 | check owner 8000 | ctrl-break 500 | ctrl-break 500') 'asked twice, never killed, nothing else'
        Assert ($world.db -eq 'behind') 'no upgrade under a running API'
        Assert (@($diagnostics) -like 'Deploy incomplete: the API (PID 500) did not stop*') 'reported'
    }
    'stubborn-worker-blocks-the-deploy' {
        $world.api = 'previous'; $world.worker = $true; $world.workerStops = $false
        Start-ApiAndWorker $status
        Assert ((Events) -eq 'check owner 8000 | ctrl-break 77 | ctrl-break 77') 'API untouched when the worker does not stop'
        Assert ($world.api -eq 'previous') 'API still serving'
    }
    'no-worker-beside-an-in-process-scheduler' {
        # Even if an old API is somehow still serving after the API step, the
        # worker is not started next to it.
        $world.api = 'legacy'
        function Stop-StaleApi($Status) { return $true }
        Start-ApiAndWorker $status
        Assert ((Events) -eq 'check owner 8000') 'no worker beside the old scheduler'
        Assert (@($diagnostics) -contains 'worker not started: the API on port 8000 still runs the in-process sync scheduler.') 'reported'
    }
    'status-failure-leaves-a-healthy-api-alone' {
        $world.api = 'legacy'
        function Get-RuntimeStatus { throw 'python.exe is broken' }
        Invoke-ApiStack
        Assert ((Events) -eq '') 'nothing is stopped, upgraded or started'
        Assert (@($diagnostics) -like 'Runtime status unavailable (python.exe is broken)*') 'reported'
    }
    'status-failure-without-an-api-fails-the-launch' {
        function Get-RuntimeStatus { throw 'python.exe is broken' }
        $failed = $false
        try { Invoke-ApiStack } catch { $failed = $_.Exception.Message -eq 'python.exe is broken' }
        Assert $failed 'the launch fails'
        Assert ((Events) -eq '') 'nothing started'
    }
    'status-is-queried-through-the-process-runner' {
        # The real Get-RuntimeStatus runs launch_status via the runner and parses its JSON.
        function Start-HiddenCommand([string]$Command) {
            $world.events.Add('status')
            Assert ($Command -match '-m runway\.operations\.launch_status 1>"[^"]*status\.json" 2>"[^"]*status\.stderr\.log"') "command: $Command"
            Set-Content -LiteralPath (Join-Path $runtime 'status.json') -Value '{"database": "x", "current": null, "head": "0016_shared_contracts", "up_to_date": false, "known_revision": true, "revision": "abc", "worker_lock": "l", "worker_pid": "p"}'
            return New-FakeProcess $true 0
        }
        $got = Get-RuntimeStatus
        Assert ($got.head -eq '0016_shared_contracts' -and $got.up_to_date -eq $false -and $null -eq $got.current) 'parsed'
        function Start-HiddenCommand([string]$Command) { return New-FakeProcess $true 2 }
        $failed = $false
        try { Get-RuntimeStatus } catch { $failed = $_.Exception.Message.Contains('code 2') }
        Assert $failed 'a failed query is an error, not an empty status'
    }
    'database-ahead-leaves-a-healthy-api-alone' {
        # Older code checked out over a newer schema (rollback without db downgrade).
        $world.api = 'current'; $world.worker = $true; Set-DatabaseAhead
        Start-ApiAndWorker $status
        Assert ((Events) -eq '') 'nothing is stopped, migrated or started'
        Assert (@($diagnostics) -like 'The database is at revision 0099_from_newer_code, which this checkout does not know*db downgrade*') 'reported'
    }
    'database-ahead-without-an-api-fails-clearly' {
        Set-DatabaseAhead
        $failed = $false
        try { Start-ApiAndWorker $status } catch { $failed = $_.Exception.Message -like '*does not know*db downgrade*' }
        Assert $failed 'the launch fails with the rollback instruction'
        Assert ((Events) -eq '') 'no upgrade is attempted'
    }
    'a-foreign-process-behind-worker-pid-is-never-signalled' {
        # worker.pid is stale and Windows reused its PID for another program.
        $world.api = 'previous'; $world.worker = $true; $world.workerIsRunway = $false
        Start-ApiAndWorker $status
        Assert (-not ((Events) -like '*ctrl-break 77*')) 'the foreign process is not signalled'
        Assert ($world.api -eq 'previous') 'the API keeps serving'
        Assert (@($diagnostics) -like '*PID 77 is not a Runway worker*') 'reported'
    }
    'an-unverifiable-api-owner-stops-nothing' {
        $world.api = 'previous'; $world.worker = $true; $world.portOwnerOk = $false
        $failed = $false
        try { Start-ApiAndWorker $status } catch { $failed = $true }
        Assert $failed 'the launch fails'
        Assert (-not ((Events) -like '*ctrl-break*')) 'the worker is not stopped before the owner check'
    }
    'deferred-deploy-restarts-a-stopped-worker-when-safe' {
        # The worker stopped, the API (expansion code, same schema) would not.
        $world.api = 'previous'; $world.worker = $true; $world.apiStops = $false
        Start-ApiAndWorker $status
        Assert ((Events) -eq 'check owner 8000 | ctrl-break 77 | check owner 8000 | ctrl-break 500 | ctrl-break 500 | start worker') 'worker restarted beside the API'
        Assert ($world.worker) 'a worker runs again'
    }
    'a-malformed-worker-pid-file-is-ignored' {
        $file = Join-Path $runtime 'worker.pid'
        Set-Content -LiteralPath $file -Value '{"pid": "abc", "revision": "x"}'
        Assert ($null -eq (& $realGetWorkerRecord $file)) 'non-numeric pid'
        Set-Content -LiteralPath $file -Value '{"pid": 4242, "revision": "x"}'
        Assert ((& $realGetWorkerRecord $file).pid -eq 4242) 'numeric pid'
    }
    'fresh-install-is-migrated-not-refused' {
        # A never-migrated database: current is null and known_revision true.
        $status.current = $null; $status.up_to_date = $false; $world.db = 'behind'
        Start-ApiAndWorker $status
        Assert ((Events) -eq 'check owner 8000 | db upgrade | start api | check owner 8000 | start worker') 'fresh install order'
    }
    'worker-command-lines-are-recognised-exactly' {
        $yes = @(
            '"C:\Python313\python.exe" -m runway.cli.worker_commands',
            '"C:\repo\.venv\Scripts\python.exe"  -m runway.cli.worker_commands 1>"C:\repo\data\runtime\worker.log" 2>&1',
            '/d /s /c ""C:\repo\.venv\Scripts\python.exe" -m runway.cli.worker_commands 1>"x" 2>&1"',
            '"C:\repo\.venv\Scripts\python.exe" -m runway.cli.main worker',
            '"C:\repo\.venv\Scripts\runway.exe" worker',
            'C:\repo\.venv\Scripts\runway.exe worker --once',
            'runway worker'
        )
        $no = @(
            '', 'python -c "import runway.cli.worker_commands"', 'notepad runway.cli.worker_commands.log',
            'cmd /c "echo runway worker"', '"C:\bots\xrunway.exe" worker', 'python -m runway.cli.worker_commands_extra',
            '"C:\Python313\python.exe" -m runway.cli.main serve', '"C:\bots\csfloat\bot.exe" --worker 3'
        )
        foreach ($line in $yes) { Assert (Test-RunwayWorkerCommandLine $line) "should match: $line" }
        foreach ($line in $no) { Assert (-not (Test-RunwayWorkerCommandLine $line)) "should not match: $line" }
    }
    'an-out-of-range-worker-pid-is-ignored' {
        $file = Join-Path $runtime 'worker.pid'
        Set-Content -LiteralPath $file -Value '{"pid": 99999999999, "revision": "x"}'
        Assert ($null -eq (& $realGetWorkerRecord $file)) 'pid beyond Int32'
    }
    'a-foreign-process-on-the-api-port-after-the-worker-stop-is-never-signalled' {
        # The API exits while the worker is being stopped and another program binds 8000.
        $world.api = 'previous'; $world.worker = $true
        $script:checks = 0
        function Assert-PortOwner([string]$Component, [int]$Port) {
            $world.events.Add("check owner $Port"); $script:checks++
            if ($script:checks -ge 2) { throw "port $Port is owned by another program" }
        }
        $failed = $false
        try { Start-ApiAndWorker $status } catch { $failed = $true }
        Assert $failed 'the launch fails'
        Assert (-not ((Events) -like '*ctrl-break 500*')) 'the port owner is not signalled'
    }
    default { throw "unknown scenario $Scenario" }
}
Write-Output "scenario $Scenario passed"
"""

SCENARIOS = [
    "deploy-over-legacy",
    "deploy-over-previous-revision",
    "behind-with-api-down-and-worker-up",
    "current-stack-is-left-alone",
    "worker-failure-keeps-the-api",
    "stubborn-legacy-api-is-left-running",
    "stubborn-worker-blocks-the-deploy",
    "no-worker-beside-an-in-process-scheduler",
    "status-failure-leaves-a-healthy-api-alone",
    "status-failure-without-an-api-fails-the-launch",
    "status-is-queried-through-the-process-runner",
    "database-ahead-leaves-a-healthy-api-alone",
    "database-ahead-without-an-api-fails-clearly",
    "a-foreign-process-behind-worker-pid-is-never-signalled",
    "an-unverifiable-api-owner-stops-nothing",
    "deferred-deploy-restarts-a-stopped-worker-when-safe",
    "a-malformed-worker-pid-file-is-ignored",
    "fresh-install-is-migrated-not-refused",
    "worker-command-lines-are-recognised-exactly",
    "an-out-of-range-worker-pid-is-ignored",
    "a-foreign-process-on-the-api-port-after-the-worker-stop-is-never-signalled",
]


def _powershell(
    script: Path, *arguments: str, timeout: float = 120
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
            *arguments,
        ],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


@pytest.mark.skipif(sys.platform != "win32", reason="Windows launcher behaviour")
@pytest.mark.parametrize("scenario", SCENARIOS)
def test_launcher_deploy_behaviour(tmp_path: Path, scenario: str) -> None:
    script = tmp_path / "deploy.ps1"
    script.write_text(HARNESS, encoding="utf-8")
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    result = _powershell(
        script, "-Launcher", str(LAUNCHER), "-Runtime", str(runtime), "-Scenario", scenario
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"scenario {scenario} passed" in result.stdout


CONSOLE_STOP = r"""
param([string]$Launcher, [string]$Interpreter, [int]$Target)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$ast = [System.Management.Automation.Language.Parser]::ParseFile($Launcher, [ref]$null, [ref]$null)
$ast.FindAll({ param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst]
}, $false) | ForEach-Object { Invoke-Expression $_.Extent.Text }
$python = $Interpreter
function Write-Diagnostic([string]$Message) { Write-Output $Message }
if (-not (Stop-ByConsole $Target 'test child' @(30))) { throw 'the child did not stop on Ctrl+Break' }
Write-Output 'console stop passed'
"""

CHILD = """
import pathlib, signal, sys, threading, time
stop = threading.Event()
# Like uvicorn and runway worker: Ctrl+Break asks for a graceful stop.
signal.signal(signal.SIGBREAK, lambda *_: stop.set())
pathlib.Path(sys.argv[2]).write_text("ready")
deadline = time.monotonic() + 120
while not stop.is_set() and time.monotonic() < deadline:
    time.sleep(0.1)
pathlib.Path(sys.argv[1]).write_text("graceful" if stop.is_set() else "timed out")
"""


@pytest.mark.skipif(sys.platform != "win32", reason="Windows console control")
def test_the_launcher_stops_a_process_with_ctrl_break_on_its_own_console(tmp_path: Path) -> None:
    done = tmp_path / "done.txt"
    ready = tmp_path / "ready.txt"
    startup = subprocess.STARTUPINFO()  # type: ignore[attr-defined]
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW  # type: ignore[attr-defined]
    startup.wShowWindow = 0  # SW_HIDE
    # Like the launcher's children: its own (hidden) console.
    child = subprocess.Popen(
        [sys.executable, "-c", CHILD, str(done), str(ready)],
        creationflags=subprocess.CREATE_NEW_CONSOLE,  # type: ignore[attr-defined]
        startupinfo=startup,
    )
    try:
        deadline = time.monotonic() + 60
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.1)
        assert ready.exists(), "child did not start"
        script = tmp_path / "console-stop.ps1"
        script.write_text(CONSOLE_STOP, encoding="utf-8")
        result = _powershell(
            script,
            "-Launcher",
            str(LAUNCHER),
            "-Interpreter",
            sys.executable,
            "-Target",
            str(child.pid),
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "console stop passed" in result.stdout
        assert child.wait(timeout=30) is not None
        assert done.read_text() == "graceful"
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=30)
