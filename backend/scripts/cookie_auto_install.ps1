# One-click cookie-extension auto-install (productized CookieInstallWorker flow).
#
# Mechanism (proven on Chrome 151, this machine): drive the user's REAL
# profile entirely by UIA on a hidden window — spawn chrome --new-window
# chrome://extensions (reuses the RUNNING instance, never kills anything),
# alpha-0 the window (renderer stays live so UIA sees the page), click
# "Load unpacked" by automation id, drive the folder dialog by keyboard
# (Ctrl+L, type the path, Enter, Enter — Chrome 151's picker on Win11 24H2+
# exposes "Pasta:"/"Selecionar pasta" as pattern-less Panes, so UIA
# SetValue/Invoke are no-ops; keyboard falls back to the classic UIA
# SetValue+click for older layouts), verify the extension card, capture
# its ID, close the hidden window.
#
# Why not CDP: Chrome 151 refuses --remote-debugging-port on the real
# profile (the account gets revoked); the old debug-instance mechanism
# failed with "browser did not expose the debug port". The UIA path needs
# no debug port and never restarts the browser, so the user's session
# survives untouched.
#
# stdlib/BCL only (Add-Type P/Invoke + UIAutomation). No npm/PyPI deps.
#
# Usage:
#   powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File `
#     cookie_auto_install.ps1 -ExtensionDir "C:\...\VOD.RIP-cookies" `
#     [-Browser chrome|msedge|brave] [-DebugPort 9222] [-DryRun]
#   cookie_auto_install.ps1 -ExtensionDir "C:\...\VOD.RIP-cookies" -ReloadOnly `
#     [-ExpectedVersion 0.8.3] [-Browser chrome|msedge|brave] [-DryRun]
#
# -ReloadOnly: the extension is already installed UNPACKED but may be running
# stale code (Chrome does NOT hot-reload a loaded folder on file change, and
# the plain auto-install short-circuits with "already present, nothing to
# do"). This mode clicks the card's Reload button on the hidden
# chrome://extensions window instead, so the service worker re-registers
# from the folder on disk in place — the browser keeps its extension id,
# permissions and every user tab. -ExpectedVersion, when given, reports
# whether the card text contains that version string after the reload.
#
# stdout: exactly ONE JSON line (the result), human progress goes to stderr:
#   {"ok":true,"installed":true,"extension_id":"...","error":null}
#   {"ok":true,"reloaded":true,"extension_id":"...","version_found":true,"error":null}
# DryRun prints the resolved plan without touching the browser.

