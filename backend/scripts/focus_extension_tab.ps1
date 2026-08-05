<#
focus_extension_tab.ps1 — put the extensions manager in front of the user's
browser. Used by the Cookie Bridge "Open extensions" button.

Two modes, in order:
  1. REUSE — an extensions tab is already open somewhere: select that tab
     (UI Automation TabItem + SelectionItemPattern, no CDP) and raise the
     window, so a second click never spawns a duplicate.
  2. DRIVE — no extensions tab open, but a real browser window exists:
     focus the topmost one (Win32 z-order over the browser processes'
     MainWindowHandle windows — Chrome's background-tab content windows are
     deliberately ignored) and open the URL by keystroke (Ctrl+L -> paste ->
     Enter). Command-line URL forwarding is NOT used: a running Chrome
     silently drops chrome:// URLs handed off through its process singleton
     (http(s) forward fine, chrome:// die), so we drive the omnibox the way
     a human would. Headless/automation Chrome instances (--headless,
     --remote-debugging*, custom --user-data-dir) are excluded — they are
     tooling, never the user's browser.

Exit codes:
  0 — an Extensions tab was found, selected, and its window raised
  2 — no Extensions tab; a new tab was driven in the topmost browser window
  1 — no browser running at all (caller should spawn a fresh browser)
  3 — a browser is running but no usable window / focus could not be won
      (caller must NOT spawn chrome.exe with the URL — a running instance
      drops the URL and leaves a stray new-tab page instead)

On exit 2 the script prints the driven browser's process name (chrome,
msedge, brave) so the caller can report the right extension-manager URL.
#>
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
Add-Type -AssemblyName System.Windows.Forms
Add-Type @"
using System;
using System.Runtime.InteropServices;
public class FocusExtNative {
  [DllImport("user32.dll")] public static extern IntPtr GetTopWindow(IntPtr hWnd);
  [DllImport("user32.dll")] public static extern IntPtr GetWindow(IntPtr hWnd, uint uCmd);
  [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint pid);
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
  [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
  [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
  [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr hWnd);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr hWnd);
  [DllImport("user32.dll")] public static extern bool AttachThreadInput(uint idAttach, uint idAttachTo, bool fAttach);
  [DllImport("kernel32.dll")] public static extern uint GetCurrentThreadId();
}
"@

# chrome://extensions page title per UI locale (top Chrome locales). If the
# user's locale is missing here we fall back to opening a new tab — harmless.
$names = @(
  'Extensions', 'Extensões', 'Extensiones', 'Erweiterungen', 'Estensioni',
  'Extensies', 'Rozszerzenia', 'Расширения', '拡張機能', '확장 프로그램',
  '扩展程序', '擴充功能', 'Uzantılar', 'Розширення', 'Rozšíření',
  'Tillägg', 'Udvidelser', 'Laajennukset', 'Utvidelser', 'Extensii',
  'Bővítmények', 'Επεκτάσεις', 'ส่วนขยาย', 'Tiện ích', 'Ekstensi',
  'الإضافات', 'הרחבות', 'एक्सटेंशन'
)

# Scope to Chrome-family browsers only — other apps (Explorer, terminals…)
# also expose TabItem elements.
$browsers = @('chrome', 'msedge', 'brave')

# --- 1) reuse: select the already-open extensions tab -----------------------
$tabCond = New-Object System.Windows.Automation.PropertyCondition(
  [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
  [System.Windows.Automation.ControlType]::TabItem)
$root = [System.Windows.Automation.AutomationElement]::RootElement
$match = $null
try {
  $tabs = $root.FindAll([System.Windows.Automation.TreeScope]::Descendants, $tabCond)
  if ($tabs) {
    foreach ($t in $tabs) {
      $pidOf = $t.Current.ProcessId
      $procName = (Get-Process -Id $pidOf -ErrorAction SilentlyContinue).ProcessName
      if (-not $procName) { continue }
      $procName = $procName.ToLower()
      if (-not ($browsers -contains $procName)) { continue }
      $name = ($t.Current.Name -as [string]).Trim()
      if ($names -contains $name) { $match = $t; break }
    }
  }
} catch {
  exit 3
}

if ($match) {
  try {
    $sel = $match.GetCurrentPattern([System.Windows.Automation.SelectionItemPattern]::Pattern)
    $sel.Select()
  } catch {
    # Some Chromium builds expose tabs without a working selection pattern —
    # fall through to the drive path.
    $match = $null
  }
  if ($match) {
    # Raise the owning window (works even when minimized).
    try {
      $walker = [System.Windows.Automation.TreeWalker]::ControlViewWalker
      $node = $walker.GetParent($match)
      while ($node -and $node.Current.ControlType -ne [System.Windows.Automation.ControlType]::Window) {
        $node = $walker.GetParent($node)
      }
      if ($node) {
        try {
          $wp = $node.GetCurrentPattern([System.Windows.Automation.WindowPattern]::Pattern)
          $wp.SetWindowVisualState([System.Windows.Automation.WindowVisualState]::Normal)
        } catch { }
        $node.SetFocus()
      }
    } catch { }
    exit 0
  }
}

# --- 2) drive: open the URL by keystroke in the topmost REAL browser window --
# Collect the browser processes' top-level windows first. Chrome also exposes
# each tab's content as a top-level window (invisible for background tabs),
# and automation tooling spawns headless instances with their own profiles —
# both must never receive the keystrokes.
$browserProcs = Get-Process -ErrorAction SilentlyContinue |
  Where-Object { $browsers -contains $_.ProcessName.ToLower() }
if ($browserProcs.Count -eq 0) { exit 1 }   # nothing running — fresh spawn is safe

$realWindows = @{}   # hwnd -> process name
foreach ($p in $browserProcs) {
  $hw = $p.MainWindowHandle
  if ($hw -eq 0) { continue }
  $cl = (Get-CimInstance Win32_Process -Filter "ProcessId=$($p.Id)" -ErrorAction SilentlyContinue).CommandLine
  if ($cl -and ($cl -match '--headless' -or $cl -match '--remote-debugging' -or $cl -match '--user-data-dir')) {
    continue   # tooling browser, not the user's
  }
  $realWindows[$hw] = $p.ProcessName.ToLower()
}
if ($realWindows.Count -eq 0) { exit 3 }   # running, but only tooling/windowless instances

$GW_HWNDNEXT = 2
$target = [IntPtr]::Zero
$targetProc = ''
$fallback = [IntPtr]::Zero
$fallbackProc = ''
$h = [FocusExtNative]::GetTopWindow([IntPtr]::Zero)
while ($h -ne [IntPtr]::Zero) {
  if ($realWindows.ContainsKey($h)) {
    if ([FocusExtNative]::IsWindowVisible($h) -and $target -eq [IntPtr]::Zero) {
      $target = $h
      $targetProc = $realWindows[$h]
    } elseif (-not [FocusExtNative]::IsWindowVisible($h) -and $fallback -eq [IntPtr]::Zero) {
      $fallback = $h
      $fallbackProc = $realWindows[$h]
    }
  }
  $h = [FocusExtNative]::GetWindow($h, $GW_HWNDNEXT)
}
if ($target -eq [IntPtr]::Zero) { $target = $fallback; $targetProc = $fallbackProc }
if ($target -eq [IntPtr]::Zero) { exit 3 }

$url = if ($targetProc -eq 'msedge') { 'edge://extensions' } else { 'chrome://extensions' }

if ([FocusExtNative]::IsIconic($target)) {
  [FocusExtNative]::ShowWindow($target, 9) | Out-Null   # SW_RESTORE
  Start-Sleep -Milliseconds 400
}
# AttachThreadInput defeats Windows' foreground lock (a helper process may
# not be allowed to steal focus from the app the user is clicking in).
$tpid = 0
[FocusExtNative]::GetWindowThreadProcessId($target, [ref]$tpid) | Out-Null
$curTid = [FocusExtNative]::GetCurrentThreadId()
[FocusExtNative]::AttachThreadInput($curTid, $tpid, $true) | Out-Null
try {
  # Win focus is contested by the app the user is clicking in — retry a few
  # times, and only then accept any other real window of the same browser
  # family as the recipient. Bailing out here would send the caller down the
  # spawn path, which turns into a stray new-tab page in a running browser.
  $focused = $false
  for ($i = 0; $i -lt 3 -and -not $focused; $i++) {
    [FocusExtNative]::SetForegroundWindow($target) | Out-Null
    Start-Sleep -Milliseconds 400
    if ([FocusExtNative]::GetForegroundWindow() -eq $target) { $focused = $true }
  }
  if (-not $focused) {
    $fg = [FocusExtNative]::GetForegroundWindow()
    if (-not $realWindows.ContainsKey($fg)) { exit 3 }
    $target = $fg   # keystrokes land in another window of the same browser — fine
  }

  # Preserve whatever the user had on the clipboard — the paste clobbers it.
  $clipSaved = $null
  $clipHadText = $false
  try {
    if ([System.Windows.Forms.Clipboard]::ContainsText()) {
      $clipSaved = [System.Windows.Forms.Clipboard]::GetText()
      $clipHadText = $true
    }
  } catch { }

  try {
    [System.Windows.Forms.SendKeys]::SendWait("^l")          # focus omnibox
    Start-Sleep -Milliseconds 300
    [System.Windows.Forms.Clipboard]::SetText($url)
    Start-Sleep -Milliseconds 150
    [System.Windows.Forms.SendKeys]::SendWait("^v")          # paste URL
    Start-Sleep -Milliseconds 300
    [System.Windows.Forms.SendKeys]::SendWait("{ENTER}")
  } finally {
    if ($clipHadText) {
      try { [System.Windows.Forms.Clipboard]::SetText($clipSaved) } catch { }
    }
  }
} finally {
  [FocusExtNative]::AttachThreadInput($curTid, $tpid, $false) | Out-Null
}

Write-Output $targetProc
exit 2
