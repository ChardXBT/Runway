param([string]$TaskName = 'A_Runway_Task')
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$launcher = Join-Path $root 'run-runway.ps1'
$powerShell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$action = New-ScheduledTaskAction -Execute $powerShell -WorkingDirectory $root -Argument (
    '-NoLogo -NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "{0}"' -f $launcher
)
$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    $backup = Join-Path $root 'data\runtime'
    $null = New-Item -ItemType Directory -Force -Path $backup
    Export-ScheduledTask -TaskName $TaskName | Set-Content -LiteralPath (
        Join-Path $backup ('scheduled-task-before-{0}.xml' -f (Get-Date -Format 'yyyyMMdd-HHmmss'))
    )
    # Preserve triggers/account, but local loopback services do not need elevation.
    $existing.Actions = @($action)
    $existing.Principal.RunLevel = 0 # Limited, consistent with manual/Supervisor launch
    $existing.Settings.RunOnlyIfNetworkAvailable = $false
    $existing.Settings.MultipleInstances = 2 # IgnoreNew
    Set-ScheduledTask -InputObject $existing | Out-Null
} else {
    $principal = New-ScheduledTaskPrincipal -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited
    $settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 5)
    Register-ScheduledTask -TaskName $TaskName -Action $action -Principal $principal -Settings $settings -Description 'Start the local Runway API and production web server; no publishing is triggered.' | Out-Null
}
Write-Host "Installed $TaskName -> $launcher. Run with Start-ScheduledTask -TaskName '$TaskName'."
