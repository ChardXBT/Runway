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
