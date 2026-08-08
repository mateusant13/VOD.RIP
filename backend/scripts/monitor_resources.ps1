# Resource monitor — samples CPU/RAM every 2s for N seconds, reports the worst offenders.
param([int]$Seconds = 60)
$samples = @()
$deadline = (Get-Date).AddSeconds($Seconds)
$pids = @{}
while ((Get-Date) -lt $deadline) {
    $proc = Get-Process | Where-Object { $_.Id -gt 4 }
    foreach ($p in $proc) {
        $key = $p.Id
        if ($pids.ContainsKey($key)) {
            $oldCpu = $pids[$key].cpu
            $dt = ((Get-Date) - $pids[$key].t).TotalSeconds
            $rate = if ($dt -gt 0) { ($p.CPU - $oldCpu) / $dt } else { 0 }
            $samples += [pscustomobject]@{
                Name = $p.ProcessName; Id = $key; CpuPct = [math]::Round([math]::Max(0, $rate) * 100)
                RamMB = [math]::Round($p.WS / 1MB)
            }
        }
        $pids[$key] = @{ cpu = $p.CPU; t = Get-Date }
    }
    Start-Sleep -Seconds 2
}
$os = Get-CimInstance Win32_OperatingSystem
$total = [math]::Round($os.TotalVisibleMemorySize / 1MB, 1)
$used = [math]::Round($total - [math]::Round($os.FreePhysicalMemory / 1MB, 1), 1)
Write-Host ("RAM final: {0}GB used / {1}GB ({2}%)" -f $used, $total, [math]::Round($used / $total * 100))
Write-Host "--- Pico de CPU (media amostral, % de 1 core) ---"
$samples | Group-Object Id | ForEach-Object {
    $g = $_
    [pscustomobject]@{
        Name = $g.Group[0].Name; Id = $g.Name; MaxCpu = ($g.Group | Measure-Object CpuPct -Maximum).Maximum
        AvgRam = [math]::Round(($g.Group | Measure-Object RamMB -Average).Average)
    }
} | Sort-Object MaxCpu -Descending | Select-Object -First 15 | Format-Table -AutoSize | Out-String -Width 100
Write-Host "--- Pico de RAM (MB) ---"
$samples | Group-Object Id | ForEach-Object {
    $g = $_
    [pscustomobject]@{
        Name = $g.Group[0].Name; Id = $g.Name; MaxRam = ($g.Group | Measure-Object RamMB -Maximum).Maximum
        MaxCpu = ($g.Group | Measure-Object CpuPct -Maximum).Maximum
    }
} | Sort-Object MaxRam -Descending | Select-Object -First 15 | Format-Table -AutoSize | Out-String -Width 100
