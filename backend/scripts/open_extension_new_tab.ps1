# Opens the browser's extensions manager in a NEW tab of a RUNNING Chromium
# browser — never reuses or navigates the active tab, never opens a stray
# window, never changes a visible window's size or fullscreen state.
#
# Flow, exactly what the user would do by hand: (1) the browser window comes
# to the foreground and a NEW tab opens (Ctrl+T), (2) that tab is navigated to
# the extensions URL (Ctrl+L -> type URL -> Enter). The URL is typed straight
# into the omnibox via SendKeys — no clipboard round-trip (Set-Clipboard can
# block for seconds while this app's WebView2 holds the clipboard).
#
# Why keystrokes and not a command line? `start chrome --new-tab
# chrome://extensions` looks right but fails when Chrome is already running:
# chrome:// URLs are dropped by the process-singleton handoff, leaving a stray
# blank-tab window (http(s) URLs forward fine, chrome:// die). Typing the URL
# into a freshly created tab is the only reliable route, and it cannot touch
# whatever tab the user is on.
#
# Window state is NEVER changed for a visible window: no ShowWindow restore
# (an unconditional SW_RESTORE flips resolution/fullscreen), no ALT-tap. We
# only foreground with the thread-input-attach recipe (AttachThreadInput +
# SetForegroundWindow), skip entirely when the target is already foreground,
# and restore from minimized (IsIconic) only because keystrokes need a
# visible window — restoring then returns it to its previous state, which is
# correct.
#
# The window is chosen by command line, not by "first process with a window":
# default-profile processes (no --user-data-dir) are preferred, so the drive
# never lands in another profile or an incognito session. If no window is
# visible we poll briefly (a background-mode browser can raise one) before
# giving up — and we NEVER report "no browser" while a process exists.
#
# Exit codes (stdout carries the browser name: chrome|msedge|brave|none):
#   0  new tab opened and navigated
#   1  NO Chromium browser process is running — and only then may the caller
#      spawn a fresh instance (no singleton exists to drop the URL)
#   2  a browser process is running but its window could not be driven —
#      the caller must NOT bare-spawn: the singleton would drop the
#      chrome:// URL into a stray blank window

param([string]$Url = $null)

$ErrorActionPreference = 'Stop'

# One small P/Invoke type for the window-state/foreground calls that have no
# BCL equivalent. Compiled ONCE at the top; the SendKeys grammar comes from
# System.Windows.Forms (an assembly LOAD, not a compile).
Add-Type -AssemblyName System.Windows.Forms
Add-Type @"
using System;
using System.Runtime.InteropServices;
public static class ExtTabNative {
    [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint pid);
    [DllImport("kernel32.dll")] public static extern uint GetCurrentThreadId();
    [DllImport("user32.dll")] public static extern bool AttachThreadInput(uint a, uint b, bool attach);
    [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr hWnd);
    public const int SW_RESTORE = 9;
}
"@

$browserOrder = @('chrome', 'msedge', 'brave')
$urlMap = @{
    chrome = 'chrome://extensions/'
    msedge = 'edge://extensions/'
    brave  = 'chrome://extensions/'
}

# Every Chromium-family process for one browser, joined with its window state.
# The command line decides the profile: default (no --user-data-dir) vs custom.
function Get-BrowserProcesses([string]$name) {
    $handles = @{}
    Get-Process $name -ErrorAction SilentlyContinue | ForEach-Object {
        $handles[[string]$_.Id] = $_
    }
    foreach ($cim in @(Get-CimInstance Win32_Process -Filter "Name='$name.exe'" -ErrorAction SilentlyContinue)) {
        $p = $handles[[string]$cim.ProcessId]
        if (-not $p) { continue }
        $cmd = [string]$cim.CommandLine
        [pscustomobject]@{
            Id             = $p.Id
            Handle         = $p.MainWindowHandle
            Title          = $p.MainWindowTitle
            ProcessName    = $p.ProcessName
            # unreadable command line is treated as a custom profile, so a
            # wrong-profile window is only ever the LAST resort, never the pick
            DefaultProfile = ($cmd -ne '' -and $cmd -notmatch '--user-data-dir')
        }
    }
}

# Prefer a visible default-profile window; fall back to any visible window.
function Get-BrowserWindow {
    foreach ($name in $browserOrder) {
        $procs = @(Get-BrowserProcesses $name)
        $win = $procs | Where-Object { $_.DefaultProfile -and $_.Handle -ne 0 } | Select-Object -First 1
        if (-not $win) { $win = $procs | Where-Object { $_.Handle -ne 0 } | Select-Object -First 1 }
        if ($win) { return $win }
    }
    return $null
}

# True only when NO Chromium-family process exists at all.
function Test-AnyBrowserRunning {
    foreach ($name in $browserOrder) {
        if (Get-Process $name -ErrorAction SilentlyContinue) { return $true }
    }
    return $false
}

