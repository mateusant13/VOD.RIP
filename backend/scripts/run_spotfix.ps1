$ErrorActionPreference = 'Continue'
'--- Repair-Volume SpotFix (online, no reboot) ---'
try {
  $r = Repair-Volume -DriveLetter C -SpotFix -Verbose 4>&1
  $r | ForEach-Object { $_ }
} catch {
  "EXCEPTION: $($_.Exception.Message)"
}
'--- dirty after spotfix ---'
fsutil dirty query C:
