# VOD.RIP cookie bridge - zero-click extension install (HKCU policy, no admin).
#
# Installs the packed extension (dist\extension.crx from the Get-cookies.txt-LOCALLY
# fork) as a force-installed extension for Chrome AND Edge via the
# ExtensionInstallForcelist user policy, pointing at the local backend's
# update manifest so the browser can fetch + install the crx automatically.
#
# The extension id is derived from dist\extension.pem (the pack key) - the
# same key signs future updates, so the id is stable across rebuilds.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File scripts\install-cookie-bridge-policy.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\install-cookie-bridge-policy.ps1 -Uninstall
#
# Switches:
#   -ExtensionDist <dir>   Where extension.crx + extension.pem live (default:
#                          the sibling Get-cookies.txt-LOCALLY checkout, or the
#                          VODRIP_EXT_DIST env var).
#   -UpdateUrl <url>       Update manifest URL written into the policy (default:
#                          http://127.0.0.1:7897/api/session/cookies/extension/update.xml).
#                          Override only for testing against another port.
param(
    [switch]$Uninstall,
    [string]$ExtensionDist = "",
    [string]$UpdateUrl = "http://127.0.0.1:7897/api/session/cookies/extension/update.xml"
)

$ErrorActionPreference = "Stop"

if (-not $ExtensionDist) {
    $envDist = $env:VODRIP_EXT_DIST
    if ($envDist) {
        $ExtensionDist = $envDist
    }
    else {
        $ExtensionDist = Join-Path (Split-Path (Split-Path $PSScriptRoot -Parent) -Parent) "Get-cookies.txt-LOCALLY\dist"
    }
}

$appDataDir = Join-Path $env:APPDATA "VOD.RIP\cookie-extension"
$crxSrc = Join-Path $ExtensionDist "extension.crx"
$pemSrc = Join-Path $ExtensionDist "extension.pem"

if (-not (Test-Path $crxSrc) -or -not (Test-Path $pemSrc)) {
    Write-Error "Packed extension not found in $ExtensionDist (need extension.crx + extension.pem). Pack it first: chrome --pack-extension=<repo>\src, then move the artifacts into dist\."
    exit 1
}

# --- extension id from the pack key ---
# chrome --pack-extension writes the key as a PKCS#8 "BEGIN PRIVATE KEY". The
# extension id hashes the PUBLIC half (SPKI DER), so derive it with DER
# parsing (no external modules). "BEGIN PUBLIC KEY" pems work too.
function Read-DerTlv([byte[]]$data, [int]$start, [ref]$next) {
    $tag = $data[$start]
    $ln = $data[$start + 1]
    $off = $start + 2
    if (($ln -band 0x80) -ne 0) {
        $n = $ln -band 0x7F
        $lenBytes = New-Object byte[] $n
        [Array]::Copy($data, $off, $lenBytes, 0, $n)
        $ln = 0
        foreach ($b in $lenBytes) { $ln = ($ln -shl 8) -bor $b }
        $off += $n
    }
    $content = New-Object byte[] $ln
    [Array]::Copy($data, $off, $content, 0, $ln)
    $next.Value = $off + $ln
    return ,@($tag, $content)
}

function New-DerLen([int]$len) {
    if ($len -lt 0x80) { return ,[byte[]]@([byte]$len) }
    $bytes = New-Object System.Collections.Generic.List[byte]
    while ($len -gt 0) { $bytes.Insert(0, [byte]($len -band 0xFF)); $len = $len -shr 8 }
    $out = New-Object System.Collections.Generic.List[byte]
    $out.Add([byte](0x80 -bor $bytes.Count))
    $out.AddRange($bytes)
    return ,$out.ToArray()
}

function New-DerTlv([byte]$tag, [byte[]]$content) {
    $out = New-Object System.Collections.Generic.List[byte]
    $out.Add($tag)
    $out.AddRange((New-DerLen $content.Length))
    $out.AddRange($content)
    return ,$out.ToArray()
}

