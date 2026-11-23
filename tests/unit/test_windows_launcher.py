# ruff: noqa: E501  (embedded PowerShell contract scripts)
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.skipif(sys.platform != "win32", reason="Windows launcher contract")
def test_windows_launcher_identity_and_readiness(tmp_path: Path) -> None:
    launcher = Path(__file__).resolve().parents[2] / "run-runway.ps1"
    script = tmp_path / "launcher-contract.ps1"
    script.write_text(
        r"""
param([string]$Launcher)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$ast = [System.Management.Automation.Language.Parser]::ParseFile($Launcher, [ref]$null, [ref]$null)
$ast.FindAll({ param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst]
}, $false) |
    ForEach-Object { Invoke-Expression $_.Extent.Text }
$python = 'C:\repo with spaces\.venv\Scripts\python.exe'
$next = 'C:\repo with spaces\apps\web\node_modules\next\dist\bin\next'
function Assert($Value, $Label) { if (-not $Value) { throw $Label } }
$api = [pscustomobject]@{
    Name='python.exe'; ExecutablePath='C:\Python\python.exe'
    CommandLine='"' + $python + '" -m uvicorn runway.api.app:app'
}
Assert (Test-Owned $api 'api') 'venv redirector must be recognized'
$api.CommandLine = '"' + $python.ToLowerInvariant() + '" -m uvicorn runway.api.app:app'
Assert (Test-Owned $api 'api') 'path casing must not disown a running Runway API'
$api.CommandLine = '"' + $python + '" -m uvicorn runway.api.app:app'
$api.CommandLine = 'python -m uvicorn other:app'
Assert (-not (Test-Owned $api 'api')) 'unrelated Python must not be owned'
# Python 3.12 python.org venv: the listener is the base interpreter, its parent the redirector.
$base = [pscustomobject]@{
    Name='python.exe'; ParentProcessId=42
    CommandLine='"C:\Python312\python.exe" -m uvicorn runway.api.app:app --port 8000'
}
$parentProcess = [pscustomobject]@{ CommandLine='"' + $python + '" -m uvicorn runway.api.app:app --port 8000' }
function Get-ParentProcess { return $script:parentProcess }
Assert (Test-Owned $base 'api') 'base interpreter started by our venv redirector must be owned'
$parentProcess = [pscustomobject]@{ CommandLine='"C:\other\.venv\Scripts\python.exe" -m uvicorn runway.api.app:app' }
Assert (-not (Test-Owned $base 'api')) 'another checkout''s redirector must not be owned'
$parentProcess = $null
Assert (-not (Test-Owned $base 'api')) 'orphaned base interpreter must not be owned'
$base.CommandLine = '"C:\Python312\python.exe" -m uvicorn other:app'
$parentProcess = [pscustomobject]@{ CommandLine='"' + $python + '" -m uvicorn runway.api.app:app' }
Assert (-not (Test-Owned $base 'api')) 'base interpreter running another app must not be owned'
$web = [pscustomobject]@{ Name='node.exe'; CommandLine='node "' + $next + '" start' }
Assert (Test-Owned $web 'web') 'absolute Next entry point must be recognized'
$web.CommandLine = 'node C:\other\server.js'
Assert (-not (Test-Owned $web 'web')) 'unrelated Node must not be owned'
function Invoke-RestMethod { return $script:health }
$health = @{product='Runway'; status='ok'; component='web'}
Assert (Test-Ready 'web') 'valid web identity'
$health = @{product='Other'; status='ok'; component='web'}
Assert (-not (Test-Ready 'web')) 'foreign web identity'
$health = @{product='Runway'; status='failed'; component='web'}
Assert (-not (Test-Ready 'web')) 'unhealthy response'
$health = @()
Assert (-not (Test-Ready 'api')) 'malformed response must fail closed'
function Get-Listener { return @() }
Assert-PortOwner 'api' 8000
function Get-Listener { return [pscustomobject]@{OwningProcess=123} }
function Get-CimInstance { return $web }
$rejected = $false
try { Assert-PortOwner 'web' 3000 } catch {
    $rejected = $_.Exception.Message.Contains('Nothing was killed')
}
Assert $rejected 'foreign listener must fail without termination'
Write-Output 'launcher contracts passed'
""",
        encoding="utf-8",
    )
    result = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
            "-Launcher",
            str(launcher),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "launcher contracts passed" in result.stdout