# Bring hWnd to the foreground even under Windows foreground-lock. NEVER
# changes a visible window's state: no ShowWindow, no ALT-tap. Restores only
# when the window is actually minimized (keystrokes need a visible window) —
# restoring then returns it to its previous state, which is correct. Skips
# entirely when the target is already foreground.
function Bring-ToForeground([IntPtr]$hWnd) {
    if ([ExtTabNative]::GetForegroundWindow() -eq $hWnd) { return $true }
    if (-not [ExtTabNative]::IsWindowVisible($hWnd)) { return $false }
    if ([ExtTabNative]::IsIconic($hWnd)) {
        [ExtTabNative]::ShowWindow($hWnd, [ExtTabNative]::SW_RESTORE) | Out-Null
    }
    $targetTid = [ExtTabNative]::GetWindowThreadProcessId($hWnd, [ref]([uint32]0))
    $myTid = [ExtTabNative]::GetCurrentThreadId()
    try {
        [ExtTabNative]::AttachThreadInput($myTid, $targetTid, $true) | Out-Null
        [ExtTabNative]::SetForegroundWindow($hWnd) | Out-Null
    } finally {
        [ExtTabNative]::AttachThreadInput($myTid, $targetTid, $false) | Out-Null
    }
    return [ExtTabNative]::GetForegroundWindow() -eq $hWnd
}

# SendKeys grammar reserves + ^ % ~ ( ) [ ] { } — wrap any literal occurrence.
function ConvertTo-SendKeys([string]$text) {
    $sb = [System.Text.StringBuilder]::new()
    foreach ($ch in $text.ToCharArray()) {
        switch ([string]$ch) {
            '{' { [void]$sb.Append('{{}') }
            '}' { [void]$sb.Append('{}}') }
            '+' { [void]$sb.Append('{+}') }
            '^' { [void]$sb.Append('{^}') }
            '%' { [void]$sb.Append('{%}') }
            '~' { [void]$sb.Append('{~}') }
            '(' { [void]$sb.Append('{(}') }
            ')' { [void]$sb.Append('{)}') }
            '[' { [void]$sb.Append('{[}') }
            ']' { [void]$sb.Append('{]}') }
            default { [void]$sb.Append($ch) }
        }
    }
    return $sb.ToString()
}

# Poll a predicate with short sleeps instead of a fixed delay.
function Wait-Until([scriptblock]$predicate, [int]$timeoutMs = 500) {
    $deadline = [Environment]::TickCount + $timeoutMs
    while ([Environment]::TickCount -lt $deadline) {
        if (& $predicate) { return $true }
        Start-Sleep -Milliseconds 50
    }
    return (& $predicate)
}

# Send one keystroke sequence and make sure the browser window is still the
# foreground owner afterwards — keystrokes are never fired blind at a random
# app. No fixed sleeps: SendWait is synchronous (returns only after the
# browser processed the keys), and the post-check polls instead of sleeping.
function Send-Keys([string]$keys, [IntPtr]$hWnd) {
    [System.Windows.Forms.SendKeys]::SendWait($keys)
    if (-not (Wait-Until { [ExtTabNative]::GetForegroundWindow() -eq $hWnd } 400)) {
        throw 'browser lost foreground'
    }
}

# --- find a driveable window (poll briefly: a background-mode browser can
# --- materialize a window a moment after it starts) -------------------------
$win = $null
for ($i = 0; $i -lt 5 -and -not $win; $i++) {
    $win = Get-BrowserWindow
    if (-not $win) { Start-Sleep -Milliseconds 200 }
}

if (-not $win) {
    Write-Output 'none'
    if (Test-AnyBrowserRunning) { exit 2 } else { exit 1 }
}

$url = if ($Url) { $Url } else { $urlMap[$win.ProcessName] }
if (-not $url) {
    Write-Output 'none'
    exit 2
}

# --- foreground with retry (foreground-lock can need a couple of tries) -----
$foreground = $false
for ($i = 0; $i -lt 5 -and -not $foreground; $i++) {
    $foreground = Bring-ToForeground $win.Handle
    if (-not $foreground) { Start-Sleep -Milliseconds 200 }
}
if (-not $foreground) {
    Write-Output 'none'
    exit 2
}

# --- drive: NEW tab, omnibox, type the URL, Enter ----------------------------
try {
    Send-Keys '^t' $win.Handle                           # NEW tab — active tab untouched
    Send-Keys '^l' $win.Handle                           # omnibox in the new tab
    Send-Keys (ConvertTo-SendKeys $url) $win.Handle      # type URL — no clipboard
    Send-Keys '{ENTER}' $win.Handle
    Write-Output $win.ProcessName
    exit 0
} catch {
    Write-Output 'none'
    exit 2
}
