$ErrorActionPreference = 'SilentlyContinue'
'--- System log: chkdsk/autochk/ntfs recent ---'
Get-WinEvent -FilterHashtable @{LogName='System'} -MaxEvents 500 |
  Where-Object { $_.ProviderName -match 'Ntfs|disk|volmgr' -or $_.Id -in 1001,26226 } |
  Select-Object -First 10 TimeCreated, Id, ProviderName, @{n='Msg';e={$_.Message.Substring(0,[Math]::Min(120,$_.Message.Length))}} |
  Format-List
'--- Fast startup (HiberbootEnabled) ---'
Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager\Power' |
  Select-Object HiberbootEnabled | Format-List
'--- hibernate configured ---'
powercfg /a 2>&1 | Select-String 'Hiberna|Hibernate|Suspensão|Fast' | Select-Object -First 5