TREE_SCRIPT = r"""
param([string]$Launcher, [string]$Python, [string]$PidFile)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$ast = [System.Management.Automation.Language.Parser]::ParseFile($Launcher, [ref]$null, [ref]$null)
$ast.FindAll({ param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst]
}, $false) | ForEach-Object { Invoke-Expression $_.Extent.Text }
# Like the venv redirector: the started process spawns the long-lived server.
$code = "import subprocess,sys,time; p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(120)']); open(r'$PidFile','w').write(str(p.pid)); time.sleep(120)"
$child = Start-Process -FilePath $Python -ArgumentList @('-c', ('"{0}"' -f $code)) -WindowStyle Hidden -PassThru
$deadline = (Get-Date).AddSeconds(20)
while (-not (Test-Path -LiteralPath $PidFile) -and (Get-Date) -lt $deadline) { Start-Sleep -Milliseconds 100 }
Start-Sleep -Milliseconds 300
$grandchild = [int](Get-Content -LiteralPath $PidFile)
Stop-StartedChild $child
Start-Sleep -Milliseconds 500
if (Get-Process -Id $grandchild -ErrorAction SilentlyContinue) { throw "grandchild $grandchild survived rollback" }
if (-not $child.HasExited) { throw 'started child survived rollback' }
Write-Output 'rollback stopped the whole tree'
"""


@pytest.mark.skipif(sys.platform != "win32", reason="Windows launcher contract")
def test_failed_start_rollback_stops_the_redirected_server(tmp_path: Path) -> None:
    launcher = Path(__file__).resolve().parents[2] / "run-runway.ps1"
    script = tmp_path / "rollback-tree.ps1"
    script.write_text(TREE_SCRIPT, encoding="utf-8")
    result = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
            "-Launcher",
            str(launcher),
            "-Python",
            sys.executable,
            "-PidFile",
            str(tmp_path / "grandchild.pid"),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "rollback stopped the whole tree" in result.stdout


ROTATION_SCRIPT = r"""
param([string]$Launcher, [string]$Runtime)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$ast = [System.Management.Automation.Language.Parser]::ParseFile($Launcher, [ref]$null, [ref]$null)
$ast.FindAll({ param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst]
}, $false) | ForEach-Object { Invoke-Expression $_.Extent.Text }
$runtime = $Runtime
function Assert($Value, $Label) { if (-not $Value) { throw $Label } }
Save-PreviousLog 'api.stderr'
Assert (-not (Test-Path (Join-Path $runtime 'api.stderr.previous.log'))) 'missing log must be a no-op'
Set-Content -LiteralPath (Join-Path $runtime 'api.stderr.log') -Value 'Traceback: crash evidence'
Save-PreviousLog 'api.stderr'
Assert (-not (Test-Path (Join-Path $runtime 'api.stderr.log'))) 'restart must not append to the crashed log'
Assert ((Get-Content (Join-Path $runtime 'api.stderr.previous.log')) -eq 'Traceback: crash evidence') 'crash evidence must survive a restart'
Set-Content -LiteralPath (Join-Path $runtime 'launcher.log') -Value 'small'
Save-PreviousLog 'launcher' 1MB
Assert (Test-Path (Join-Path $runtime 'launcher.log')) 'launcher log below the cap must stay in place'
Write-Output 'log rotation contracts passed'
"""


@pytest.mark.skipif(sys.platform != "win32", reason="Windows launcher contract")
def test_restart_preserves_previous_component_logs(tmp_path: Path) -> None:
    launcher = Path(__file__).resolve().parents[2] / "run-runway.ps1"
    script = tmp_path / "rotation.ps1"
    script.write_text(ROTATION_SCRIPT, encoding="utf-8")
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    result = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
            "-Launcher",
            str(launcher),
            "-Runtime",
            str(runtime),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "log rotation contracts passed" in result.stdout


