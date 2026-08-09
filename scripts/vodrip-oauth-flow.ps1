# OAuth consent flow fully off-screen — SAME mechanism as the extension
# install (final6.2): spawn a NEW EMPTY Chrome window (user's real profile,
# already logged in), move it off-screen + minimize ~1 frame after creation
# (before any content paints) using the consultgpt off-screen technique
# (browser_pool.headed_offscreen_args: --start-minimized + -32000,-32000),
# then navigate via clipboard+^l+^v. The consent page never renders on the
# viewport. Click "Autorizar" via UIA (virtual invoke, no mouse), poll the
# dev server until the token lands in settings, close ONLY the new window.
# User's windows untouched; clipboard backed up and restored.
#
# stdlib/BCL only. ASCII only (PS 5.1 ANSI) - accented button names use \u
# escapes in the C# so the file stays ASCII.
#
# Usage:
#   powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File `
#     vodrip-oauth-flow.ps1 -Url "<authorize-url>" -OrigToken "<current>"
#
# stdout: "OK <token>" or "ERR: <why>".

param(
    [string]$Url = '',
    [string]$OrigToken = ''
)

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
Add-Type @"
using System;
using System.Text;
using System.Collections.Generic;
using System.Runtime.InteropServices;
public static class OffF2 {
  public delegate bool EnumProc(IntPtr hWnd, IntPtr lParam);
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc cb, IntPtr lParam);
  [DllImport("user32.dll", CharSet = CharSet.Unicode)] public static extern int GetWindowText(IntPtr h, StringBuilder sb, int max);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
  [DllImport("user32.dll")] public static extern bool PostMessage(IntPtr h, uint msg, IntPtr w, IntPtr l);
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
  [DllImport("user32.dll")] public static extern bool SetWindowPos(IntPtr h, IntPtr after, int x, int y, int cx, int cy, uint flags);
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
  [DllImport("user32.dll")] public static extern int GetWindowLong(IntPtr h, int idx);
  [DllImport("user32.dll")] public static extern int SetWindowLong(IntPtr h, int idx, int val);
  [DllImport("user32.dll")] public static extern bool SetLayeredWindowAttributes(IntPtr h, uint crKey, byte alpha, uint flags);
  public const int GWL_EXSTYLE = -20;
  public const int WS_EX_LAYERED = 0x00080000;
  public const uint LWA_ALPHA = 0x00000002;
  public static void Invisible(IntPtr h) {
    int ex = GetWindowLong(h, GWL_EXSTYLE);
    SetWindowLong(h, GWL_EXSTYLE, ex | WS_EX_LAYERED);
    SetLayeredWindowAttributes(h, 0, 0, LWA_ALPHA);
  }
  [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
  [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint pid);
  [DllImport("kernel32.dll")] public static extern uint GetCurrentThreadId();
  [DllImport("user32.dll")] public static extern bool AttachThreadInput(uint a, uint b, bool attach);
  [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr hWnd);
  [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int n);
  public struct RECT { public int Left, Top, Right, Bottom; }
  public const int SW_RESTORE = 9;
  public const uint SWP_NOZORDER = 0x0004;
  public const uint SWP_NOACTIVATE = 0x0010;
  public const uint WM_CLOSE = 0x0010;
  public const uint WM_KEYDOWN = 0x0100;
  public const uint WM_KEYUP = 0x0101;
  public const uint VK_RETURN = 0x0D;
  public static void PostEnter(IntPtr h) {
    PostMessage(h, WM_KEYDOWN, (IntPtr)VK_RETURN, IntPtr.Zero);
    PostMessage(h, WM_KEYUP, (IntPtr)VK_RETURN, IntPtr.Zero);
  }
  public const uint CF_UNICODETEXT = 13;
  public const uint GMEM_MOVEABLE = 0x0002;
  [DllImport("user32.dll")] public static extern bool EnumDisplayMonitors(IntPtr hdc, IntPtr lprc, MonitorEnumProc cb, IntPtr lParam);
  public delegate bool MonitorEnumProc(IntPtr hMonitor, IntPtr hdc, ref RECT rc, IntPtr lParam);
  private static System.Text.StringBuilder MonitorsSb;
  private static bool MonProc(IntPtr h, IntPtr dc, ref RECT rc, IntPtr lp) {
    MonitorsSb.Append('[').Append(rc.Left).Append(',').Append(rc.Top).Append('-').Append(rc.Right).Append(',').Append(rc.Bottom).Append("] ");
    return true;
  }
  public static string Monitors() {
    MonitorsSb = new System.Text.StringBuilder();
    EnumDisplayMonitors(IntPtr.Zero, IntPtr.Zero, MonProc, IntPtr.Zero);
    return MonitorsSb.ToString();
  }
  public static void Move(IntPtr h, int x, int y) { RECT r; GetWindowRect(h, out r); SetWindowPos(h, IntPtr.Zero, x, y, r.Right - r.Left, r.Bottom - r.Top, SWP_NOZORDER | SWP_NOACTIVATE); }
  public static List<IntPtr> FindWindowsAll(string titlePart) {
    var res = new List<IntPtr>();
    EnumWindows((h, l) => {
      if (!IsWindowVisible(h)) return true;
      var t = new StringBuilder(256); GetWindowText(h, t, 256);
      if (t.ToString().Contains(titlePart)) res.Add(h);
      return true;
    }, IntPtr.Zero);
    return res;
  }
  public static bool BringToForeground(IntPtr h) {
    if (GetForegroundWindow() == h) return true;
    if (!IsWindowVisible(h)) return false;
    if (IsIconic(h)) ShowWindow(h, SW_RESTORE);
    uint t; GetWindowThreadProcessId(h, out t);
    uint me = GetCurrentThreadId();
    try {
      AttachThreadInput(me, t, true);
      SetForegroundWindow(h);
    } finally { AttachThreadInput(me, t, false); }
    return GetForegroundWindow() == h;
  }
  public static void Close(IntPtr h) { PostMessage(h, WM_CLOSE, IntPtr.Zero, IntPtr.Zero); }
  public static Dictionary<uint, byte[]> Backup() {
    var res = new Dictionary<uint, byte[]>();
    if (!OpenClipboard(IntPtr.Zero)) return res;
    try {
      uint f = 0;
      while ((f = EnumClipboardFormats(f)) != 0) {
        IntPtr h = GetClipboardData(f);
        if (h == IntPtr.Zero) continue;
        IntPtr p = GlobalLock(h);
        if (p == IntPtr.Zero) continue;
        int sz = (int)GlobalSize(h);
        var buf = new byte[sz];
        Marshal.Copy(p, buf, 0, sz);
        GlobalUnlock(h);
        res[f] = buf;
      }
    } finally { CloseClipboard(); }
    return res;
  }
  public static void SetText(string s) {
    OpenClipboard(IntPtr.Zero);
    EmptyClipboard();
    var bytes = Encoding.Unicode.GetBytes(s + "\0");
    IntPtr h = GlobalAlloc(GMEM_MOVEABLE, (IntPtr)bytes.Length);
    IntPtr p = GlobalLock(h);
    Marshal.Copy(bytes, 0, p, bytes.Length);
    GlobalUnlock(h);
    SetClipboardData(CF_UNICODETEXT, h);
    CloseClipboard();
  }
  public static void Restore(Dictionary<uint, byte[]> data) {
    if (data.Count == 0) return;
    OpenClipboard(IntPtr.Zero);
    EmptyClipboard();
    foreach (var kv in data) {
      IntPtr h = GlobalAlloc(GMEM_MOVEABLE, (IntPtr)kv.Value.Length);
      IntPtr p = GlobalLock(h);
      Marshal.Copy(kv.Value, 0, p, kv.Value.Length);
      GlobalUnlock(h);
      SetClipboardData(kv.Key, h);
    }
    CloseClipboard();
  }
  [DllImport("user32.dll")] public static extern bool OpenClipboard(IntPtr h);
  [DllImport("user32.dll")] public static extern bool CloseClipboard();
  [DllImport("user32.dll")] public static extern bool EmptyClipboard();
  [DllImport("user32.dll")] public static extern uint EnumClipboardFormats(uint f);
  [DllImport("user32.dll")] public static extern IntPtr GetClipboardData(uint f);
  [DllImport("user32.dll")] public static extern IntPtr SetClipboardData(uint f, IntPtr h);
  [DllImport("kernel32.dll")] public static extern IntPtr GlobalAlloc(uint flags, IntPtr size);
  [DllImport("kernel32.dll")] public static extern IntPtr GlobalLock(IntPtr h);
  [DllImport("kernel32.dll")] public static extern bool GlobalUnlock(IntPtr h);
  [DllImport("kernel32.dll")] public static extern IntPtr GlobalSize(IntPtr h);
}
"@
Add-Type -TypeDefinition @"
using System;
using System.Threading;
using System.Windows.Automation;
public static class UiaClick {
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
  public static string BarValue(IntPtr hwnd) {
    try {
      var win = AutomationElement.FromHandle(hwnd);
      var edits = win.FindAll(TreeScope.Descendants,
        new PropertyCondition(AutomationElement.ControlTypeProperty, ControlType.Edit));
      foreach (AutomationElement e in edits) {
        var name = e.Current.Name ?? "";
        if (name.IndexOf("Endere", StringComparison.OrdinalIgnoreCase) >= 0 ||
            name.IndexOf("Address", StringComparison.OrdinalIgnoreCase) >= 0) {
          var vp = e.GetCurrentPattern(ValuePattern.Pattern) as ValuePattern;
          return vp != null ? (vp.Current.Value ?? "(vazio)") : "(sem value)";
        }
      }
      return "(sem barra)";
    } catch (Exception ex) { return "ERR: " + ex.Message; }
  }
  public static string DumpButtons(IntPtr hwnd) {
    try {
      var win = AutomationElement.FromHandle(hwnd);
      var btns = win.FindAll(TreeScope.Descendants,
        new PropertyCondition(AutomationElement.ControlTypeProperty, ControlType.Button));
      var sb = new System.Text.StringBuilder();
      int n = 0;
      foreach (AutomationElement b in btns) {
        if (n++ >= 40) break;
        sb.Append('[').Append(b.Current.Name ?? "").Append("] ");
      }
      return sb.Length == 0 ? "(nenhum botao)" : sb.ToString();
    } catch (Exception ex) { return "ERR: " + ex.Message; }
  }
  public static string DumpText(IntPtr hwnd) {
    try {
      var win = AutomationElement.FromHandle(hwnd);
      var texts = win.FindAll(TreeScope.Descendants,
        new PropertyCondition(AutomationElement.ControlTypeProperty, ControlType.Text));
      var sb = new System.Text.StringBuilder();
      int n = 0;
      foreach (AutomationElement t in texts) {
        if (n++ >= 25) break;
        var nm = t.Current.Name ?? "";
        if (nm.Trim().Length > 0) sb.Append('[').Append(nm).Append("] ");
      }
      return sb.Length == 0 ? "(sem textos)" : sb.ToString();
    } catch (Exception ex) { return "ERR: " + ex.Message; }
  }
  public static string ClickAuthorize(IntPtr hwnd, int timeoutMs) {
    var win = AutomationElement.FromHandle(hwnd);
    var cond = new AndCondition(
      new PropertyCondition(AutomationElement.ControlTypeProperty, ControlType.Button),
      new OrCondition(
        new PropertyCondition(AutomationElement.NameProperty, "Autorizar"),
        new PropertyCondition(AutomationElement.NameProperty, "Authorize")));
    var sw = System.Diagnostics.Stopwatch.StartNew();
    while (sw.ElapsedMilliseconds < timeoutMs) {
      try {
        var btn = win.FindFirst(TreeScope.Descendants, cond);
        if (btn != null) {
          bool enabled = btn.Current.IsEnabled;
          if (!enabled) { Thread.Sleep(300); continue; }  // renderer paused (greyed) — wait until live
          var inv = btn.GetCurrentPattern(InvokePattern.Pattern) as InvokePattern;
          if (inv != null) { inv.Invoke(); return "CLICKED"; }
          return "NO_INVOKE";
        }
      } catch { }
      Thread.Sleep(300);
    }
    return "NOT_FOUND";
  }
}
"@ -ReferencedAssemblies UIAutomationClient,UIAutomationTypes

