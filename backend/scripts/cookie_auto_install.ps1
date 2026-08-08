# One-click cookie-extension auto-install (productized CookieInstallWorker flow).
#
# Proven mechanism, unchanged in substance: (1) close the browser, (2) relaunch
# it with --remote-debugging-port + an EXPLICIT --user-data-dir (Chrome 136+
# ignores the debug flag on the default profile without it), (3) drive
# chrome://extensions over CDP — real Input.dispatchMouseEvent clicks, because
# a synthetic JS .click() never opens the native folder dialog, (4) drive the
# #32770 folder dialog 100% by Win32 (WM_SETTEXT the "Pasta:" edit, BM_CLICK
# the "Selecionar pasta" button), (5) verify the extension card, (6) close the
# debug instance and relaunch the browser normally so the extension persists.
#
# stdlib/BCL only: ClientWebSocket for CDP, Add-Type P/Invoke for the dialog,
# Invoke-RestMethod for the /json endpoints. No npm/PyPI deps.
#
# Usage:
#   powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File `
#     cookie_auto_install.ps1 -ExtensionDir "C:\...\VOD.RIP-cookies" `
#     [-Browser chrome|msedge|brave] [-DebugPort 9222] [-DryRun]
#
# stdout: exactly ONE JSON line (the result), human progress goes to stderr:
#   {"ok":true,"installed":true,"extension_id":"...","error":null}
# DryRun prints the resolved plan without touching the browser.

param(
    [string]$ExtensionDir,
    [string]$Browser = 'chrome',
    [int]$DebugPort = 9222,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'

$EXT_NAME = 'VOD RIP Get Cookies'

# --- browser resolution ------------------------------------------------------
$browserMap = @{
    chrome = @{ exe = 'chrome.exe'; rel = 'Google\Chrome\Application\chrome.exe'; udd = 'Google\Chrome\User Data'; url = 'chrome://extensions/' }
    msedge = @{ exe = 'msedge.exe'; rel = 'Microsoft\Edge\Application\msedge.exe'; udd = 'Microsoft\Edge\User Data'; url = 'edge://extensions/' }
    brave  = @{ exe = 'brave.exe'; rel = 'BraveSoftware\Brave-Browser\Application\brave.exe'; udd = 'BraveSoftware\Brave-Browser\User Data'; url = 'chrome://extensions/' }
}
if (-not $browserMap.ContainsKey($Browser)) {
    Write-Output (@{ ok = $false; installed = $false; extension_id = ''; error = "unsupported browser '$Browser'" } | ConvertTo-Json -Compress)
    exit 1
}
$binfo = $browserMap[$Browser]

$exe = $null
foreach ($root in @($env:ProgramFiles, ${env:ProgramFiles(x86)})) {
    if ($root) {
        $candidate = Join-Path $root $binfo.rel
        if (Test-Path -LiteralPath $candidate) { $exe = $candidate; break }
    }
}
if (-not $exe) { $exe = (Get-Command $binfo.exe -ErrorAction SilentlyContinue).Source }
if (-not $exe -or -not (Test-Path -LiteralPath $exe)) {
    Write-Output (@{ ok = $false; installed = $false; extension_id = ''; error = 'browser not found' } | ConvertTo-Json -Compress)
    exit 1
}

$userData = Join-Path $env:LOCALAPPDATA $binfo.udd
$manifest = Join-Path $ExtensionDir 'manifest.json'
if (-not (Test-Path -LiteralPath $manifest)) {
    Write-Output (@{ ok = $false; installed = $false; extension_id = ''; error = "extension folder missing manifest.json: $ExtensionDir" } | ConvertTo-Json -Compress)
    exit 1
}

if ($DryRun) {
    Write-Output (@{
        ok = $true; dryRun = $true; browser = $Browser; browser_exe = $exe
        user_data_dir = $userData; extension_dir = $ExtensionDir; debug_port = $DebugPort
    } | ConvertTo-Json -Compress)
    exit 0
}

# --- Win32 P/Invoke (compiled once) ------------------------------------------
Add-Type -AssemblyName System.Windows.Forms
Add-Type @"
using System;
using System.Text;
using System.Runtime.InteropServices;
public static class AutoInstallNative {
  public delegate bool EnumProc(IntPtr hWnd, IntPtr lParam);
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc cb, IntPtr lParam);
  [DllImport("user32.dll")] public static extern bool EnumChildWindows(IntPtr parent, EnumProc cb, IntPtr lParam);
  [DllImport("user32.dll", CharSet = CharSet.Unicode)] public static extern int GetWindowText(IntPtr h, StringBuilder sb, int max);
  [DllImport("user32.dll", CharSet = CharSet.Unicode)] public static extern int GetClassName(IntPtr h, StringBuilder sb, int max);
  [DllImport("user32.dll", CharSet = CharSet.Unicode)] public static extern IntPtr SendMessage(IntPtr h, uint msg, IntPtr w, string l);
  [DllImport("user32.dll", CharSet = CharSet.Unicode)] public static extern IntPtr SendMessage(IntPtr h, uint msg, IntPtr w, StringBuilder l);
  [DllImport("user32.dll")] public static extern IntPtr SendMessage(IntPtr h, uint msg, IntPtr w, IntPtr l);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
}
"@