DIAGNOSTICS_SCRIPT = r"""
param([string]$Launcher, [string]$Repo, [string]$NotRepo)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$ast = [System.Management.Automation.Language.Parser]::ParseFile($Launcher, [ref]$null, [ref]$null)
$ast.FindAll({ param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst]
}, $false) | ForEach-Object { Invoke-Expression $_.Extent.Text }
function Assert($Value, $Label) { if (-not $Value) { throw $Label } }
$line = Get-LaunchSummary $Repo $true $true 90
Assert ($line -match '^Launch requested: git=[0-9a-f]{12} components=api,web timeout=90s pid=\d+$') "summary: $line"
$line = Get-LaunchSummary $NotRepo $false $true 45
Assert ($line -match '^Launch requested: git=unknown components=web timeout=45s pid=\d+$') "no repo: $line"
# Since KB5127936/KB5132402 a listener can report a blank State. Only rows with
# no remote port whose State is blank or Listen are listeners on that port.
function Get-NetTCPConnection {
    return @(
        [pscustomobject]@{ LocalPort = 8000; RemotePort = 0; State = ''; OwningProcess = 11 },
        [pscustomobject]@{ LocalPort = 8000; RemotePort = 0; State = 'Listen'; OwningProcess = 12 },
        [pscustomobject]@{ LocalPort = 8000; RemotePort = 0; State = 'Bound'; OwningProcess = 13 },
        [pscustomobject]@{ LocalPort = 8000; RemotePort = 51234; State = 'Established'; OwningProcess = 14 },
        [pscustomobject]@{ LocalPort = 8000; RemotePort = 51235; State = ''; OwningProcess = 15 },
        [pscustomobject]@{ LocalPort = 8000; RemotePort = 0; State = 'TimeWait'; OwningProcess = 16 },
        [pscustomobject]@{ LocalPort = 3000; RemotePort = 0; State = 'Listen'; OwningProcess = 17 }
    )
}
$owners = @(Get-Listener 8000 | ForEach-Object { $_.OwningProcess }) -join ','
Assert ($owners -eq '11,12') "listeners on 8000: $owners"
$owners = @(Get-Listener 3000 | ForEach-Object { $_.OwningProcess }) -join ','
Assert ($owners -eq '17') "listeners on 3000: $owners"
Assert (@(Get-Listener 4000).Count -eq 0) 'a port nobody listens on is free'
function Get-NetTCPConnection { throw 'query failed' }
$failed = $false
try { Get-Listener 8000 | Out-Null } catch { $failed = $true }
Assert $failed 'a failed port query must not look like a free port'
Write-Output 'launcher diagnostics contracts passed'
"""


@pytest.mark.skipif(sys.platform != "win32", reason="Windows launcher contract")
def test_launch_summary_and_listener_detection(tmp_path: Path) -> None:
    repo = Path(__file__).resolve().parents[2]
    launcher = repo / "run-runway.ps1"
    script = tmp_path / "diagnostics.ps1"
    script.write_text(DIAGNOSTICS_SCRIPT, encoding="utf-8")
    not_repo = tmp_path / "not-a-repo"
    not_repo.mkdir()
    result = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
            "-Launcher",
            str(launcher),
            "-Repo",
            str(repo),
            "-NotRepo",
            str(not_repo),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "launcher diagnostics contracts passed" in result.stdout


UPGRADE_SCRIPT = r"""
param([string]$Launcher, [string]$Root)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$ast = [System.Management.Automation.Language.Parser]::ParseFile($Launcher, [ref]$null, [ref]$null)
$ast.FindAll({ param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst]
}, $false) | ForEach-Object { Invoke-Expression $_.Extent.Text }
function Assert($Value, $Label) { if (-not $Value) { throw $Label } }
$root = $Root
$runtime = Join-Path $Root 'runtime'
$null = New-Item -ItemType Directory -Force -Path $runtime
# A fake venv interpreter that records its arguments and exits with FAKE_EXIT.
$python = Join-Path $Root 'fake python.cmd'
Set-Content -LiteralPath $python -Value "@echo off`r`necho %*> `"%~dp0args.txt`"`r`necho alembic output`r`nexit /b %FAKE_EXIT%" -Encoding ASCII
$env:FAKE_EXIT = '0'
Invoke-DatabaseUpgrade
$recorded = (Get-Content -LiteralPath (Join-Path $Root 'args.txt')).Trim()
Assert ($recorded -eq '-m runway.cli.db_commands upgrade') "upgrade arguments: $recorded"
Assert ((Get-Content (Join-Path $runtime 'db-upgrade.log')) -contains 'alembic output') 'upgrade output must be logged'
$env:FAKE_EXIT = '3'
$failed = $false
try { Invoke-DatabaseUpgrade } catch {
    $failed = $_.Exception.Message.Contains('code 3') -and $_.Exception.Message.Contains('db-upgrade.log')
}
Assert $failed 'a failed upgrade must stop the launch'
Write-Output 'database upgrade contracts passed'
"""