function Get-SpkiFromPkcs8([byte[]]$pkcs8) {
    $off = 0
    $t = Read-DerTlv $pkcs8 0 ([ref]$null)          # outer SEQUENCE
    $outer = $t[1]
    $t = Read-DerTlv $outer 0 ([ref]$off)           # INTEGER version
    $t = Read-DerTlv $outer $off ([ref]$off)        # AlgorithmIdentifier
    $t = Read-DerTlv $outer $off ([ref]$null)       # OCTET STRING { RSAPrivateKey }
    $rsaBody = $t[1]
    $t = Read-DerTlv $rsaBody 0 ([ref]$off)         # SEQUENCE { version, n, e, ... }
    $seqBody = $t[1]
    $t = Read-DerTlv $seqBody 0 ([ref]$off)         # INTEGER version
    $t = Read-DerTlv $seqBody $off ([ref]$off)      # INTEGER modulus
    $n = $t[1]
    $t = Read-DerTlv $seqBody $off ([ref]$null)     # INTEGER public exponent
    $e = $t[1]
    $oid = [byte[]](0x2a, 0x86, 0x48, 0x86, 0xf7, 0x0d, 0x01, 0x01, 0x01)  # rsaEncryption
    $algo = New-DerTlv 0x30 ((New-DerTlv 0x06 $oid) + (New-DerTlv 0x05 ([byte[]]::new(0))))
    $inner = New-DerTlv 0x30 ((New-DerTlv 0x02 $n) + (New-DerTlv 0x02 $e))
    $spki = New-DerTlv 0x30 ($algo + (New-DerTlv 0x03 ([byte[]](@(0) + $inner))))
    return ,$spki
}

$pem = Get-Content -Raw $pemSrc
$b64 = (($pem -split "\`r?\`n") | Where-Object { $_ -and $_ -notmatch "^-----" }) -join ""
$der = [Convert]::FromBase64String($b64)
$off = 0
$t = Read-DerTlv $der 0 ([ref]$null)
$t = Read-DerTlv $t[1] 0 ([ref]$off)
if ($t[0] -eq 0x02) { $der = Get-SpkiFromPkcs8 $der }   # PRIVATE KEY pem
$sha = [System.Security.Cryptography.SHA256]::Create()
$digest = $sha.ComputeHash($der)
$alphabet = "abcdefghijklmnop"
$extId = -join (0..15 | ForEach-Object {
    $b = $digest[$_]
    $alphabet[($b -shr 4)] + $alphabet[($b -band 0x0F)]
})

Write-Host "Extension id: $extId"

$policyRoots = @(
    "HKCU:\Software\Policies\Google\Chrome",
    "HKCU:\Software\Policies\Microsoft\Edge"
)

if ($Uninstall) {
    foreach ($root in $policyRoots) {
        $key = Join-Path $root "ExtensionInstallForcelist"
        if (Test-Path $key) {
            foreach ($name in (Get-Item $key).Property) {
                $entry = (Get-ItemProperty -Path $key -Name $name).$name
                if ($entry -and $entry.StartsWith($extId)) {
                    Remove-ItemProperty -Path $key -Name $name
                    Write-Host "Removed policy entry $name from $key"
                }
            }
            if (((Get-Item $key).Property | Measure-Object).Count -eq 0) {
                Remove-Item -Path $key -Force
                Write-Host "Removed empty policy key $key"
            }
        }
    }
    Write-Host "Cookie bridge policy removed. The extension stays installed in existing profiles until Chrome/Edge restart."
    exit 0
}

# --- install: copy the packed artifacts into the app data dir first ---
New-Item -ItemType Directory -Force -Path $appDataDir | Out-Null
Copy-Item -Force $crxSrc (Join-Path $appDataDir "extension.crx")
Copy-Item -Force $pemSrc (Join-Path $appDataDir "extension.pem")
Write-Host "Copied extension artifacts to $appDataDir"

$entryValue = "$extId;$UpdateUrl"
foreach ($root in $policyRoots) {
    $key = Join-Path $root "ExtensionInstallForcelist"
    New-Item -ItemType Directory -Force -Path $root | Out-Null
    New-Item -ItemType Directory -Force -Path $key | Out-Null

    # Reuse an existing entry for this extension id; otherwise take the first
    # free value name (1, 2, ...) so pre-existing forced extensions survive.
    $targetName = $null
    foreach ($name in (Get-Item $key).Property) {
        $entry = (Get-ItemProperty -Path $key -Name $name).$name
        if ($entry -and $entry.StartsWith($extId)) {
            $targetName = $name
            break
        }
    }
    if (-not $targetName) {
        $used = @(Get-Item $key).Property
        $targetName = "1"
        while ($used -contains $targetName) {
            $targetName = ([int]$targetName) + 1
        }
    }
    Set-ItemProperty -Path $key -Name $targetName -Type String -Value $entryValue
    Write-Host "Set $key\$targetName = $entryValue"
}

Write-Host ""
Write-Host "Done. Restart Chrome/Edge - the extension installs automatically (no admin, no prompts)."
Write-Host "Verify: chrome://extensions should list 'Get cookies.txt LOCALLY' as installed by policy."
