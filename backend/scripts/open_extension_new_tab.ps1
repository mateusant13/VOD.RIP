# Opens the browser's extensions manager in a NEW tab of a RUNNING Chromium
# browser — never reuses or navigates the active tab.
#
# Two-step flow, exactly what the user would do by hand: (1) the browser window
# comes to the front and a NEW tab opens (Ctrl+T), (2) that tab is navigated to
# the extensions URL (Ctrl+L -> paste -> Enter). The URL is pasted, not typed,
# so the whole thing lands in under a second — no visible "ghost typing".
#
# Why keystrokes and not a command line? `start chrome --new-tab
# chrome://extensions` looks right but fails when Chrome is already running:
# chrome:// URLs are dropped by the process-singleton handoff, leaving a stray
# blank-tab window (http(s) URLs forward fine, chrome:// die). Typing the URL
# into a freshly created tab is the only reliable route, and it cannot touch
# whatever tab the user is on.
#
# The window is brought to the foreground with the thread-input-attach recipe
# (works even when Windows foreground-lock would deny SetForegroundWindow),
# and we VERIFY the target is actually foreground before sending anything —
# keystrokes are never fired blind at a random app.
#
# Exit codes (stdout carries the browser name: chrome|msedge|brave|none):
#   0  new tab opened and navigated
#   1  no Chromium browser process running (caller may spawn a fresh instance)
#   2  browser running but its window could not be driven (caller reports blocked)

param([string]$Url = $null)

$ErrorActionPreference = 'Stop'

Add-Type @"
using System;
using System.Runtime.InteropServices;
public static class ExtTabNative {
    [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern bool BringWindowToTop(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint pid);
    [DllImport("kernel32.dll")] public static extern uint GetCurrentThreadId();
    [DllImport("user32.dll")] public static extern bool AttachThreadInput(uint a, uint b, bool attach);
    [DllImport("user32.dll")] public static extern void keybd_event(byte vk, byte scan, uint flags, UIntPtr extra);
    public const int VK_MENU = 0x12;
    public const int KEYEVENTF_KEYUP = 0x0002;
}
"@

$browserOrder = @('chrome', 'msedge', 'brave')
$urlMap = @{
    chrome = 'chrome://extensions/'
    msedge = 'edge://extensions/'
    brave  = 'chrome://extensions/'
}

function Get-BrowserWindow {
    foreach ($name in $browserOrder) {
        $proc = Get-Process $name -ErrorAction SilentlyContinue |
            Where-Object { $_.MainWindowHandle -ne 0 } |
            Select-Object -First 1
        if ($proc) { return $proc }
    }
    return $null
}

# Bring hWnd to the foreground even under Windows foreground-lock:
# attach our input queue to the target's thread, then restore/raise.
function Bring-ToForeground([IntPtr]$hWnd) {
    $targetTid = [ExtTabNative]::GetWindowThreadProcessId($hWnd, [ref]([uint32]0))
    $myTid = [ExtTabNative]::GetCurrentThreadId()
    try {
        [ExtTabNative]::AttachThreadInput($myTid, $targetTid, $true) | Out-Null
        [ExtTabNative]::ShowWindow($hWnd, 9) | Out-Null               # SW_RESTORE
        [ExtTabNative]::SetForegroundWindow($hWnd) | Out-Null
        [ExtTabNative]::BringWindowToTop($hWnd) | Out-Null
        Start-Sleep -Milliseconds 100
        # classic ALT-tap unlocks foreground if the lock still holds
        [ExtTabNative]::keybd_event([ExtTabNative]::VK_MENU, 0, 0, [UIntPtr]::Zero)
        [ExtTabNative]::keybd_event([ExtTabNative]::VK_MENU, 0, [ExtTabNative]::KEYEVENTF_KEYUP, [UIntPtr]::Zero)
        [ExtTabNative]::SetForegroundWindow($hWnd) | Out-Null
    } finally {
        [ExtTabNative]::AttachThreadInput($myTid, $targetTid, $false) | Out-Null
    }
    Start-Sleep -Milliseconds 250
    return [ExtTabNative]::GetForegroundWindow() -eq $hWnd
}

$ws = New-Object -ComObject WScript.Shell

# two attempts: the first drive can lose a focus race
for ($attempt = 0; $attempt -lt 2; $attempt++) {
    $proc = Get-BrowserWindow
    if (-not $proc) {
        Write-Output 'none'
        exit 1
    }
    $url = if ($Url) { $Url } else { $urlMap[$proc.ProcessName] }
    if (-not $url) {
        Write-Output 'none'
        exit 2
    }
    if (-not (Bring-ToForeground $proc.MainWindowHandle)) {
        Start-Sleep -Milliseconds 300
        continue
    }
    try {
        $ws.SendKeys('^t')          # step 1: NEW tab opens visibly — the active
        Start-Sleep -Milliseconds 350  # tab is never touched (also releases focus
        $ws.SendKeys('^l')          # step 2: navigate it — omnibox, paste, Enter
        Start-Sleep -Milliseconds 150
        Set-Clipboard -Value $url
        $ws.SendKeys('^v')
        Start-Sleep -Milliseconds 100
        $ws.SendKeys('{ENTER}')
        Write-Output $proc.ProcessName
        exit 0
    } catch {
        # fall through to the retry, then blocked
    }
    Start-Sleep -Milliseconds 300
}

Write-Output 'none'
exit 2
