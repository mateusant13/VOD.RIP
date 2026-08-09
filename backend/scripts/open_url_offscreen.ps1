# Open a URL in a NEW off-screen Chrome window, hold, close it.
#
# Why: cookie maintenance (e.g. a missing twitch.tv auth-token) needs the
# user's logged-in Chrome session, but the user must never see a window pop.
# Mechanism (proven, see the extension auto-install flow): the browser's own
# window is untouched; a brand-new window is spawned via `chrome.exe
# --new-window` (a window command — the singleton routes it, no URL drop),
# moved off-screen ~1 frame after creation, navigated via clipboard+^l+^v,
# held, then closed. The extension cookie bridge picks up session cookies on
# load (onChanged + 10-min heartbeat).
#
# stdlib/BCL only. ASCII only (PS 5.1 ANSI).
#
# Usage:
#   powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File `
#     open_url_offscreen.ps1 -Url "https://www.twitch.tv/" [-HoldSeconds 5]
#
# stdout: "OK" on success, "ERR: <why>" otherwise.

param(
    [string]$Url = 'https://www.twitch.tv/',
    [int]$HoldSeconds = 5
)

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
Add-Type @"
using System;
using System.Text;
using System.Collections.Generic;
using System.Runtime.InteropServices;
public static class OffF {
  public delegate bool EnumProc(IntPtr hWnd, IntPtr lParam);
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc cb, IntPtr lParam);
  [DllImport("user32.dll", CharSet = CharSet.Unicode)] public static extern int GetWindowText(IntPtr h, StringBuilder sb, int max);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
  [DllImport("user32.dll")] public static extern bool PostMessage(IntPtr h, uint msg, IntPtr w, IntPtr l);
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
  [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
  [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint pid);
  [DllImport("kernel32.dll")] public static extern uint GetCurrentThreadId();
  [DllImport("user32.dll")] public static extern bool AttachThreadInput(uint a, uint b, bool attach);
  [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr hWnd);
  [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int n);
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
  [DllImport("user32.dll")] public static extern bool SetWindowPos(IntPtr h, IntPtr after, int x, int y, int cx, int cy, uint flags);
  public struct RECT { public int Left, Top, Right, Bottom; }
  public const int SW_RESTORE = 9;
  public const uint SWP_NOZORDER = 0x0004;
  public const uint SWP_NOACTIVATE = 0x0010;
  public const uint WM_CLOSE = 0x0010;
  public static RECT GetRect(IntPtr h) { RECT r; GetWindowRect(h, out r); return r; }
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
  public static IntPtr FindWindow(string titlePart) {
    IntPtr found = IntPtr.Zero;
    EnumWindows((h, l) => {
      if (!IsWindowVisible(h)) return true;
      var t = new StringBuilder(256); GetWindowText(h, t, 256);
      if (t.ToString().Contains(titlePart)) { found = h; return false; }
      return true;
    }, IntPtr.Zero);
    return found;
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
  public const uint CF_UNICODETEXT = 13;
  public const uint GMEM_MOVEABLE = 0x0002;
}
"@

$backup = [OffF]::Backup()
try {
    $chrome = [OffF]::FindWindow('Google Chrome')
    if ($chrome -eq [IntPtr]::Zero) { Write-Output 'ERR: Chrome nao achado'; exit 1 }
    $chromeExe = "$env:ProgramFiles\Google\Chrome\Application\chrome.exe"
    if (-not (Test-Path $chromeExe)) { $chromeExe = (Get-Command chrome -ErrorAction SilentlyContinue).Source }
    if (-not $chromeExe) { Write-Output 'ERR: chrome.exe nao achado'; exit 1 }
    Start-Process -FilePath $chromeExe -ArgumentList '--new-window'
    Start-Sleep -Milliseconds 200

    $newWin = [IntPtr]::Zero
    for ($i = 0; $i -lt 30 -and $newWin -eq [IntPtr]::Zero; $i++) {
        Start-Sleep -Milliseconds 30
        foreach ($w in [OffF]::FindWindowsAll('Google Chrome')) {
            if ($w -ne $chrome) { $newWin = $w; break }
        }
    }
    if ($newWin -eq [IntPtr]::Zero) { Write-Output 'ERR: nova janela nao abriu'; exit 1 }
    [OffF]::Move($newWin, -3000, -3000)
    Start-Sleep -Milliseconds 120
    [void][OffF]::BringToForeground($newWin)

    [OffF]::SetText($Url)
    [System.Windows.Forms.SendKeys]::SendWait('^l')
    Start-Sleep -Milliseconds 80
    [System.Windows.Forms.SendKeys]::SendWait('^v{ENTER}')

    Start-Sleep -Seconds $HoldSeconds
    [OffF]::Close($newWin)
    Write-Output 'OK'
} finally {
    [OffF]::Restore($backup)
}