$backup = [OffF2]::Backup()
try {
    $chromeExe = "$env:ProgramFiles\Google\Chrome\Application\chrome.exe"
    if (-not (Test-Path $chromeExe)) { $chromeExe = (Get-Command chrome -ErrorAction SilentlyContinue).Source }
    if (-not $chromeExe) { Write-Output 'ERR: chrome.exe nao achado'; exit 1 }

    # final6.2 mechanism: empty window first, move off-screen ~1 frame
    # (15ms poll) BEFORE any content paints, then navigate via clipboard.
    # consultgpt off-screen position (-32000,-32000): no frame ever paints
    # on the viewport. NOTE: no SW_MINIMIZE — a minimized window pauses the
    # renderer and the UIA tree goes empty (button never found).
    $before = @([OffF2]::FindWindowsAll('Google Chrome'))
    Start-Process -FilePath $chromeExe -ArgumentList '--new-window'
    $newWin = [IntPtr]::Zero
    for ($i = 0; $i -lt 100 -and $newWin -eq [IntPtr]::Zero; $i++) {
        Start-Sleep -Milliseconds 15
        foreach ($w in [OffF2]::FindWindowsAll('Google Chrome')) {
            if ($before -notcontains $w) { $newWin = $w; break }
        }
    }
    if ($newWin -eq [IntPtr]::Zero) { Write-Output 'ERR: janela nova nao abriu'; exit 1 }
    [OffF2]::Move($newWin, -32000, -32000)
    Start-Sleep -Milliseconds 120
    # NO BringToForeground here — the flow must never steal the user's focus.
    # UIA SetValue + PostMessage(Enter) work without foreground.
    # Prefer a second (virtual) monitor: window is "visible" there so the
    # renderer stays live (button enabled) while nothing shows on screen.
    $mons = [OffF2]::Monitors()
    Write-Output "MONS: $mons"
    $placed = $false
    foreach ($m in [regex]::Matches($mons, '\[(-?\d+),(-?\d+)-(-?\d+),(-?\d+)\]')) {
        $left = [int]$m.Groups[1].Value
        if ($left -gt 100) { [OffF2]::Move($newWin, $left + 60, 80); $placed = $true; break }
    }
    if (-not $placed) {  # no second monitor — on-screen but fully transparent
        [OffF2]::Move($newWin, 80, 80)
        [OffF2]::Invisible($newWin)
    }

    $nav = [UiaClick]::Navigate($newWin, $Url)
    if ($nav -ne 'SET') { Write-Output "ERR: navegacao: $nav"; exit 1 }
    $title = ''
    for ($i = 0; $i -lt 30; $i++) {
        [OffF2]::PostEnter($newWin)
        Start-Sleep -Milliseconds 400
        $t = New-Object System.Text.StringBuilder 256
        [void][OffF2]::GetWindowText($newWin, $t, 256)
        $title = $t.ToString()
        if ($title -like '*Twitch*') { break }
        if (($i % 4) -eq 3) { [void][UiaClick]::Navigate($newWin, $Url) }
    }
    Write-Output "TITLE: $title"

    $r = [UiaClick]::ClickAuthorize($newWin, 30000)
    if ($r -ne 'CLICKED') {
        # Botão sumiu = consent já foi (redirect p/ callback) — checa antes de falhar.
        $t2 = New-Object System.Text.StringBuilder 256
        [void][OffF2]::GetWindowText($newWin, $t2, 256)
        if ($t2.ToString() -notlike '*Twitch token*') { Write-Output "ERR: clique: $r"; exit 1 }
    }
    Write-Output "BAR: $([UiaClick]::BarValue($newWin))"

    $tok = $OrigToken
    for ($i = 0; $i -lt 30; $i++) {
        Start-Sleep -Seconds 1
        try {
            $s = Invoke-RestMethod -Uri 'http://localhost:7897/api/settings' -TimeoutSec 5
            $tok = [string]$s.twitch_helix_token
        } catch { $tok = $OrigToken }
        if ($tok -and $tok -ne $OrigToken) { break }
    }
    Start-Sleep -Milliseconds 300
    [OffF2]::Close($newWin)
    if ($tok -and $tok -ne $OrigToken) { Write-Output "OK $tok" }
    else { Write-Output 'ERR: token nao mudou'; exit 1 }
} finally {
    [OffF2]::Restore($backup)
}
