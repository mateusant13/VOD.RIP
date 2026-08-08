$ErrorActionPreference = 'SilentlyContinue'
'--- listening ports of the three pythons ---'
$rows = @()
foreach ($procId in 22156, 26936, 28968) {
  $conns = Get-NetTCPConnection -OwningProcess $procId -State Listen -ErrorAction SilentlyContinue
  foreach ($c in $conns) {
    $rows += [PSCustomObject]@{ Pid = $procId; Port = $c.LocalPort; Addr = $c.LocalAddress }
  }
}
$rows | Format-Table -AutoSize
'--- all python processes ---'
Get-Process python* -ErrorAction SilentlyContinue | ForEach-Object {
  $cmd = (Get-CimInstance Win32_Process -Filter "ProcessId=$($_.Id)").CommandLine
  [PSCustomObject]@{ Id = $_.Id; Cmd = $cmd }
} | Format-List