function Write-ProgressLog([string]$msg) { [Console]::Error.WriteLine($msg) }

# --- browser lifecycle --------------------------------------------------------
function Stop-BrowserProcesses {
    param([string]$name)
    # Graceful close first (saves the session for --restore-last-session),
    # then force-kill stragglers.
    foreach ($p in @(Get-Process -Name $name -ErrorAction SilentlyContinue)) {
        if ($p.MainWindowHandle -ne 0) { try { [void]$p.CloseMainWindow() } catch { } }
    }
    for ($i = 0; $i -lt 12; $i++) {
        if (@(Get-Process -Name $name -ErrorAction SilentlyContinue).Count -eq 0) { return }
        Start-Sleep -Milliseconds 500
    }
    foreach ($p in @(Get-Process -Name $name -ErrorAction SilentlyContinue)) {
        try { $p.Kill() } catch { }
    }
    Start-Sleep -Milliseconds 800
}

# --- CDP plumbing --------------------------------------------------------------
function Wait-CdpPort {
    param([int]$port, [int]$timeoutSec)
    $deadline = [DateTime]::UtcNow.AddSeconds($timeoutSec)
    while ([DateTime]::UtcNow -lt $deadline) {
        try {
            $v = Invoke-RestMethod -Uri "http://127.0.0.1:$port/json/version" -TimeoutSec 2 -ErrorAction Stop
            if ($v.webSocketDebuggerUrl) { return $true }
        } catch { Start-Sleep -Milliseconds 400 }
    }
    return $false
}

$script:cdpId = 0
function Invoke-Cdp {
    param($ws, [string]$method, $params)
    $script:cdpId++
    $id = $script:cdpId
    $obj = @{ id = $id; method = $method }
    if ($null -ne $params) { $obj.params = $params }
    $json = $obj | ConvertTo-Json -Depth 40 -Compress
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($json)
    $len = $bytes.Length
    if ($len -lt 126) {
        $header = New-Object byte[] 6
        $payloadOff = 6
    } else {
        $header = New-Object byte[] 8
        $header[2] = [byte](($len -shr 8) -band 0xFF)
        $header[3] = [byte]($len -band 0xFF)
        $payloadOff = 8
    }
    $header[0] = 0x81  # FIN + text
    if ($len -lt 126) { $header[1] = 0x80 -bor $len } else { $header[1] = 0x80 -bor 126 }
    $mask = 0x11, 0x22, 0x33, 0x44
    for ($i = 0; $i -lt 4; $i++) { $header[$payloadOff - 4 + $i] = $mask[$i] }
    $frame = New-Object byte[] ($payloadOff + $len)
    [Array]::Copy($header, 0, $frame, 0, $payloadOff)
    for ($i = 0; $i -lt $len; $i++) { $frame[$payloadOff + $i] = $bytes[$i] -bxor $mask[$i % 4] }
    $ct = [System.Threading.CancellationToken]::None
    $ws.SendAsync([ArraySegment[byte]]::new($frame), [System.Net.WebSockets.WebSocketMessageType]::Binary, $true, $ct).GetAwaiter().GetResult()
    # Read frames until the reply carrying our id (events interleave freely).
    $buffer = New-Object byte[] 524288
    while ($true) {
        $sb = [System.Text.StringBuilder]::new()
        do {
            $cts = [System.Threading.CancellationTokenSource]::new([TimeSpan]::FromSeconds(30))
            $res = $ws.ReceiveAsync([ArraySegment[byte]]::new($buffer), $cts.Token).GetAwaiter().GetResult()
            if ($res.MessageType -eq [System.Net.WebSockets.WebSocketMessageType]::Close) { throw 'CDP websocket closed by browser' }
            [void]$sb.Append([System.Text.Encoding]::UTF8.GetString($buffer, 0, $res.Count))
        } while (-not $res.EndOfMessage)
        $msg = $sb.ToString() | ConvertFrom-Json
        if ($msg.id -eq $id) { return $msg }
    }
}

function Invoke-Eval {
    param($ws, [string]$expression, [bool]$awaitPromise = $false)
    $resp = Invoke-Cdp $ws 'Runtime.evaluate' @{
        expression = $expression; returnByValue = $true; awaitPromise = $awaitPromise; userGesture = $true
    }
    if ($resp.error) { throw ("CDP error: " + ($resp.error | ConvertTo-Json -Compress)) }
    if ($resp.result.exceptionDetails) { throw ("page exception: " + $resp.result.exceptionDetails.text) }
    return $resp.result.result.value
}

function Send-MouseClick {
    param($ws, [double]$x, [double]$y)
    $click = @{ x = $x; y = $y; button = 'left'; clickCount = 1 }
    [void](Invoke-Cdp $ws 'Input.dispatchMouseEvent' @{ type = 'mousePressed'; x = $x; y = $y; button = 'left'; clickCount = 1 })
    [void](Invoke-Cdp $ws 'Input.dispatchMouseEvent' @{ type = 'mouseReleased'; x = $x; y = $y; button = 'left'; clickCount = 1 })
}

# --- chrome://extensions probes ------------------------------------------------
$script:deepFinder = @'
(() => {
  function deep(root, sel) {
    if (!root || !root.querySelector) return null;
    let el = root.querySelector(sel);
    if (el) return el;
    for (const c of root.children || []) {
      if (c.shadowRoot) { el = deep(c.shadowRoot, sel); if (el) return el; }
    }
    return null;
  }
  const mgr = document.querySelector('extensions-manager');
  if (!mgr) return null;
  return { root: mgr.shadowRoot ? mgr.shadowRoot : null, find: (sel) => deep(mgr.shadowRoot, sel) };
})()
'@

function Get-DevModeState {
    param($ws)
    return Invoke-Eval $ws @'
(() => {
  function deep(root, sel) {
    if (!root || !root.querySelector) return null;
    let el = root.querySelector(sel);
    if (el) return el;
    for (const c of root.children || []) {
      if (c.shadowRoot) { el = deep(c.shadowRoot, sel); if (el) return el; }
    }
    return null;
  }
  const mgr = document.querySelector('extensions-manager');
  if (!mgr) return { ok: false, reason: 'no-manager' };
  const row = deep(mgr.shadowRoot, '#devMode');
  if (!row) return { ok: false, reason: 'no-dev-mode' };
  const input = row.shadowRoot ? row.shadowRoot.querySelector('input[type=checkbox]') : null;
  if (!input) return { ok: false, reason: 'no-dev-input' };
  input.scrollIntoView({ block: 'center', behavior: 'instant' });
  const r = input.getBoundingClientRect();
  return { ok: true, checked: !!input.checked, x: r.x + r.width / 2, y: r.y + r.height / 2 };
})()
'@
}

