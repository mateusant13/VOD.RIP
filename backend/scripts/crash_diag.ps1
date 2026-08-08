$os = Get-CimInstance Win32_OperatingSystem
$total = [math]::Round($os.TotalVisibleMemorySize / 1MB, 1)
$free = [math]::Round($os.FreePhysicalMemory / 1MB, 1)
$used = [math]::Round($total - $free, 1)
Write-Host ("RAM: {0}GB total | {1}GB used ({2}%) | {3}GB free" -f $total, $used, [math]::Round($used / $total * 100), $free)
Write-Host "--- Top 12 por RAM ---"
Get-Process | Sort-Object WS -Descending | Select-Object -First 12 `
  Name, Id, @{N='RAM_MB'; E={[math]::Round($_.WS / 1MB)}} |
  Format-Table -AutoSize | Out-String -Width 100
Write-Host "--- Eventos System nivel Error/Warning (30min) ---"
Get-WinEvent -FilterHashtable @{LogName='System'; Level=1,2; StartTime=(Get-Date).AddMinutes(-30)} -MaxEvents 10 -ErrorAction SilentlyContinue |
  ForEach-Object {
    $first = ($_.Message -split "`r?`n" | Where-Object { $_ -and $_ -notmatch '^\s*$' } | Select-Object -First 1)
    Write-Host ("{0:HH:mm:ss} [{1}] {2}: {3}" -f $_.TimeCreated, $_.Id, $_.ProviderName, $first)
  }
Write-Host "--- Eventos Application nivel Error (30min) ---"
Get-WinEvent -FilterHashtable @{LogName='Application'; Level=2; StartTime=(Get-Date).AddMinutes(-30)} -MaxEvents 8 -ErrorAction SilentlyContinue |
  ForEach-Object {
    $first = ($_.Message -split "`r?`n" | Where-Object { $_ -and $_ -notmatch '^\s*$' } | Select-Object -First 1)
    Write-Host ("{0:HH:mm:ss} [{1}] {2}: {3}" -f $_.TimeCreated, $_.Id, $_.ProviderName, $first)
  }
