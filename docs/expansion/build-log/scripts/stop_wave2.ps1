$ErrorActionPreference = 'Continue'
$procs = Get-CimInstance Win32_Process
$watch = $procs | Where-Object { $_.Name -eq 'bash.exe' -and $_.CommandLine -and $_.CommandLine -match 'orchestrate-wave2\.sh|jobs-wave2\.txt' }
foreach ($p in $watch) { "stop watchdog bash $($p.ProcessId)"; Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue }
$kiro = $procs | Where-Object { $_.Name -eq 'kiro-cli.exe' -and $_.CommandLine -match 'Job name: \*\*rwy-realtime\*\*|Job rwy-realtime:' }
function Stop-Tree($id) {
  Get-CimInstance Win32_Process | Where-Object { $_.ParentProcessId -eq $id } | ForEach-Object { Stop-Tree $_.ProcessId }
  Stop-Process -Id $id -Force -ErrorAction SilentlyContinue
  "stopped $id"
}
foreach ($k in $kiro) { Stop-Tree $k.ProcessId }
