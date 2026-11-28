# ruff: noqa: E501  (embedded PowerShell contract script)
"""Contract tests for ``scripts/sync_private_database_release.ps1``.

The script's functions are extracted from its AST and run with ``gh`` and the
settings lookup replaced by PowerShell functions, so nothing reaches GitHub.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "sync_private_database_release.ps1"


def _powershell() -> str | None:
    if sys.platform == "win32":
        return shutil.which("powershell.exe") or shutil.which("pwsh")
    return shutil.which("pwsh")


POWERSHELL = _powershell()

CONTRACT = r"""
param([string]$Script)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($Script, [ref]$null, [ref]$errors)
if ($errors) { throw "script does not parse: $errors" }
$ast.FindAll({ param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst]
}, $false) | ForEach-Object { Invoke-Expression $_.Extent.Text }
$codeRepository = 'ChardXBT/Runway'

$script:setting = ''
function Get-InstanceRepositorySetting { param([string]$Python) return $script:setting }
$script:repos = @{
    'owner/runway-instance' = '{"nameWithOwner":"owner/runway-instance","visibility":"PRIVATE"}'
    'owner/showcase' = '{"nameWithOwner":"owner/showcase","visibility":"PUBLIC"}'
    'ChardXBT/Runway' = '{"nameWithOwner":"ChardXBT/Runway","visibility":"PUBLIC"}'
}
$script:ghCalls = @()
function gh {
    $script:ghCalls += ,($args -join ' ')
    $json = $script:repos[[string]$args[2]]
    if ($null -eq $json) { $global:LASTEXITCODE = 1; return $null }
    $global:LASTEXITCODE = 0
    return $json
}
function Assert($Value, $Label) { if (-not $Value) { throw $Label } }
function Assert-Refused([scriptblock]$Action, $Label) {
    try { & $Action; throw "NOT REFUSED: $Label" }
    catch { if ($_.Exception.Message -like 'NOT REFUSED*') { throw } }
}

Assert ((Resolve-TargetRepository -Explicit 'owner/runway-instance' -Python 'py') -eq 'owner/runway-instance') 'explicit -Repository wins'
$script:setting = 'owner/runway-instance'
Assert ((Resolve-TargetRepository -Explicit '' -Python 'py') -eq 'owner/runway-instance') 'defaults to the instance repository setting'
Assert ((Resolve-TargetRepository -Explicit 'owner/showcase' -Python 'py') -eq 'owner/showcase') 'explicit beats the setting'
$script:setting = ''
Assert ((Resolve-TargetRepository -Explicit '' -Python 'py') -eq 'ChardXBT/Runway') 'without a setting the code repository remains the target'
Assert-Refused { Resolve-TargetRepository -Explicit 'not a repo' -Python 'py' } 'malformed name'
Assert-Refused { Resolve-TargetRepository -Explicit '--repo=x/y' -Python 'py' } 'option-shaped name'

Assert ((Assert-PrivateRepository -Name 'owner/runway-instance') -eq 'owner/runway-instance') 'private target accepted'
Assert-Refused { Assert-PrivateRepository -Name 'owner/showcase' } 'public target'
Assert-Refused { Assert-PrivateRepository -Name 'ChardXBT/Runway' } 'public code repository'
Assert-Refused { Assert-PrivateRepository -Name 'owner/missing' } 'unknown repository'
Assert ($script:ghCalls[0] -eq 'repo view owner/runway-instance --json visibility,nameWithOwner') ('gh call was ' + $script:ghCalls[0])

$text = Get-Content -LiteralPath $Script -Raw
Assert ($text -match '\[string\]\$Repository = ""') 'Repository parameter'
$visibilityChecks = ([regex]::Matches($text, 'Assert-PrivateRepository -Name')).Count
Assert ($visibilityChecks -eq 1) 'the upload path calls the visibility guard'
Assert ($text.IndexOf('Assert-PrivateRepository -Name $targetRepository') -lt $text.IndexOf('gh release upload')) 'guard runs before any upload'
Write-Output 'release script contracts passed'
"""


@pytest.mark.skipif(POWERSHELL is None, reason="PowerShell is not installed")
def test_release_script_targets_the_instance_repository_and_refuses_public_ones(
    tmp_path: Path,
) -> None:
    assert POWERSHELL is not None
    contract = tmp_path / "release-contract.ps1"
    contract.write_text(CONTRACT, encoding="utf-8")
    command = [POWERSHELL, "-NoProfile", "-NonInteractive"]
    if sys.platform == "win32":
        command += ["-ExecutionPolicy", "Bypass"]
    result = subprocess.run(
        [*command, "-File", str(contract), "-Script", str(SCRIPT)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "release script contracts passed" in result.stdout
