param(
    # The clone to configure; defaults to the checkout this script belongs to.
    [string]$RepositoryRoot = ""
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

if (-not $RepositoryRoot) {
    $RepositoryRoot = Split-Path -Parent $PSScriptRoot
}
$RepositoryRoot = [System.IO.Path]::GetFullPath($RepositoryRoot)
$hook = Join-Path $RepositoryRoot ".githooks\pre-commit"
if (-not (Test-Path -LiteralPath $hook -PathType Leaf)) {
    throw "$hook is missing; run this script from a Runway checkout."
}

& git -C $RepositoryRoot config core.hooksPath .githooks
if ($LASTEXITCODE -ne 0) {
    throw "git config core.hooksPath failed with exit code $LASTEXITCODE."
}
$configured = (& git -C $RepositoryRoot config --get core.hooksPath).Trim()
if ($configured -ne ".githooks") {
    throw "core.hooksPath is '$configured', expected '.githooks'."
}
Write-Output "Secret-scan pre-commit hook enabled for $RepositoryRoot (core.hooksPath=.githooks)."