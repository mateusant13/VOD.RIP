param(
    [string]$DownloadId = "dl_c52beb4356ca",
    [int]$MaxSeconds = 7200
)

$uri = "http://127.0.0.1:7897/api/download/$DownloadId/stream"
$statusUri = "http://127.0.0.1:7897/api/download/$DownloadId"

Write-Host "Monitoring download $DownloadId (max ${MaxSeconds}s)"
Write-Host "SSE: $uri"
Write-Host ""

$lastPct = -1
$lastStatus = ""
$start = Get-Date
$muxingSeen = $false
$finalisingSeen = $false
$postProcessEvents = @()

try {
    $req = [System.Net.HttpWebRequest]::Create($uri)
    $req.Method = "GET"
    $req.Timeout = [int]::MaxValue
    $resp = $req.GetResponse()
    $stream = $resp.GetResponseStream()
    $reader = New-Object System.IO.StreamReader($stream)

    while (((Get-Date) - $start).TotalSeconds -lt $MaxSeconds) {
        $line = $reader.ReadLine()
        if ($null -eq $line) { break }
        if ($line -notmatch "^data: ") { continue }

        $json = $line.Substring(6)
        try {
            $evt = $json | ConvertFrom-Json
        } catch {
            continue
        }

        $type = $evt.type
        $val = $evt.data
        $ts = (Get-Date).ToString("HH:mm:ss")

        if ($type -eq "progress") {
            $pct = [int]$val
            if ($pct -ne $lastPct) {
                Write-Host "[$ts] progress: $pct%"
                $lastPct = $pct
            }
        }
        elseif ($type -eq "status") {
            $status = [string]$val
            if ($status -ne $lastStatus) {
                Write-Host "[$ts] status: $status"
                $lastStatus = $status
                if ($status -match "Muxing|Postprocess|Encoding|Remuxing|Finalis") {
                    $postProcessEvents += "[$ts] $status"
                    if ($status -match "Muxing") { $muxingSeen = $true }
                    if ($status -match "Finalis") { $finalisingSeen = $true }
                }
            }
        }
        elseif ($type -eq "complete") {
            Write-Host "[$ts] COMPLETE at $val%"
            break
        }
        elseif ($type -eq "error") {
            Write-Host "[$ts] ERROR: $val"
            break
        }
    }
}
catch {
    Write-Host "SSE ended or error: $_"
}

# Final poll
try {
    $dl = Invoke-RestMethod -Uri $statusUri
    Write-Host ""
    Write-Host "=== Final state ==="
    Write-Host "status: $($dl.status)"
    Write-Host "progress: $($dl.progress)%"
    if ($dl.error) { Write-Host "error: $($dl.error)" }
    if ($dl.output_file) { Write-Host "output: $($dl.output_file)" }
    if (Test-Path $dl.output_file) {
        $sz = (Get-Item $dl.output_file).Length
        Write-Host "file size: $([math]::Round($sz/1GB, 2)) GB"
    }
}
catch {
    Write-Host "Could not fetch final state: $_"
}

Write-Host ""
Write-Host "=== Post-process events captured ==="
if ($postProcessEvents.Count -eq 0) {
    Write-Host "(none yet - download may still be in downloading phase)"
} else {
    $postProcessEvents | ForEach-Object { Write-Host $_ }
}
Write-Host ""
Write-Host "Muxing seen: $muxingSeen"
Write-Host "Finalising seen: $finalisingSeen"