param(
    [string]$ExtensionDir,
    [string]$Browser = 'chrome',
    [int]$DebugPort = 9222,  # kept for CLI compat; the UIA path needs no CDP
    [switch]$DryRun,
    [switch]$ReloadOnly,
    [string]$ExpectedVersion = ''
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

$manifest = Join-Path $ExtensionDir 'manifest.json'
if (-not (Test-Path -LiteralPath $manifest)) {
    Write-Output (@{ ok = $false; installed = $false; extension_id = ''; error = "extension folder missing manifest.json: $ExtensionDir" } | ConvertTo-Json -Compress)
    exit 1
}

if ($DryRun) {
    Write-Output (@{
        ok = $true; dryRun = $true; browser = $Browser; browser_exe = $exe
        mechanism = if ($ReloadOnly) { 'uia-hidden-window-reload' } else { 'uia-hidden-window' }
        reloadOnly = [bool]$ReloadOnly
        extension_dir = $ExtensionDir
        expected_version = $ExpectedVersion
    } | ConvertTo-Json -Compress)
    exit 0
}

function Write-ProgressLog([string]$msg) { [Console]::Error.WriteLine($msg) }

# --- Win32 + UIA plumbing (compiled once) --------------------------------------
Add-Type -AssemblyName System.Windows.Forms
Add-Type -TypeDefinition @"
using System;
using System.Text;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using System.Threading;
using System.Windows.Automation;
public static class ExtWin {
  public delegate bool EnumProc(IntPtr hWnd, IntPtr lParam);
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc cb, IntPtr lParam);
  [DllImport("user32.dll", CharSet = CharSet.Unicode)] public static extern int GetWindowText(IntPtr h, StringBuilder sb, int max);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
  [DllImport("user32.dll")] public static extern bool IsWindow(IntPtr h);
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
  [DllImport("user32.dll")] public static extern bool SetWindowPos(IntPtr h, IntPtr after, int x, int y, int cx, int cy, uint flags);
  [DllImport("user32.dll")] public static extern int GetWindowLong(IntPtr h, int idx);
  [DllImport("user32.dll")] public static extern int SetWindowLong(IntPtr h, int idx, int val);
  [DllImport("user32.dll")] public static extern bool SetLayeredWindowAttributes(IntPtr h, uint crKey, byte alpha, uint flags);
  [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint pid);
  [DllImport("user32.dll")] public static extern bool PostMessage(IntPtr h, uint msg, IntPtr w, IntPtr l);
  public const int GWL_EXSTYLE = -20;
  public const int WS_EX_LAYERED = 0x00080000;
  public const uint LWA_ALPHA = 0x00000002;
  public const uint SWP_NOZORDER = 0x0004;
  public const uint SWP_NOACTIVATE = 0x0010;
  public const uint WM_KEYDOWN = 0x0100;
  public const uint WM_KEYUP = 0x0101;
  public const uint WM_CHAR = 0x0102;
  public const uint VK_RETURN = 0x0D;
  public const uint WM_CLOSE = 0x0010;
  public struct RECT { public int Left, Top, Right, Bottom; }
  public static void PostEnter(IntPtr h) {
    PostMessage(h, WM_KEYDOWN, (IntPtr)VK_RETURN, IntPtr.Zero);
    PostMessage(h, WM_KEYUP, (IntPtr)VK_RETURN, IntPtr.Zero);
  }
  public static void Close(IntPtr h) { PostMessage(h, WM_CLOSE, IntPtr.Zero, IntPtr.Zero); }
  public static void Invisible(IntPtr h) {
    int ex = GetWindowLong(h, GWL_EXSTYLE);
    SetWindowLong(h, GWL_EXSTYLE, ex | WS_EX_LAYERED);
    SetLayeredWindowAttributes(h, 0, 0, LWA_ALPHA);
  }
  public static void Move(IntPtr h, int x, int y) { RECT r; GetWindowRect(h, out r); SetWindowPos(h, IntPtr.Zero, x, y, r.Right - r.Left, r.Bottom - r.Top, SWP_NOZORDER | SWP_NOACTIVATE); }
  public static string Title(IntPtr h) { var t = new StringBuilder(256); GetWindowText(h, t, 256); return t.ToString(); }
  public static uint Pid(IntPtr h) { uint p; GetWindowThreadProcessId(h, out p); return p; }
  public static List<IntPtr> AllWindows() {
    var res = new List<IntPtr>();
    EnumWindows((h, l) => { res.Add(h); return true; }, IntPtr.Zero);
    return res;
  }
  public static string Navigate(IntPtr hwnd, string url) {
    var win = AutomationElement.FromHandle(hwnd);
    var cond = new PropertyCondition(AutomationElement.ControlTypeProperty, ControlType.Edit);
    var sw = System.Diagnostics.Stopwatch.StartNew();
    while (sw.ElapsedMilliseconds < 20000) {
      try {
        var edits = win.FindAll(TreeScope.Descendants, cond);
        foreach (AutomationElement e in edits) {
          var name = e.Current.Name ?? "";
          if (name.IndexOf("Endere", StringComparison.OrdinalIgnoreCase) >= 0 ||
              name.IndexOf("Address", StringComparison.OrdinalIgnoreCase) >= 0) {
            var vp = e.GetCurrentPattern(ValuePattern.Pattern) as ValuePattern;
            if (vp != null) { vp.SetValue(url); return "SET"; }
            return "NO_VALUE";
          }
        }
      } catch { }
      Thread.Sleep(300);
    }
    return "NO_BAR";
  }
  public static string ClickButton(IntPtr hwnd, string namePart, string autoId, int timeoutMs, bool toggleFallback) {
    var win = AutomationElement.FromHandle(hwnd);
    var sw = System.Diagnostics.Stopwatch.StartNew();
    while (sw.ElapsedMilliseconds < timeoutMs) {
      try {
        // Match by automation id FIRST: the browser toolbar's page-reload
        // button is ALSO named "Recarregar"/"Reload" (view_1003) and comes
        // before the extension card's dev-reload-button in the a11y tree —
        // a name-first match reloaded the extensions PAGE instead of the
        // extension (silent no-op, proven live 2026-08-10: version never
        // changed after "reload"). Ids are unique; names are not.
        if (autoId.Length > 0) {
          var byId = win.FindAll(TreeScope.Descendants,
            new PropertyCondition(AutomationElement.ControlTypeProperty, ControlType.Button));
          foreach (AutomationElement b in byId) {
            if ((b.Current.AutomationId ?? "") == autoId) {
              var inv = b.GetCurrentPattern(InvokePattern.Pattern) as InvokePattern;
              if (inv != null) { inv.Invoke(); return "CLICKED:" + (b.Current.Name ?? ""); }
            }
          }
        }
        var btns = win.FindAll(TreeScope.Descendants,
          new PropertyCondition(AutomationElement.ControlTypeProperty, ControlType.Button));
        foreach (AutomationElement b in btns) {
          var nm = b.Current.Name ?? "";
          if (namePart.Length > 0 && nm.IndexOf(namePart, StringComparison.OrdinalIgnoreCase) >= 0) {
            var inv = b.GetCurrentPattern(InvokePattern.Pattern) as InvokePattern;
            if (inv != null) { inv.Invoke(); return "CLICKED:" + nm; }
          }
        }
        if (toggleFallback) {
          foreach (AutomationElement b in btns) {
            var nm = b.Current.Name ?? "";
            if (nm.IndexOf(namePart, StringComparison.OrdinalIgnoreCase) >= 0) {
              var tg = b.GetCurrentPattern(TogglePattern.Pattern) as TogglePattern;
              if (tg != null) { tg.Toggle(); return "TOGGLED:" + nm; }
            }
          }
        }
      } catch { }
      Thread.Sleep(300);
    }
    return "NOT_FOUND";
  }
  public static string DumpText(IntPtr hwnd, int max = 4000) {
    try {
      var win = AutomationElement.FromHandle(hwnd);
      var all = win.FindAll(TreeScope.Descendants, Condition.TrueCondition);
      var sb = new System.Text.StringBuilder();
      int n = 0;
      foreach (AutomationElement e in all) {
        if (n++ >= 120) break;
        var nm = e.Current.Name ?? "";
        if (nm.Trim().Length == 0) continue;
        sb.Append(' ').Append(nm);
        if (sb.Length > max) break;
      }
      return sb.ToString();
    } catch (Exception ex) { return "ERR: " + ex.Message; }
  }
  public static string DrivePicker(IntPtr hwnd, string path, int timeoutMs) {
    var sw = System.Diagnostics.Stopwatch.StartNew();
    // PRIMARY: keyboard. Chrome 151's folder picker on Win11 24H2+ exposes the
    // "Pasta:" field and "Selecionar pasta" as dead Panes (no Value/Invoke
    // patterns), so UIA SetValue is a no-op. Ctrl+L -> type path -> Enter
    // (navigate) -> Enter (confirm) works on both the new and classic layouts.
    try {
      PostMessage(hwnd, WM_KEYDOWN, (IntPtr)0x11, IntPtr.Zero); // Ctrl
      PostMessage(hwnd, WM_KEYDOWN, (IntPtr)0x4C, IntPtr.Zero); // L
      PostMessage(hwnd, WM_KEYUP, (IntPtr)0x4C, IntPtr.Zero);
      PostMessage(hwnd, WM_KEYUP, (IntPtr)0x11, IntPtr.Zero);
      Thread.Sleep(500);
      foreach (char c in path) PostMessage(hwnd, WM_CHAR, (IntPtr)c, IntPtr.Zero);
      Thread.Sleep(300);
      PostEnter(hwnd); // navigate the folder view to `path`
      Thread.Sleep(1500);
      PostEnter(hwnd); // confirm "Selecionar pasta"
      var ksw = System.Diagnostics.Stopwatch.StartNew();
      while (ksw.ElapsedMilliseconds < 15000) {
        if (!IsStillThere(hwnd)) return "KBD_OK";
        Thread.Sleep(300);
      }
      // dialog still open: keyboard failed (unfocused/blocked), fall through
      // to the legacy UIA drive below.
    } catch { }
    sw.Restart();

    var win = AutomationElement.FromHandle(hwnd);
    string lastEdit = "(none)";
    while (sw.ElapsedMilliseconds < timeoutMs) {
      try {
        if (sw.ElapsedMilliseconds > 500 && !IsStillThere(hwnd)) return "DIALOG_GONE";
        var edits = win.FindAll(TreeScope.Descendants,
          new PropertyCondition(AutomationElement.ControlTypeProperty, ControlType.Edit));
        AutomationElement target = null;
        foreach (AutomationElement e in edits) {
          var id = e.Current.AutomationId ?? "";
          var nm = e.Current.Name ?? "";
          lastEdit = id + "/" + nm;
          if (id == "1148") { target = e; break; }
        }
        if (target == null) {
          foreach (AutomationElement e in edits) {
            var nm = e.Current.Name ?? "";
            if (nm.IndexOf("ome do arquivo", StringComparison.OrdinalIgnoreCase) >= 0 ||
                nm.IndexOf("File name", StringComparison.OrdinalIgnoreCase) >= 0 ||
                nm.IndexOf("asta", StringComparison.OrdinalIgnoreCase) >= 0 ||
                nm.IndexOf("Folder", StringComparison.OrdinalIgnoreCase) >= 0) {
              target = e; break;
            }
          }
        }
        if (target != null) {
          var vp = target.GetCurrentPattern(ValuePattern.Pattern) as ValuePattern;
          if (vp != null) {
            try { target.SetFocus(); } catch { }
            vp.SetValue(path);
            Thread.Sleep(400);
            PostEnter(hwnd);
            Thread.Sleep(1500);
            if (!IsStillThere(hwnd)) return "SET+ENTER_OK";
            var btns = win.FindAll(TreeScope.Descendants,
              new PropertyCondition(AutomationElement.ControlTypeProperty, ControlType.Button));
            foreach (AutomationElement b in btns) {
              var nm = b.Current.Name ?? "";
              if (nm.IndexOf("Selecionar Pasta", StringComparison.OrdinalIgnoreCase) >= 0 ||
                  nm.IndexOf("Select Folder", StringComparison.OrdinalIgnoreCase) >= 0 ||
                  nm.IndexOf("Abrir", StringComparison.OrdinalIgnoreCase) >= 0 ||
                  nm.IndexOf("Open", StringComparison.OrdinalIgnoreCase) >= 0) {
                var inv = b.GetCurrentPattern(InvokePattern.Pattern) as InvokePattern;
                if (inv != null) { inv.Invoke(); return "CLICKED:" + nm; }
              }
            }
            return "PATH_SET_NO_BUTTON";
          }
        }
      } catch { }
      Thread.Sleep(300);
    }
    return "TIMEOUT lastEdit=" + lastEdit;
  }
  private static bool IsStillThere(IntPtr hwnd) {
    try { return AutomationElement.FromHandle(hwnd).Current.ControlType != null; }
    catch { return false; }
  }
}
"@ -ReferencedAssemblies UIAutomationClient,UIAutomationTypes

