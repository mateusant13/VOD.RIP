$ErrorActionPreference = 'SilentlyContinue'
'--- schedule Windows Memory Diagnostic on next boot ---'
try {
  $p = Start-Process -FilePath 'C:\Windows\System32\MdSched.exe' -ArgumentList '/r' -Verb RunAs -PassThru
  Start-Sleep -Seconds 2
  if ($p -and !$p.HasExited) { $p.Kill() }
  'mdsched started'
} catch {
  "mdsched EXCEPTION: $($_.Exception.Message)"
}
'--- verify scheduled ---'
Get-CimInstance Win32_ComputerSystem | Select-Object -ExpandProperty Model | Out-Null
$wmd = Get-CimInstance -Namespace root\cimv2 -ClassName Win32_Process -Filter "Name='MdSched.exe'" -ErrorAction SilentlyContinue
if ($wmd) { $wmd | Select-Object ProcessId, CommandLine | Format-List } else { 'no mdsched process (may have exited after scheduling)' }
'--- BootExecute current ---'
(Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager').BootExecute
