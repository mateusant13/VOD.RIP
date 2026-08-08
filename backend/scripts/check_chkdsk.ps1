$ErrorActionPreference = 'SilentlyContinue'
Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager' |
  Select-Object BootExecute | Format-List
'--- events ---'
Get-WinEvent -FilterHashtable @{LogName='Application'} -MaxEvents 200 |
  Where-Object { $_.Id -in 1001,26226,26212,98 } |
  Select-Object -First 12 TimeCreated, Id, ProviderName |
  Format-Table -AutoSize
'--- last boot ---'
(Get-CimInstance Win32_OperatingSystem).LastBootUpTime
