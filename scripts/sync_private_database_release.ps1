param(
    [string]$TargetSha = "",
    # Where the release goes. Defaults to the private instance repository
    # (RUNWAY_INSTANCE_REPOSITORY / instance_repository), else the code repository.
    [string]$Repository = ""
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$codeRepository = "ChardXBT/Runway"
$releaseTag = "database-latest"
$databaseAssetName = "runway-database.db.gz"
$mediaAssetName = "runway-operational-media.zip"
$manifestAssetName = "runway-database-manifest.json"
$maxReleaseAssetBytes = 2GB

function Invoke-Native {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Command,
        [Parameter(ValueFromRemainingArguments = $true)]
        [string[]]$Arguments
    )

    & $Command @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$Command failed with exit code $LASTEXITCODE."
    }
}

function Assert-ReleaseAsset {
    param(
        [Parameter(Mandatory = $true)]
        [object]$Release,
        [Parameter(Mandatory = $true)]
        [string]$Name,
        [Parameter(Mandatory = $true)]
        [long]$Size,
        [Parameter(Mandatory = $true)]
        [string]$Sha256
    )

    $asset = $Release.assets | Where-Object name -eq $Name | Select-Object -First 1
    if ($null -eq $asset -or $asset.size -ne $Size) {
        throw "Uploaded release asset $Name is missing or has the wrong size."
    }
    $expectedDigest = "sha256:$Sha256"
    if ($asset.digest -and $asset.digest -ne $expectedDigest) {
        throw "Uploaded release asset $Name has the wrong GitHub digest."
    }
}

function Get-InstanceRepositorySetting {
    param([Parameter(Mandatory = $true)][string]$Python)

    $value = & $Python -c "from runway.config import get_settings; print(get_settings().instance_repository or '')"
    if ($LASTEXITCODE -ne 0) {
        throw "Database sync refused: Runway settings could not be loaded to find the instance repository."
    }
    return ([string]$value).Trim()
}

function Resolve-TargetRepository {
    param(
        [string]$Explicit,
        [Parameter(Mandatory = $true)][string]$Python
    )

    $name = $Explicit
    if (-not $name) {
        $name = Get-InstanceRepositorySetting -Python $Python
    }
    if (-not $name) {
        $name = $codeRepository
    }
    if ($name -notmatch "^[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100}$") {
        throw "Database sync refused: '$name' is not a GitHub repository in owner/name form."
    }
    return $name
}

function Assert-PrivateRepository {
    param([Parameter(Mandatory = $true)][string]$Name)

    $json = gh repo view $Name --json "visibility,nameWithOwner"
    if ($LASTEXITCODE -ne 0 -or -not $json) {
        throw "Database sync refused: $Name is not confirmed private."
    }
    $state = $json | ConvertFrom-Json
    if ($state.visibility -ne "PRIVATE") {
        throw "Database sync refused: $Name is not confirmed private."
    }
    return [string]$state.nameWithOwner
}

$repoRoot = (git rev-parse --show-toplevel).Trim()
if ($LASTEXITCODE -ne 0 -or -not $repoRoot) {
    throw "Unable to resolve the Runway repository root."
}
$repoRoot = [System.IO.Path]::GetFullPath($repoRoot)

$originUrl = (git -C $repoRoot remote get-url origin).Trim()
if ($LASTEXITCODE -ne 0 -or $originUrl -notmatch "github\.com[:/]ChardXBT/Runway(?:\.git)?$") {
    throw "Database sync refused: origin is not ChardXBT/Runway."
}

if (-not $TargetSha) {
    $TargetSha = (git -C $repoRoot rev-parse HEAD).Trim()
}
if ($TargetSha -notmatch "^[0-9a-fA-F]{40}$") {
    throw "Database sync refused: target commit SHA is invalid."
}
$TargetSha = $TargetSha.ToLowerInvariant()

# This is deliberately a post-push operation. A pre-push release upload can
# advertise a database for a commit that GitHub never accepted.
$remoteMain = (git -C $repoRoot ls-remote origin refs/heads/main).Trim().Split("`t")[0]
if ($LASTEXITCODE -ne 0 -or $remoteMain -ne $TargetSha) {
    throw (
        "Database sync refused: origin/main must already equal $TargetSha. " +
        "Push main successfully before publishing its database snapshot."
    )
}

$branch = (git -C $repoRoot branch --show-current).Trim()
if ($LASTEXITCODE -ne 0 -or $branch -ne "main") {
    throw "Database sync refused: the checked-out branch must be main."
}
$dataRoot = Join-Path $repoRoot "data\qlob-production"
$sourceDatabase = Join-Path $dataRoot "runway.db"
if (-not (Test-Path -LiteralPath $sourceDatabase -PathType Leaf)) {
    throw "Database sync refused: $sourceDatabase does not exist."
}

$python = Join-Path $repoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    $pythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
    if (-not $pythonCommand) {
        throw "Database sync requires Python."
    }
    $python = $pythonCommand.Source
}

$targetRepository = Resolve-TargetRepository -Explicit $Repository -Python $python
Invoke-Native gh auth status --hostname github.com
$targetRepository = Assert-PrivateRepository -Name $targetRepository