function Get-LoadUnpackedSpot {
    param($ws)
    return Invoke-Eval $ws @'
(() => {
  function deep(root, sel) {
    if (!root || !root.querySelector) return null;
    let el = root.querySelector(sel);
    if (el) return el;
    for (const c of root.children || []) {
      if (c.shadowRoot) { el = deep(c.shadowRoot, sel); if (el) return el; }
    }
    return null;
  }
  const mgr = document.querySelector('extensions-manager');
  if (!mgr) return { ok: false, reason: 'no-manager' };
  const btn = deep(mgr.shadowRoot, '#loadUnpacked');
  if (!btn) return { ok: false, reason: 'no-load-unpacked' };
  btn.scrollIntoView({ block: 'center', behavior: 'instant' });
  const r = btn.getBoundingClientRect();
  return { ok: true, x: r.x + r.width / 2, y: r.y + r.height / 2 };
})()
'@
}

function Get-ExtCardState {
    param($ws)
    return Invoke-Eval $ws (@'
(() => {
  function deepText(root) {
    if (!root) return '';
    let s = root.textContent || '';
    for (const c of root.children || []) {
      if (c.shadowRoot) s += ' ' + deepText(c.shadowRoot);
    }
    return s;
  }
  const mgr = document.querySelector('extensions-manager');
  if (!mgr) return { found: false, error: false };
  const list = mgr.shadowRoot.querySelector('extensions-item-list');
  const items = list ? list.shadowRoot.querySelectorAll('extensions-item') : [];
  let found = false, error = false;
  for (const it of items) {
    const txt = deepText(it.shadowRoot).replace(/\s+/g, ' ');
    if (txt.indexOf('__EXT_NAME__') !== -1) found = true;
    if (/erro|error/i.test(txt)) error = true;
  }
  return { found: found, error: error };
})()
'@ -replace '__EXT_NAME__', $EXT_NAME)
}

function Get-ExtIdFromManagement {
    param($ws)
    return Invoke-Eval $ws (@'
chrome.management.getAll().then(exts => {
  const e = exts.find(x => x.name === '__EXT_NAME__');
  return { found: !!e, id: e ? e.id : '' };
})
'@ -replace '__EXT_NAME__', $EXT_NAME) $true
}

# --- native folder dialog (Win32) ----------------------------------------------
$script:dialogHwnd = [IntPtr]::Zero
function Find-FolderDialog {
    $script:dialogHwnd = [IntPtr]::Zero
    $cb = [AutoInstallNative+EnumProc]{ param($h, $lp)
        if ([AutoInstallNative]::IsWindowVisible($h)) {
            $t = [System.Text.StringBuilder]::new(256)
            [void][AutoInstallNative]::GetWindowText($h, $t, 256)
            $c = [System.Text.StringBuilder]::new(64)
            [void][AutoInstallNative]::GetClassName($h, $c, 64)
            if ($c.ToString() -eq '#32770' -and $t.ToString() -match 'Selec|Select|Pasta|Folder') {
                $script:dialogHwnd = $h
                return $false
            }
        }
        return $true
    }
    [void][AutoInstallNative]::EnumWindows($cb, [IntPtr]::Zero)
    return $script:dialogHwnd
}

function Get-DialogControls {
    param([IntPtr]$dialog)
    $script:ctrlList = [System.Collections.Generic.List[object]]::new()
    $cb = [AutoInstallNative+EnumProc]{ param($h, $lp)
        $c = [System.Text.StringBuilder]::new(64)
        [void][AutoInstallNative]::GetClassName($h, $c, 64)
        $cls = $c.ToString()
        if ($cls -eq 'Edit' -or $cls -eq 'Button') {
            $t = [System.Text.StringBuilder]::new(512)
            [void][AutoInstallNative]::GetWindowText($h, $t, 512)
            $script:ctrlList.Add([pscustomobject]@{ Handle = $h; Class = $cls; Text = $t.ToString() })
        }
        return $true
    }
    [void][AutoInstallNative]::EnumChildWindows($dialog, $cb, [IntPtr]::Zero)
    return , $script:ctrlList
}

function Set-PathInDialog {
    param([IntPtr]$dialog, [string]$path)
    $controls = Get-DialogControls $dialog
    foreach ($e in @($controls | Where-Object { $_.Class -eq 'Edit' })) {
        [void][AutoInstallNative]::SendMessage($e.Handle, 0x000C, [IntPtr]::Zero, $path)  # WM_SETTEXT
    }
    Start-Sleep -Milliseconds 400
    $landed = $false
    foreach ($e in @($controls | Where-Object { $_.Class -eq 'Edit' })) {
        $sb = [System.Text.StringBuilder]::new(1024)
        [void][AutoInstallNative]::SendMessage($e.Handle, 0x000D, [IntPtr]::new(1024), $sb)  # WM_GETTEXT
        if ($sb.ToString().Trim() -ne '') { $landed = $true; break }
    }
    return $landed
}

function Click-DialogConfirm {
    param([IntPtr]$dialog)
    $controls = Get-DialogControls $dialog
    $btn = @($controls | Where-Object { $_.Class -eq 'Button' -and $_.Text -match 'Selecionar|Select' } | Select-Object -First 1)
    if (-not $btn) { $btn = @($controls | Where-Object { $_.Class -eq 'Button' -and $_.Text -match '^(OK|Abrir|Open)$' } | Select-Object -First 1) }
    if (-not $btn) { return $false }
    [void][AutoInstallNative]::SendMessage($btn[0].Handle, 0x00F5, [IntPtr]::Zero, [IntPtr]::Zero)  # BM_CLICK
    return $true
}

function Wait-DialogClosed {
    param([int]$timeoutSec)
    $deadline = [DateTime]::UtcNow.AddSeconds($timeoutSec)
    while ([DateTime]::UtcNow -lt $deadline) {
        Start-Sleep -Milliseconds 400
        $d = Find-FolderDialog
        if ($d -eq [IntPtr]::Zero) { return $true }
    }
    return $false
}

function Install-Extension {
    param($ws, [string]$path)
    for ($attempt = 1; $attempt -le 2; $attempt++) {
        $spot = Get-LoadUnpackedSpot $ws
        if (-not $spot.ok) { throw "load unpacked button not found: $($spot.reason)" }
        Send-MouseClick $ws $spot.x $spot.y
        # wait for the native folder dialog
        $dialog = [IntPtr]::Zero
        $deadline = [DateTime]::UtcNow.AddSeconds(20)
        while ([DateTime]::UtcNow -lt $deadline) {
            Start-Sleep -Milliseconds 300
            $dialog = Find-FolderDialog
            if ($dialog -ne [IntPtr]::Zero) { break }
        }
        if ($dialog -eq [IntPtr]::Zero) { throw 'native folder dialog did not open' }
        Start-Sleep -Milliseconds 600
        $landed = Set-PathInDialog $dialog $path
        if (-not $landed) { Write-ProgressLog "attempt ${attempt}: path edit did not accept WM_SETTEXT — retrying" }
        if (-not (Click-DialogConfirm $dialog)) { Write-ProgressLog "attempt ${attempt}: confirm button not found — retrying" }
        if (-not (Wait-DialogClosed 12)) {
            Write-ProgressLog "attempt ${attempt}: dialog still open after confirm — retrying"
            # dismiss the stale dialog (Esc) so the next attempt starts clean
            [void][AutoInstallNative]::SetForegroundWindow($dialog)
            Start-Sleep -Milliseconds 200
            [System.Windows.Forms.SendKeys]::SendWait('{ESC}')
            Start-Sleep -Milliseconds 800
            continue
        }
        # extension load is async — poll the card
        $deadline = [DateTime]::UtcNow.AddSeconds(25)
        while ([DateTime]::UtcNow -lt $deadline) {
            Start-Sleep -Milliseconds 700
            $card = Get-ExtCardState $ws
            if ($card.found) { return $card }
        }
        Write-ProgressLog "attempt ${attempt}: card not detected after dialog close"
    }
    return $null
}

# --- main -----------------------------------------------------------------------
$result = @{ ok = $false; installed = $false; extension_id = ''; error = $null }
try {
    Write-ProgressLog "auto-install: closing $Browser"
    Stop-BrowserProcesses $Browser

    Write-ProgressLog "auto-install: launching debug instance on :$DebugPort"
    $launchArgs = "--remote-debugging-port=$DebugPort --remote-allow-origins=* --user-data-dir=`"$userData`" --no-first-run --no-default-browser-check $($binfo.url)"
    [void](Start-Process -FilePath $exe -ArgumentList $launchArgs)

    if (-not (Wait-CdpPort $DebugPort 25)) { throw 'browser did not expose the debug port' }
    $targets = @(Invoke-RestMethod -Uri "http://127.0.0.1:$DebugPort/json" -TimeoutSec 5 -ErrorAction Stop)
    $target = $targets | Where-Object { $_.type -eq 'page' -and $_.url -match '(chrome|edge)://extensions' } | Select-Object -First 1
    if (-not $target) {
        Write-ProgressLog "auto-install: extensions tab not found, opening it via CDP"
        $enc = [Uri]::EscapeDataString($binfo.url)
        try {
            $new = Invoke-RestMethod -Method Put -Uri "http://127.0.0.1:$DebugPort/json/new?$enc" -TimeoutSec 5 -ErrorAction Stop
            $target = $new
        } catch { }
    }
    if (-not $target -or -not $target.webSocketDebuggerUrl) {
        # last resort: evaluate nothing — just re-query until the tab settles
        for ($i = 0; $i -lt 8 -and -not $target; $i++) {
            Start-Sleep -Milliseconds 800
            $targets = @(Invoke-RestMethod -Uri "http://127.0.0.1:$DebugPort/json" -TimeoutSec 5 -ErrorAction Stop)
            $target = $targets | Where-Object { $_.type -eq 'page' -and $_.url -match '(chrome|edge)://extensions' } | Select-Object -First 1
        }
    }
    if (-not $target) { throw 'extensions page target not found' }

    $ws = [System.Net.WebSockets.ClientWebSocket]::new()
    $ws.ConnectAsync([Uri]$target.webSocketDebuggerUrl, [System.Threading.CancellationToken]::None).GetAwaiter().GetResult()
    try {
        # ensure Developer mode
        for ($i = 0; $i -lt 3; $i++) {
            $dev = Get-DevModeState $ws
            if (-not $dev.ok) { throw "developer-mode toggle not found: $($dev.reason)" }
            if ($dev.checked) { break }
            Send-MouseClick $ws $dev.x $dev.y
            Start-Sleep -Milliseconds 800
            $dev = Get-DevModeState $ws
            if ($dev.checked) { break }
        }
        $dev = Get-DevModeState $ws
        if (-not $dev.checked) { throw 'could not enable developer mode' }
        Write-ProgressLog "auto-install: developer mode ON"

        # already loaded? (idempotent re-run) — skip the dialog dance
        $card = Get-ExtCardState $ws
        if (-not $card.found) {
            $card = Install-Extension $ws $ExtensionDir
            if ($null -eq $card) { throw 'extension did not load — folder dialog may have rejected the path' }
        } else {
            Write-ProgressLog "auto-install: extension card already present"
        }
        $mgmt = Get-ExtIdFromManagement $ws
        $result.ok = $true
        $result.installed = $true
        $result.extension_id = if ($mgmt.id) { $mgmt.id } else { '' }
        Write-ProgressLog "auto-install: verified ('$EXT_NAME' loaded)"
    } finally {
        try { $ws.Dispose() } catch { }
    }
} catch {
    $result.error = $_.Exception.Message
    Write-ProgressLog "auto-install error: $($result.error)"
} finally {
    # the user's browser must come back no matter what happened
    try {
        Stop-BrowserProcesses $Browser
        [void](Start-Process -FilePath $exe -ArgumentList '--restore-last-session')
        Write-ProgressLog "auto-install: relaunched $Browser normally"
    } catch {
        if (-not $result.error) { $result.error = "relaunch failed: $($_.Exception.Message)" }
    }
    Write-Output ($result | ConvertTo-Json -Compress)
}