@pytest.mark.skipif(sys.platform != "win32", reason="Windows launcher contract")
def test_launcher_upgrades_the_database_with_the_venv_python(tmp_path: Path) -> None:
    launcher = Path(__file__).resolve().parents[2] / "run-runway.ps1"
    script = tmp_path / "upgrade.ps1"
    script.write_text(UPGRADE_SCRIPT, encoding="utf-8")
    root = tmp_path / "root with spaces"
    root.mkdir()
    result = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
            "-Launcher",
            str(launcher),
            "-Root",
            str(root),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "database upgrade contracts passed" in result.stdout


def _launcher_function(name: str) -> str:
    text = (Path(__file__).resolve().parents[2] / "run-runway.ps1").read_text(encoding="utf-8")
    body = text[text.index(f"function {name}") :]
    return body[: body.index("\n}\n")]


def test_launcher_upgrades_before_starting_the_api() -> None:
    start_component = _launcher_function("Start-Component")
    upgrade = start_component.index("if ($Component -eq 'api') { Invoke-DatabaseUpgrade }")
    assert upgrade < start_component.index("Start-HiddenCommand")
    # The API itself never migrates; uvicorn serves the app module unchanged.
    assert "-m uvicorn runway.api.app:app --host 127.0.0.1 --port 8000" in start_component


