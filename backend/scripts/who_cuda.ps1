$ErrorActionPreference = 'SilentlyContinue'
Get-Process -Id 28968,22156,26936 -ErrorAction SilentlyContinue | ForEach-Object {
  $p = $_
  $cmd = (Get-CimInstance Win32_Process -Filter "ProcessId=$($p.Id)").CommandLine
  [PSCustomObject]@{ Id = $p.Id; Name = $p.ProcessName; Started = $p.StartTime; Cmd = $cmd }
} | Format-List
