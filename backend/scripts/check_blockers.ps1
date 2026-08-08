$ErrorActionPreference = 'SilentlyContinue'
'--- Smart App Control state ---'
Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\CI\Policy' |
  Select-Object VerifiedAndReputablePolicyState, @{n='SAC';e={$_.VerifiedAndReputablePolicyState}} | Format-List
'--- AppLocker enforced? ---'
$al = Get-AppLockerPolicy -Effective -ErrorAction SilentlyContinue
if ($al) { $al.RuleCollections | ForEach-Object { $_.Name + ': ' + ($_.Count) } } else { 'no AppLocker policy' }
'--- Fast startup ---'
Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager\Power' |
  Select-Object HiberbootEnabled | Format-List
'--- WDAC ---'
Get-CimInstance -ClassName Win32_DeviceGuard -Namespace root\Microsoft\Windows\DeviceGuard -ErrorAction SilentlyContinue |
  Select-Object VirtualizationBasedSecurityStatus, SecurityServicesConfigured, SecurityServicesRunning | Format-List
'--- try fsutil to see volume state ---'
fsutil dirty query C:
