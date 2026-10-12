param(
    [string]$TaskName = 'A_Runway_Task',
    [ValidateRange(0, 1440)][int]$WatchdogMinutes = 5
)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$launcher = Join-Path $root 'run-runway.ps1'
$powerShell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$user = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$action = New-ScheduledTaskAction -Execute $powerShell -WorkingDirectory $root -Argument (
    '-NoLogo -NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "{0}"' -f $launcher
)
# The launcher's own waits are bounded (about 6.5 minutes in the worst case), so this limit
# only ends a genuinely stuck launcher, never a healthy start.
$timeLimit = New-TimeSpan -Minutes 10

function New-WatchdogTrigger {
    # Start at logon, then rerun the idempotent launcher. Healthy services are left alone;
    # a component whose port is free (it crashed) is started again. Nothing is ever killed.
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
    $trigger.Delay = 'PT30S'
    if ($WatchdogMinutes -gt 0) {
        $trigger.Repetition = (New-ScheduledTaskTrigger -Once -At (Get-Date) `
            -RepetitionInterval (New-TimeSpan -Minutes $WatchdogMinutes)).Repetition
    }
    return $trigger
}

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    $backup = Join-Path $root 'data\runtime'
    $null = New-Item -ItemType Directory -Force -Path $backup
    Export-ScheduledTask -TaskName $TaskName | Set-Content -LiteralPath (
        Join-Path $backup ('scheduled-task-before-{0}.xml' -f (Get-Date -Format 'yyyyMMdd-HHmmss'))
    )
    # Preserve existing triggers/account, but local loopback services do not need elevation.
    $existing.Actions = @($action)
    $existing.Principal.RunLevel = 0 # Limited, consistent with a manual launch
    $existing.Settings.RunOnlyIfNetworkAvailable = $false
    $existing.Settings.MultipleInstances = 2 # IgnoreNew: never overlap launcher runs
    $existing.Settings.ExecutionTimeLimit = 'PT10M'
    $hasLogon = @($existing.Triggers | Where-Object {
        $_.CimClass.CimClassName -eq 'MSFT_TaskLogonTrigger'
    }).Count -gt 0
    if (-not $hasLogon) { $existing.Triggers = @($existing.Triggers) + (New-WatchdogTrigger) }
    Set-ScheduledTask -InputObject $existing | Out-Null
} else {
    $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
    $settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries -StartWhenAvailable -ExecutionTimeLimit $timeLimit `
        -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger (New-WatchdogTrigger) `
        -Principal $principal -Settings $settings `
        -Description 'Start the local Runway API and production web server at logon and restart a crashed component; no publishing is triggered.' | Out-Null
}
Write-Host "Installed $TaskName -> $launcher. Run now with Start-ScheduledTask -TaskName '$TaskName'."
Write-Host "To keep Runway stopped, run Disable-ScheduledTask -TaskName '$TaskName' before stopping it."
