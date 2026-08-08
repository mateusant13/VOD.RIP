Get-WinEvent -FilterHashtable @{LogName='Application'; Id=1000; StartTime=(Get-Date).AddMinutes(-40)} -MaxEvents 3 -ErrorAction SilentlyContinue |
  ForEach-Object {
    Write-Host ("==== {0} ====" -f $_.TimeCreated)
    Write-Host $_.Message
    Write-Host ""
  }
Write-Host "=== CHKDSK pendente? ==="
Get-ItemProperty -Path 'HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager' -Name PendingFileRenameOperations -ErrorAction SilentlyContinue |
  Select-Object -ExpandProperty PendingFileRenameOperations -ErrorAction SilentlyContinue | Select-Object -First 8
Write-Host "=== LastBoot ==="
(Get-CimInstance Win32_OperatingSystem).LastBootUpTime
