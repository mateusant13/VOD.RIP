$os = Get-CimInstance Win32_OperatingSystem
$total = [math]::Round($os.TotalVisibleMemorySize / 1MB, 1)
$free = [math]::Round($os.FreePhysicalMemory / 1MB, 1)
$used = [math]::Round($total - $free, 1)
Write-Host ("RAM: {0}GB total | {1}GB used ({2}%) | {3}GB free" -f $total, $used, [math]::Round($used / $total * 100), $free)
$cpus = (Get-CimInstance Win32_Processor | Measure-Object -Property NumberOfLogicalProcessors -Sum).Sum
Write-Host ("CPU logical cores: {0}" -f $cpus)
Write-Host "--- Top 18 por RAM (WS) ---"
Get-Process | Sort-Object WS -Descending | Select-Object -First 18 `
  Name, Id, @{N='RAM_MB'; E={[math]::Round($_.WS / 1MB)}}, @{N='CPU_sec'; E={[math]::Round($_.CPU)}} |
  Format-Table -AutoSize | Out-String -Width 120