$tempBase = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath())
$workDirectory = Join-Path $tempBase ("runway-db-sync-" + [guid]::NewGuid().ToString("N"))
$workDirectory = [System.IO.Path]::GetFullPath($workDirectory)
if (
    -not $workDirectory.StartsWith($tempBase, [System.StringComparison]::OrdinalIgnoreCase) -or
    -not (Split-Path -Leaf $workDirectory).StartsWith("runway-db-sync-", [System.StringComparison]::Ordinal)
) {
    throw "Refusing to use an unsafe temporary directory."
}

New-Item -ItemType Directory -Path $workDirectory | Out-Null
$databaseArchive = Join-Path $workDirectory $databaseAssetName
$mediaArchive = Join-Path $workDirectory $mediaAssetName
$manifestPath = Join-Path $workDirectory $manifestAssetName
$downloadDirectory = Join-Path $workDirectory "remote-verification"

try {
    $manifestJson = & $python -m runway.operations.database_release create `
        --source-database $sourceDatabase `
        --data-root $dataRoot `
        --output-directory $workDirectory `
        --commit-sha $TargetSha `
        --branch $branch
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to create and verify the private database release."
    }
    $manifest = $manifestJson | ConvertFrom-Json

    foreach ($path in @($databaseArchive, $mediaArchive, $manifestPath)) {
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
            throw "Database release asset was not created: $path"
        }
        if ((Get-Item -LiteralPath $path).Length -ge $maxReleaseAssetBytes) {
            throw "Database release asset exceeds GitHub's 2 GiB asset limit: $path"
        }
    }

    & gh release view $releaseTag --repo $targetRepository *> $null
    $releaseExists = $LASTEXITCODE -eq 0
    if (-not $releaseExists) {
        $notes = @"
Rolling private recovery snapshot of Runway's canonical Qlob intelligence database.

The database uses SQLite's online backup API. A companion archive contains every media file referenced by the canonical database, including historical, candidate, review, Lineup, and published records. Browser profiles, authentication state, orphan media, logs, and WAL/SHM files are excluded. Every asset is restored and hash-verified before this release is accepted.
"@
        # --target names a commit of the code repository; an instance repository
        # has its own history, so its release tags that repository's default branch.
        $targetArguments = @()
        if ($targetRepository -eq $codeRepository) {
            $targetArguments = @("--target", "main")
        }
        Invoke-Native gh release create $releaseTag `
            --repo $targetRepository `
            @targetArguments `
            --title "Runway database snapshot" `
            --notes $notes `
            --prerelease
    }

    Invoke-Native gh release upload $releaseTag `
        "$databaseArchive#$databaseAssetName" `
        "$mediaArchive#$mediaAssetName" `
        "$manifestPath#$manifestAssetName" `
        --clobber `
        --repo $targetRepository

    $release = gh api "repos/$targetRepository/releases/tags/$releaseTag" | ConvertFrom-Json
    if ($LASTEXITCODE -ne 0) {
        throw "Unable to inspect the updated database release."
    }
    Assert-ReleaseAsset `
        -Release $release `
        -Name $databaseAssetName `
        -Size ([long]$manifest.archive_bytes) `
        -Sha256 ([string]$manifest.archive_sha256)
    Assert-ReleaseAsset `
        -Release $release `
        -Name $mediaAssetName `
        -Size ([long]$manifest.database_media.archive_bytes) `
        -Sha256 ([string]$manifest.database_media.archive_sha256)
    $manifestSha256 = (Get-FileHash -LiteralPath $manifestPath -Algorithm SHA256).Hash.ToLowerInvariant()
    Assert-ReleaseAsset `
        -Release $release `
        -Name $manifestAssetName `
        -Size ([long](Get-Item -LiteralPath $manifestPath).Length) `
        -Sha256 $manifestSha256

    New-Item -ItemType Directory -Path $downloadDirectory | Out-Null
    Invoke-Native gh release download $releaseTag `
        --repo $targetRepository `
        --pattern $databaseAssetName `
        --pattern $mediaAssetName `
        --pattern $manifestAssetName `
        --dir $downloadDirectory

    $remoteManifest = Join-Path $downloadDirectory $manifestAssetName
    if ((Get-FileHash -LiteralPath $remoteManifest -Algorithm SHA256).Hash.ToLowerInvariant() -ne $manifestSha256) {
        throw "Downloaded database manifest does not match the uploaded manifest."
    }
    $verificationJson = & $python -m runway.operations.database_release verify `
        --manifest $remoteManifest `
        --database-archive (Join-Path $downloadDirectory $databaseAssetName) `
        --media-archive (Join-Path $downloadDirectory $mediaAssetName)
    if ($LASTEXITCODE -ne 0) {
        throw "The downloaded private database release failed restoration verification."
    }
    $verification = $verificationJson | ConvertFrom-Json
    if ($verification.status -ne "verified" -or $verification.commit_sha -ne $TargetSha) {
        throw "The downloaded private database release verified the wrong commit."
    }

    Write-Host (
        "Runway private recovery release verified for {0}: database {1:N2} MiB, database media {2} files." -f
        $TargetSha,
        ($manifest.archive_bytes / 1MB),
        $manifest.database_media.entry_count
    )
}
finally {
    if (
        (Test-Path -LiteralPath $workDirectory) -and
        $workDirectory.StartsWith($tempBase, [System.StringComparison]::OrdinalIgnoreCase) -and
        (Split-Path -Leaf $workDirectory).StartsWith("runway-db-sync-", [System.StringComparison]::Ordinal)
    ) {
        Remove-Item -LiteralPath $workDirectory -Recurse -Force
    }
}