WORKER_SCRIPT = r"""
param([string]$Launcher, [string]$Root, [string]$Interpreter)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$ast = [System.Management.Automation.Language.Parser]::ParseFile($Launcher, [ref]$null, [ref]$null)
$ast.FindAll({ param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst]
}, $false) | ForEach-Object { Invoke-Expression $_.Extent.Text }
function Assert($Value, $Label) { if (-not $Value) { throw $Label } }
$root = $Root
$runtime = Join-Path $Root 'runtime'
$null = New-Item -ItemType Directory -Force -Path $runtime
$lockPath = Join-Path $runtime 'worker.lock'
$pidPath = Join-Path $runtime 'worker.pid'
$started = @()
function Write-Diagnostic([string]$Message) { $script:diagnostics += ,$Message }
$diagnostics = @()

# Ownership by command line is for the API only; the worker is found by its lock.
$python = 'C:\repo with spaces\.venv\Scripts\python.exe'
$worker = [pscustomobject]@{ Name='python.exe'; ProcessId=7; CommandLine='"' + $python + '" -m runway.cli.main worker' }
Assert (-not (Test-Owned $worker 'api')) 'a worker is not the API'
Assert (-not (Test-Owned $worker 'worker')) 'workers are not recognised by command line'

# A worker holds worker.lock through runway.locks, whatever its command line
# (here a bare python -c, like runway.exe worker would be).
Assert (-not (Test-WorkerLockHeld $lockPath)) 'no lock file means no worker'
$ready = Join-Path $Root 'holder.ready'
$holderCode = "import sys,time,pathlib; from runway.locks import exclusive_file_lock; cm=exclusive_file_lock(pathlib.Path(sys.argv[1]), timeout=0); cm.__enter__(); pathlib.Path(sys.argv[2]).write_text('x'); time.sleep(60)"
$holder = Start-Process -FilePath $Interpreter -ArgumentList @('-c', ('"{0}"' -f $holderCode), ('"{0}"' -f $lockPath), ('"{0}"' -f $ready)) -WindowStyle Hidden -PassThru
try {
    $deadline = (Get-Date).AddSeconds(60)
    while (-not (Test-Path -LiteralPath $ready) -and (Get-Date) -lt $deadline) { Start-Sleep -Milliseconds 100 }
    Assert (Test-Path -LiteralPath $ready) 'lock holder did not start'
    Set-Content -LiteralPath $pidPath -Value ('{"pid": ' + $holder.Id + ', "revision": "abc", "started_at": "now"}')
    Assert (Test-WorkerLockHeld $lockPath) 'a held worker.lock is a running worker'
    function Start-HiddenCommand { throw 'a running worker must not be started twice' }
    Start-Worker $lockPath $pidPath
    Assert ($started.Count -eq 0) 'a running worker must not be started twice'
    Assert ($diagnostics[-1] -eq "worker already running (PID $($holder.Id)).") "diagnostic: $($diagnostics[-1])"
} finally {
    Stop-Process -Id $holder.Id -Force -ErrorAction SilentlyContinue
    $holder.WaitForExit(10000) | Out-Null
}
# The OS dropped the lock with the process; the leftover file is not a worker.
Assert (Test-Path -LiteralPath $lockPath) 'lock file stays behind'
Assert (-not (Test-WorkerLockHeld $lockPath)) 'a released lock is not a running worker'

# Re-load the real process runner, then start a fake worker with the venv python.
$ast.FindAll({ param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Start-HiddenCommand'
}, $false) | ForEach-Object { Invoke-Expression $_.Extent.Text }
$python = Join-Path $Root 'fake python.cmd'
Set-Content -LiteralPath $python -Value "@echo off`r`necho %*> `"%~dp0worker-args.txt`"`r`necho worker log line`r`nif `"%FAKE_EXIT%`"==`"`" (ping -n 30 127.0.0.1 >nul) else (exit /b %FAKE_EXIT%)" -Encoding ASCII
$env:FAKE_EXIT = ''
Start-Worker $lockPath $pidPath 1
Assert ($started.Count -eq 1) 'the started worker must be tracked'
$recorded = (Get-Content -LiteralPath (Join-Path $Root 'worker-args.txt')).Trim()
Assert ($recorded -eq '-m runway.cli.worker_commands') "worker arguments: $recorded"
Assert ((Get-Content (Join-Path $runtime 'worker.log')) -contains 'worker log line') 'worker output must go to worker.log'
Stop-StartedChild $started[0]
Assert $started[0].HasExited 'the started worker can be stopped'

# A worker that exits during the grace period is an error for Start-Worker ...
$started = @()
$env:FAKE_EXIT = '3'
$failed = $false
try { Start-Worker $lockPath $pidPath 5 } catch {
    $failed = $_.Exception.Message.Contains('code 3') -and $_.Exception.Message.Contains('worker.log')
}
Assert $failed 'a worker that exits at startup is reported'

# ... but never fails the launch: it is logged and the API is left up.
$diagnostics = @()
Start-WorkerBesideApi $lockPath $pidPath
Assert ($diagnostics[-1] -like 'worker did not start: worker exited with code 3*The API stays up*') "diagnostic: $($diagnostics[-1])"
# Nor does a failed lock check.
function Test-WorkerLockHeld { throw 'lock check failed' }
Start-WorkerBesideApi $lockPath $pidPath
Assert ($diagnostics[-1] -like 'worker did not start: lock check failed*') "diagnostic: $($diagnostics[-1])"
Write-Output 'worker launcher contracts passed'
"""


@pytest.mark.skipif(sys.platform != "win32", reason="Windows launcher contract")
def test_launcher_starts_one_worker_and_never_fails_the_launch_for_it(tmp_path: Path) -> None:
    launcher = Path(__file__).resolve().parents[2] / "run-runway.ps1"
    script = tmp_path / "worker.ps1"
    script.write_text(WORKER_SCRIPT, encoding="utf-8")
    root = tmp_path / "root with spaces"
    root.mkdir()
    result = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
            "-Launcher",
            str(launcher),
            "-Root",
            str(root),
            "-Interpreter",
            sys.executable,
        ],
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "worker launcher contracts passed" in result.stdout


def test_launcher_starts_the_worker_with_the_api() -> None:
    text = (Path(__file__).resolve().parents[2] / "run-runway.ps1").read_text(encoding="utf-8")
    main = text[text.rindex("try {") :]
    assert main.index("Start-ApiAndWorker (Get-RuntimeStatus)") < main.index(
        "Start-Component 'web' 3000"
    )
    api_and_worker = _launcher_function("Start-ApiAndWorker")
    api = api_and_worker.index("Start-Component 'api' 8000")
    assert api < api_and_worker.index("Start-WorkerBesideApi")
    # A worker failure is logged inside Start-WorkerBesideApi, never thrown.
    assert "Start-Worker " not in api_and_worker
    # Health checks are unchanged: only the API and web components are probed.
    assert "Test-Ready 'worker'" not in text