$result = @{ ok = $false; installed = $false; extension_id = ''; error = $null }
$newWin = [IntPtr]::Zero

try {
    Write-ProgressLog "auto-install: spawning a hidden $Browser window at $($binfo.url)"
    $before = [ExtWin]::AllWindows()
    $p = Start-Process -FilePath $exe -ArgumentList '--new-window', $binfo.url -PassThru
    Start-Sleep -Seconds 3

    function Test-IsTarget($h) {
        $proc = Get-Process -Id ([ExtWin]::Pid($h)) -ErrorAction SilentlyContinue
        return $proc -and $proc.ProcessName -eq $Browser
    }
    for ($i = 0; $i -lt 30; $i++) {
        $now = [ExtWin]::AllWindows()
        foreach ($h in $now) {
            if ($before -contains $h) { continue }
            if (-not [ExtWin]::IsWindowVisible($h)) { continue }
            if (-not (Test-IsTarget $h)) { continue }
            $t = [ExtWin]::Title($h)
            if ($t.Trim().Length -gt 0) { $newWin = $h; break }
        }
        if ($newWin -ne [IntPtr]::Zero) { break }
        Start-Sleep -Milliseconds 500
    }
    if ($newWin -eq [IntPtr]::Zero) { throw 'new browser window not found' }
    Write-ProgressLog ("auto-install: window '" + [ExtWin]::Title($newWin) + "'")
    [ExtWin]::Move($newWin, 80, 80)
    [ExtWin]::Invisible($newWin)

    # If the page did not open on the extension URL (fresh instance), navigate.
    $t0 = [ExtWin]::Title($newWin)
    if ($t0 -notmatch 'xtens') {
        $nav = [ExtWin]::Navigate($newWin, $binfo.url)
        if ($nav -ne 'SET') { throw "address-bar navigation failed: $nav" }
        for ($i = 0; $i -lt 40; $i++) {
            [ExtWin]::PostEnter($newWin)
            Start-Sleep -Milliseconds 400
            if ([ExtWin]::Title($newWin) -match 'xtens') { break }
        }
    }

    # Wait for the page to render (dev-mode toggle + load-unpacked visible).
    Start-Sleep -Seconds 2

    # Already installed? The card text contains the extension name.
    $page = [ExtWin]::DumpText($newWin)
    if ($ReloadOnly) {
        # In-place reload of the running unpacked extension: click the card's
        # Reload button (dev mode). The service worker re-registers from the
        # folder on disk WITHOUT removing/re-adding, so the browser keeps its
        # extension id, permissions and the user's tabs untouched.
        if ($page -notmatch [regex]::Escape($EXT_NAME)) {
            throw "extension not installed ('$EXT_NAME' not found) - run the normal auto-install first"
        }
        $r = [ExtWin]::ClickButton($newWin, 'Recarregar', 'dev-reload-button', 15000, $false)
        Write-ProgressLog "auto-install: reload button -> $r"
        if ($r -eq 'NOT_FOUND') {
            $r2 = [ExtWin]::ClickButton($newWin, 'Reload', 'reload', 8000, $false)
            Write-ProgressLog "auto-install: reload fallback -> $r2"
            if ($r2 -eq 'NOT_FOUND') { throw 'reload button never appeared (developer mode off?)' }
        }
        # Wait for the card to come back after the reload (SW re-registers).
        $found = $false
        for ($i = 0; $i -lt 40; $i++) {
            if ([ExtWin]::DumpText($newWin) -match [regex]::Escape($EXT_NAME)) { $found = $true; break }
            Start-Sleep -Milliseconds 500
        }
        if (-not $found) { throw 'extension card did not reappear after reload' }
        $result.ok = $true
        $result.reloaded = $true
        $result.installed = $true
        $idm = [regex]::Match([ExtWin]::DumpText($newWin), 'ID:\s*([a-z0-9]{32})')
        if ($idm.Success) { $result.extension_id = $idm.Groups[1].Value }
        if ($ExpectedVersion) {
            $result.version_found = ([ExtWin]::DumpText($newWin) -match [regex]::Escape($ExpectedVersion))
            Write-ProgressLog "auto-install: expected version '$ExpectedVersion' found: $($result.version_found)"
        }
    } elseif ($page -match [regex]::Escape($EXT_NAME)) {
        Write-ProgressLog 'auto-install: extension already present, nothing to do'
        $result.ok = $true
        $result.installed = $true
        $idm = [regex]::Match($page, 'ID:\s*([a-z0-9]{32})')
        if ($idm.Success) { $result.extension_id = $idm.Groups[1].Value }
    } else {
        # Load unpacked (dev mode must be on — the toggle is `devMode`).
        $r = [ExtWin]::ClickButton($newWin, '', 'loadUnpacked', 12000, $false)
        Write-ProgressLog "auto-install: load unpacked -> $r"
        if ($r -eq 'NOT_FOUND') {
            $r2 = [ExtWin]::ClickButton($newWin, '', 'devMode', 8000, $true)
            Write-ProgressLog "auto-install: dev mode -> $r2"
            Start-Sleep -Seconds 2
            $r = [ExtWin]::ClickButton($newWin, '', 'loadUnpacked', 12000, $false)
            Write-ProgressLog "auto-install: load unpacked retry -> $r"
        }
        if ($r -eq 'NOT_FOUND') { throw 'Load unpacked button never appeared' }

        # The native folder dialog appears as a new top-level window.
        $dlg = [IntPtr]::Zero
        for ($i = 0; $i -lt 30; $i++) {
            $now = [ExtWin]::AllWindows()
            foreach ($h in $now) {
                if ($before -contains $h -or $h -eq $newWin) { continue }
                if (-not [ExtWin]::IsWindowVisible($h)) { continue }
                $t = [ExtWin]::Title($h)
                if ($t.Trim().Length -gt 0) { $dlg = $h; break }
            }
            if ($dlg -ne [IntPtr]::Zero) { break }
            Start-Sleep -Milliseconds 400
        }
        if ($dlg -eq [IntPtr]::Zero) { throw 'folder picker dialog never appeared' }
        Start-Sleep -Milliseconds 800
        Write-ProgressLog ("auto-install: dialog '" + [ExtWin]::Title($dlg) + "'")

        $dr = [ExtWin]::DrivePicker($dlg, $ExtensionDir, 25000)
        Write-ProgressLog "auto-install: picker -> $dr"

        # Verify the extension card appears.
        $found = $false
        for ($i = 0; $i -lt 40; $i++) {
            $txt = [ExtWin]::DumpText($newWin)
            if ($txt -match [regex]::Escape($EXT_NAME)) { $found = $true; break }
            Start-Sleep -Milliseconds 500
        }
        if (-not $found) { throw "extension card did not appear (picker: $dr)" }
        $idm = [regex]::Match([ExtWin]::DumpText($newWin), 'ID:\s*([a-z0-9]{32})')
        if ($idm.Success) { $result.extension_id = $idm.Groups[1].Value }
        $result.ok = $true
        $result.installed = $true
        Write-ProgressLog 'auto-install: installed OK'
    }
} catch {
    $result.error = $_.Exception.Message
    Write-ProgressLog "auto-install error: $($result.error)"
} finally {
    if ($newWin -ne [IntPtr]::Zero) {
        try { [ExtWin]::Close($newWin) } catch { }
        # The frontend opens the clip editor right after the route reports
        # 'installed' — if this extensions window is still alive then, the
        # editor tab can land inside it and die with it (silent clip loss).
        # Report success only once the window handle is confirmed gone.
        $sw = [System.Diagnostics.Stopwatch]::StartNew()
        while ($sw.Elapsed.TotalSeconds -lt 10 -and [ExtWin]::IsWindow($newWin)) {
            Start-Sleep -Milliseconds 300
        }
        if ([ExtWin]::IsWindow($newWin)) {
            Write-ProgressLog 'auto-install: warning - extensions window did not close in 10s'
        }
    }
    Write-Output ($result | ConvertTo-Json -Compress)
}
