# One-click extension auto-install (productized CookieInstallWorker flow)
# + zero-window version reload.
#
# INSTALL path (first-time only): drive the user's REAL profile entirely by
# UIA, SILENTLY — spawn chrome --new-window (reuses the RUNNING instance,
# never kills anything), place the window ON-SCREEN (an off-screen alpha-0
# window is treated as occluded and the WebUI renderer is throttled) but
# alpha-0 (LWA_ALPHA=0: invisible to the user, fully live to UIA), click
# "Load unpacked" by automation id, drive the folder dialog by UIA alone
# (ValuePattern.SetValue on the "Pasta:" edit + Invoke "Selecionar pasta" —
# posted keyboard types nothing into it, proven live 2026-08-13), verify the
# install, capture its ID, close the window. ZERO keyboard input, ZERO focus
# steal: navigation is omnibox SetValue + a POSTED Enter (posted keys cannot
# leak — they go to our window, not the foreground), and if the modal picker
# still activates, a ~10ms foreground watch bounces focus back to the user's
# window (AttachThreadInput + SetForegroundWindow). The reload path NEVER
# routes through this window at all.
#
# Chrome 151.0.7922.137 regression handling (2026-08-13): chrome:// URLs are
# dropped from the command line (the window opens on the NTP instead) and
# posted WM_KEYDOWN never reaches the omnibox — but UIA SetValue + a POSTED
# Enter navigates a non-occluded window reliably. Occlusion: an off-screen
# alpha-0 window is occluded (WebUI throttled, page invisible to UIA — 39
# nodes, no ids), while an ON-SCREEN alpha-0 window renders fully (330 UIA
# nodes incl. loadUnpacked/devMode). Install state is verified via the
# profile's Secure Preferences instead of page text.
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
#     [-Browser chrome|msedge|brave] [-DebugPort 7897] [-DryRun]
#   cookie_auto_install.ps1 -ExtensionDir "C:\...\VOD.RIP-cookies" -ReloadOnly `
#     [-ExpectedVersion 0.8.6] [-DryRun]
#
# -ReloadOnly: ZERO-WINDOW reload of a live unpacked extension. Chrome does
# NOT hot-reload a loaded folder on file change, so after the caller stages
# a new copy the service worker must re-register from disk. This mode NEVER
# spawns chrome and NEVER opens a tab or window (the user mandate: no focus
# steal, ever — "para de abrir uma nova guia inutil que rouba o foco"). It
# reads the staged folder's manifest version, POSTs /api/extension/reload
# {to:<version>} to the local backend, then polls GET /api/extension/status
# until the directive clears (max 90s). The extension's own service worker
# does the actual reload: it polls the same endpoint on a 30s alarm,
# chrome.runtime.reload()s itself in place, and the fresh SW POSTs
# reload-done — which clears the directive. The directive clearing IS the
# version proof: the SW only confirms after its manifest version matches the
# version the backend ships. -ExpectedVersion is accepted for CLI compat
# (the reload target is always the on-disk version).
#
# stdout: exactly ONE JSON line (the result), human progress goes to stderr:
#   {"ok":true,"installed":true,"extension_id":"...","error":null}
#   {"ok":true,"reloaded":true,"extension_id":null,"version_found":true,"error":null}
# DryRun prints the resolved plan without touching the browser or backend.

param(
    [string]$ExtensionDir,
    [string]$Browser = 'chrome',
    [int]$DebugPort = 7897,  # CLI-compat alias for the LOCAL API port (the zero-window reload POSTs/polls 127.0.0.1:$DebugPort); the UIA install path needs no port at all
    [switch]$DryRun,
    [switch]$ReloadOnly,
    [switch]$Force,
    [string]$ExpectedVersion = ''
)

$ErrorActionPreference = 'Stop'

# Card name to look for on chrome://extensions — derived from the TARGET
# extension's manifest (generic installer; folder leaf as fallback).
$EXT_NAME = Split-Path -Leaf $ExtensionDir
try { $EXT_NAME = (Get-Content -LiteralPath (Join-Path $ExtensionDir 'manifest.json') -Raw | ConvertFrom-Json).name } catch { }

$script:koT0 = [DateTime]::UtcNow
function Write-ProgressLog([string]$msg) {
    [Console]::Error.WriteLine(("[{0,6:N0}ms] {1}" -f ([DateTime]::UtcNow - $script:koT0).TotalMilliseconds, $msg))
}

# ---- Chrome 151 profile-based verification ----
# Chrome 151.0.7922.137 does not expose chrome:// WebUI content to UIA, so the
# extension card cannot be read from the page. The install is instead verified
# via the profile's Secure Preferences: extensions.settings holds one entry
# per extension keyed by ID, with "path" == the staged folder.
function Get-SecurePrefsPath([string]$udd) {
    return Join-Path (Join-Path $env:LOCALAPPDATA $udd) 'Default\Secure Preferences'
}
function Test-ExtInProfile([string]$udd, [string]$dir) {
    $p = Get-SecurePrefsPath $udd
    if (-not (Test-Path -LiteralPath $p)) { return $false }
    $esc = $dir.Replace('\', '\\').Replace('"', '\"')
    try { return ((Get-Content -Raw -LiteralPath $p) -match ('"path"\s*:\s*"' + [regex]::Escape($esc) + '"')) } catch { return $false }
}
function Get-ExtIdFromProfile([string]$udd, [string]$dir) {
    $p = Get-SecurePrefsPath $udd
    if (-not (Test-Path -LiteralPath $p)) { return '' }
    # JSON parse, not regex: the old regex ('\{[^}]*"path"') broke on nested
    # braces (commands: {}, granted_permissions: {...}) that precede "path".
    try {
        $j = Get-Content -Raw -LiteralPath $p | ConvertFrom-Json
        $dirN = $dir.TrimEnd('\').ToLowerInvariant()
        foreach ($prop in $j.extensions.settings.PSObject.Properties) {
            $pv = $prop.Value.path
            if ($pv -and $pv.TrimEnd('\').ToLowerInvariant() -eq $dirN) { return $prop.Name }
        }
    } catch { }
    return ''
}

# --- zero-window reload (directive-based, NO chrome spawn) --------------------
# The extension is already installed UNPACKED but may be running stale code
# (Chrome does NOT hot-reload a loaded folder on file change). The reload is
# directive-based and NEVER touches the browser: read the staged folder's
# version, POST /api/extension/reload {to:<version>} to the local backend,
# then poll GET /api/extension/status until the directive clears (max 90s).
# The extension's own service worker does the actual work — it polls the same
# endpoint on a 30s alarm (and on content-script messages), reloads ITSELF in
# place via chrome.runtime.reload() (extension id, permissions and every user
# tab kept; no window, no tab, no focus steal), and the fresh SW POSTs
# reload-done, which clears the directive. The directive clearing IS the
# version proof: the SW only confirms after its manifest version matches the
# version the backend ships.
if ($ReloadOnly) {
    $relManifest = Join-Path $ExtensionDir 'manifest.json'
    if (-not (Test-Path -LiteralPath $relManifest)) {
        Write-Output (@{ ok = $false; reloaded = $false; extension_id = $null; version_found = $false; error = "extension folder missing manifest.json: $ExtensionDir" } | ConvertTo-Json -Compress)
        exit 1
    }
    $targetVersion = ''
    try {
        $targetVersion = "$((Get-Content -LiteralPath $relManifest -Raw | ConvertFrom-Json).version)"
    } catch { $targetVersion = '' }
    if (-not $targetVersion) {
        Write-Output (@{ ok = $false; reloaded = $false; extension_id = $null; version_found = $false; error = "cannot read version from $relManifest" } | ConvertTo-Json -Compress)
        exit 1
    }
    # Mirror the SW's getApiBase (modules/cookie_bridge.mjs): default
    # http://127.0.0.1:7897 with a per-install override. The SW's override
    # lives in chrome.storage.local, which a PS1 cannot read; VODRIP_API_BASE
    # env covers the same "different port" use case, and -DebugPort stays as
    # the CLI-compat alias for the API port.
    $apiBase = if ($env:VODRIP_API_BASE) { $env:VODRIP_API_BASE.TrimEnd('/') } else { "http://127.0.0.1:$DebugPort" }

    if ($DryRun) {
        Write-Output (@{
            ok = $true; dryRun = $true
            mechanism = 'zero-window-directive'
            chrome_spawn = $false
            reloadOnly = $true
            extension_dir = $ExtensionDir
            target_version = $targetVersion
            api_base = $apiBase
            expected_version = $ExpectedVersion
            actions = @(
                "read live extension version from $relManifest (target: $targetVersion)",
                "POST $apiBase/api/extension/reload {to:'$targetVersion'} - persists the reload directive",
                "poll GET $apiBase/api/extension/status until reloadTo clears (fresh SW confirms via reload-done; max 90s)",
                "NO chrome.exe spawn, NO new window, NO new tab - the SW self-reloads in place"
            )
        } | ConvertTo-Json -Compress -Depth 4)
        exit 0
    }

    $result = @{ ok = $false; reloaded = $false; extension_id = $null; version_found = $false; error = $null }
    try {
        $status = Invoke-RestMethod -Uri "$apiBase/api/extension/status" -Method Get -TimeoutSec 10
        Write-ProgressLog "reload: backend up (bundled version '$($status.version)')"
        $body = @{ to = $targetVersion } | ConvertTo-Json -Compress
        Invoke-RestMethod -Uri "$apiBase/api/extension/reload" -Method Post -ContentType 'application/json' -Body $body -TimeoutSec 10 | Out-Null
        Write-ProgressLog "reload: directive set to '$targetVersion' - waiting for the SW to self-reload (no windows involved)"
        $deadline = (Get-Date).AddSeconds(90)
        $cleared = $false
        while ((Get-Date) -lt $deadline) {
            Start-Sleep -Seconds 2
            try {
                $st = Invoke-RestMethod -Uri "$apiBase/api/extension/status" -Method Get -TimeoutSec 10
                if (-not $st.reloadTo) { $cleared = $true; break }
            } catch {
                Write-ProgressLog "reload: status poll failed (backend restarting?) - $($_.Exception.Message)"
            }
        }
        if (-not $cleared) {
            throw "reload directive not cleared within 90s - is Chrome running with the extension loaded?"
        }
        $result.ok = $true
        $result.reloaded = $true
        $result.version_found = $true
    } catch {
        $result.error = $_.Exception.Message
        Write-ProgressLog "reload error: $($result.error)"
    }
    Write-Output ($result | ConvertTo-Json -Compress)
    exit 0
}

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
        mechanism = 'uia-visible-window'
        reloadOnly = $false
        extension_dir = $ExtensionDir
        expected_version = $ExpectedVersion
    } | ConvertTo-Json -Compress)
    exit 0
}

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
  public const uint SWP_NOSIZE = 0x0001;
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
  // ---- Event-driven waits (WinEvent + UIA StructureChanged; NO polling) ----
  // Research-backed (2026-08-13): WINEVENT_OUTOFCONTEXT delivers events as
  // messages to the hooking thread's queue — the wait must pump (PeekMessage)
  // but never sleeps on the condition; latency is message-queue delivery
  // (~2-10ms), so the spawned Chrome window is caught BEFORE its first paint
  // and Hide() makes frame #1 already transparent.
  public const uint EVENT_OBJECT_CREATE = 0x8000;
  public const uint EVENT_OBJECT_SHOW = 0x8002;
  public const uint EVENT_OBJECT_NAMECHANGE = 0x800C;
  public const uint WINEVENT_OUTOFCONTEXT = 0x0000;
  public const int OBJID_WINDOW = 0;
  public const uint PM_REMOVE = 0x0001;
  [DllImport("user32.dll")] public static extern IntPtr SetWinEventHook(uint evMin, uint evMax, IntPtr hmod, WinEventProc cb, uint pid, uint tid, uint flags);
  [DllImport("user32.dll")] public static extern bool UnhookWinEvent(IntPtr hHook);
  [DllImport("user32.dll")] public static extern bool PeekMessage(out MSG lpMsg, IntPtr hWnd, uint min, uint max, uint flags);
  [DllImport("user32.dll")] public static extern bool TranslateMessage(ref MSG lpMsg);
  [DllImport("user32.dll")] public static extern IntPtr DispatchMessage(ref MSG lpMsg);
  [DllImport("user32.dll")] public static extern IntPtr GetParent(IntPtr h);
  [DllImport("user32.dll")] public static extern IntPtr GetAncestor(IntPtr h, uint gaFlags);
  public delegate void WinEventProc(IntPtr hHook, uint evt, IntPtr hwnd, int idObj, int idChild, uint dwThread, uint dwTime);
  public struct MSG { public IntPtr hwnd; public uint message; public IntPtr wParam; public IntPtr lParam; public uint time; public int ptX; public int ptY; }
  static WinEventProc _wep;
  static ManualResetEvent _wev;
  static IntPtr _hk = IntPtr.Zero;
  static IntPtr _wfound;
  static List<IntPtr> _wseen;
  static int[] _wpids;
  static int _wmode;
  static void WinEventCb(IntPtr hHook, uint evt, IntPtr hwnd, int idObj, int idChild, uint dwThread, uint dwTime) {
    if (idObj != OBJID_WINDOW || hwnd == IntPtr.Zero) return;
    // Top-level test via GetAncestor(GA_ROOT) == self: the classic folder
    // picker is an OWNED dialog (owner = the Chrome window) — GetParent
    // returns the OWNER, so the old GetParent!=Zero check rejected it
    // before any filter (mode-2 hook missed the picker forever; seen live
    // 2026-08-13 via the 'Abrir'/'#32770' CREATE event that never
    // matched). GetAncestor walks the parent chain only — an owned
    // top-level still roots to itself.
    if (GetAncestor(hwnd, 2 /* GA_ROOT */) != hwnd) return;
    if (_wseen != null) lock (_wseen) { if (_wseen.Contains(hwnd)) return; }
    string t = Title(hwnd);
    if (_wmode == 1) {
      // Browser window: session-restore bubble gets dismissed inline; the
      // real window must be a chrome process with a browser-window title.
      if (t.IndexOf("Restaurar", StringComparison.OrdinalIgnoreCase) >= 0 || t.IndexOf("Restore pages", StringComparison.OrdinalIgnoreCase) >= 0) { Close(hwnd); return; }
      if (!(t.IndexOf("xtens") >= 0 || t.IndexOf("Nova guia") >= 0 || t.IndexOf("New Tab") >= 0 || t.IndexOf("Google Chrome") >= 0)) return;
      if (_wpids != null) { uint p; GetWindowThreadProcessId(hwnd, out p); if (Array.IndexOf(_wpids, (int)p) < 0) return; }
    } else {
      // Folder picker: title/class filter (port of Test-IsPickerDialog; the
      // picker can be born titled 'Abrir' and re-titled -> hook range covers
      // NAMECHANGE). Never generic browser tabs.
      if (t.Trim().Length == 0) return;
      if (t.IndexOf("Nova guia") >= 0 || t.IndexOf("New Tab") >= 0 || t.IndexOf("Sem t") >= 0 || t.IndexOf("Untitled") >= 0 || t.IndexOf("Restaurar") >= 0 || t.IndexOf("Restore pages") >= 0) return;
      string c = ClassName(hwnd);
      bool titleHit = t.IndexOf("Selecionar o diret") >= 0 || t.IndexOf("Select the direct") >= 0 || t.IndexOf("Selecionar pasta") >= 0 || t.IndexOf("Select Folder") >= 0 || t.IndexOf("escolher") >= 0 || t.IndexOf("Browse for") >= 0 || t.IndexOf("diret") >= 0 || t.IndexOf("director") >= 0;
      // mode 2: class-only. Chrome 151 re-titles the picker to
      // 'Endereço: <path>' — a NAME_CHANGE matching NO title keyword —
      // title-gated matching missed it forever and the hook timed out 8s.
      // Any NEW top-level #32770 during the wait IS the picker; the
      // seen-filter already excludes pre-existing dialogs.
      bool classHit = c == "#32770" && (_wmode == 2 || t.IndexOf("diret") >= 0 || t.IndexOf("direct") >= 0 || t.IndexOf("extens") >= 0 || t.IndexOf("Extension") >= 0 || t.IndexOf("pasta") >= 0 || t.IndexOf("folder") >= 0);
      if (!(titleHit || classHit)) return;
    }
    _wfound = hwnd;
    _wev.Set();
  }
  // Two-phase so the hook is installed BEFORE the action that creates the
  // window (Start-Process / loadUnpacked click); events queue on this
  // thread's message queue and EndWaitWindow pumps until signaled.
  public static IntPtr BeginWaitWindow(List<IntPtr> seen, int[] pids, int mode) {
    _wev = new ManualResetEvent(false);
    _wfound = IntPtr.Zero;
    _wseen = seen;
    _wpids = pids;
    _wmode = mode;
    _wep = new WinEventProc(WinEventCb);
    _hk = SetWinEventHook(EVENT_OBJECT_CREATE, EVENT_OBJECT_NAMECHANGE, IntPtr.Zero, _wep, 0, 0, WINEVENT_OUTOFCONTEXT);
    return _hk;
  }
  public static IntPtr EndWaitWindow(uint timeoutMs) {
    if (_hk == IntPtr.Zero) return IntPtr.Zero;
    var sw = System.Diagnostics.Stopwatch.StartNew();
    while (sw.ElapsedMilliseconds < timeoutMs && !_wev.WaitOne(0)) {
      MSG m;
      if (PeekMessage(out m, IntPtr.Zero, 0, 0, PM_REMOVE)) { TranslateMessage(ref m); DispatchMessage(ref m); }
      else Thread.Sleep(2); // idle yield only; never a condition poll
    }
    UnhookWinEvent(_hk);
    _hk = IntPtr.Zero;
    _wep = null;
    return _wfound;
  }
  // UIA control wait: StructureChanged event on the window subtree +
  // ManualResetEvent. The handler re-queries the tree on every change
  // (research-backed pattern; unregister in all paths). No polling.
  static AutomationElement FindFirstMatching(AutomationElement root, Func<AutomationElement, bool> match) {
    try {
      var all = root.FindAll(TreeScope.Descendants, Condition.TrueCondition);
      foreach (AutomationElement e in all) { if (match(e)) return e; }
    } catch { }
    return null;
  }
  public static AutomationElement WaitControl(IntPtr hwnd, int timeoutMs, Func<AutomationElement, bool> match) {
    AutomationElement win;
    try { win = AutomationElement.FromHandle(hwnd); } catch { return null; }
    AutomationElement f = FindFirstMatching(win, match);
    if (f != null) return f;
    // Events are primary (research pattern) but some windows never raise
    // them (e.g. a SW_HIDE-throttled renderer) — a bounded re-query every
    // 100ms is the fallback. Exits the instant either path succeeds.
    var done = new ManualResetEvent(false);
    AutomationElement res = null;
    StructureChangedEventHandler h = (s, e) => {
      AutomationElement g = FindFirstMatching(win, match);
      if (g != null) { res = g; done.Set(); }
    };
    var sw = System.Diagnostics.Stopwatch.StartNew();
    try { Automation.AddStructureChangedEventHandler(win, TreeScope.Descendants, h); } catch { }
    while (sw.ElapsedMilliseconds < timeoutMs) {
      if (done.WaitOne(100)) break;
      res = FindFirstMatching(win, match);
      if (res != null) break;
    }
    try { Automation.RemoveStructureChangedEventHandler(win, h); } catch { }
    return res;
  }
  public static void Invisible(IntPtr h) {
    int ex = GetWindowLong(h, GWL_EXSTYLE);
    SetWindowLong(h, GWL_EXSTYLE, ex | WS_EX_LAYERED);
    SetLayeredWindowAttributes(h, 0, 0, LWA_ALPHA);
  }
  // SW_HIDE: called the instant a spawned window is discovered, BEFORE the
  // DWM composites its first visible frame (~8-16ms), so the user never sees
  // a surface. The window is then alpha-0'd and shown in place: frame #1 is
  // already transparent (proven: on-screen alpha-0 renders fully to UIA).
  public static void Hide(IntPtr h) { ShowWindow(h, 0); }
  [DllImport("user32.dll")] static extern bool ShowWindow(IntPtr h, int cmd);
  public static void Move(IntPtr h, int x, int y) { RECT r; GetWindowRect(h, out r); SetWindowPos(h, IntPtr.Zero, x, y, r.Right - r.Left, r.Bottom - r.Top, SWP_NOZORDER | SWP_NOACTIVATE); }
  public static string Title(IntPtr h) { var t = new StringBuilder(256); GetWindowText(h, t, 256); return t.ToString(); }
  public static string ClassName(IntPtr h) {
    var sb = new StringBuilder(256);
    GetClassName(h, sb, 256);
    return sb.ToString();
  }
  [DllImport("user32.dll", CharSet = CharSet.Unicode)] static extern int GetClassName(IntPtr h, StringBuilder sb, int max);
  public static uint Pid(IntPtr h) { uint p; GetWindowThreadProcessId(h, out p); return p; }
  public static List<IntPtr> AllWindows() {
    var res = new List<IntPtr>();
    EnumWindows((h, l) => { res.Add(h); return true; }, IntPtr.Zero);
    return res;
  }
  public static string Navigate(IntPtr hwnd, string url) {
    AutomationElement edit = WaitControl(hwnd, 20000, e => {
      if (e.Current.ControlType != ControlType.Edit) return false;
      string n = e.Current.Name ?? "";
      return n.IndexOf("Endere", StringComparison.OrdinalIgnoreCase) >= 0 || n.IndexOf("Address", StringComparison.OrdinalIgnoreCase) >= 0;
    });
    if (edit == null) return "NO_BAR";
    var vp = edit.GetCurrentPattern(ValuePattern.Pattern) as ValuePattern;
    if (vp == null) return "NO_VALUE";
    vp.SetValue(url);
    return "SET";
  }
  public static string GetToggleState(IntPtr hwnd, string namePart, string autoId, int timeoutMs) {
    AutomationElement b = WaitControl(hwnd, timeoutMs, e => {
      if (e.Current.ControlType != ControlType.Button) return false;
      if (autoId.Length > 0) return (e.Current.AutomationId ?? "") == autoId;
      string n = e.Current.Name ?? "";
      return namePart.Length > 0 && n.IndexOf(namePart, StringComparison.OrdinalIgnoreCase) >= 0;
    });
    if (b == null) return "MISSING";
    var tg = b.GetCurrentPattern(TogglePattern.Pattern) as TogglePattern;
    if (tg == null) return "NOT_TOGGLE";
    return tg.Current.ToggleState == ToggleState.On ? "ON" : "OFF";
  }
  public static string ClickButton(IntPtr hwnd, string namePart, string autoId, int timeoutMs, bool toggleFallback) {
    // Match by automation id FIRST: the browser toolbar's page-reload button
    // is ALSO named "Recarregar"/"Reload" (view_1003) and comes before the
    // extension card's dev-reload-button in the a11y tree — a name-first
    // match reloaded the extensions PAGE instead of the extension (silent
    // no-op, proven live 2026-08-10: version never changed after "reload").
    // Ids are unique; names are not.
    AutomationElement b = WaitControl(hwnd, timeoutMs, e => {
      if (e.Current.ControlType != ControlType.Button) return false;
      if (autoId.Length > 0) return (e.Current.AutomationId ?? "") == autoId;
      string n = e.Current.Name ?? "";
      return namePart.Length > 0 && n.IndexOf(namePart, StringComparison.OrdinalIgnoreCase) >= 0;
    });
    if (b == null) return "NOT_FOUND";
    if (toggleFallback) {
      var tg = b.GetCurrentPattern(TogglePattern.Pattern) as TogglePattern;
      if (tg != null) {
        if (tg.Current.ToggleState == ToggleState.Off) { tg.Toggle(); return "TOGGLED:" + (b.Current.Name ?? ""); }
        return "ALREADY_ON:" + (b.Current.Name ?? ""); // never flip an on toggle off
      }
    }
    var inv = b.GetCurrentPattern(InvokePattern.Pattern) as InvokePattern;
    if (inv != null) { inv.Invoke(); return "CLICKED:" + (b.Current.Name ?? ""); }
    return "NO_INVOKE";
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
    // PRIMARY (only): UIA drive of the classic IFileDialog — SetValue the
    // folder edit (id 1152 / 'Pasta:'), then Invoke 'Selecionar pasta'.
    // The posted-keyboard fallback is DELETED (2026-08-13): posted WM_CHAR
    // never reaches the field (proven live) — it only re-typed the path
    // slowly. Selection is ValuePattern-aware: edits whose ValuePattern is
    // null (lazy provider, hidden file-name edit) are SKIPPED, never spun
    // on; the folder edit (1152/'Pasta'/'Folder') wins over 1148/'File
    // name'. `win` is refreshed every iteration (a stale AutomationElement
    // made FindAll throw forever, swallowed -> 25s spin, seen live).
    AutomationElement win = null;
    bool set = false;
    string lastEdit = "(none)";
    while (sw.ElapsedMilliseconds < timeoutMs) {
      try {
        if (sw.ElapsedMilliseconds > 500 && !IsStillThere(hwnd)) return "DIALOG_GONE";
        win = AutomationElement.FromHandle(hwnd);
        var edits = win.FindAll(TreeScope.Descendants,
          new PropertyCondition(AutomationElement.ControlTypeProperty, ControlType.Edit));
        if (!set) {
          AutomationElement folder = null, any = null;
          foreach (AutomationElement e in edits) {
            var id = e.Current.AutomationId ?? "";
            var nm = e.Current.Name ?? "";
            lastEdit = id + "/" + nm;
            if ((e.GetCurrentPattern(ValuePattern.Pattern) as ValuePattern) == null) continue;
            if (id == "1152" || nm.IndexOf("asta", StringComparison.OrdinalIgnoreCase) >= 0 || nm.IndexOf("Folder", StringComparison.OrdinalIgnoreCase) >= 0) { folder = e; break; }
            if (id == "1148" || nm.IndexOf("ome do arquivo", StringComparison.OrdinalIgnoreCase) >= 0 || nm.IndexOf("File name", StringComparison.OrdinalIgnoreCase) >= 0) { if (any == null) any = e; }
          }
          AutomationElement target = folder != null ? folder : any;
          if (target != null) {
            var vp = target.GetCurrentPattern(ValuePattern.Pattern) as ValuePattern;
            if (vp != null) {
              vp.SetValue(path);   // ONE call — the whole path at once
              Thread.Sleep(120);
              set = true;
            }
          }
        }
        if (set) {
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
        }
      } catch { }
      Thread.Sleep(100);
    }
    return "TIMEOUT lastEdit=" + lastEdit;
  }
  private static bool IsStillThere(IntPtr hwnd) {
    try { return AutomationElement.FromHandle(hwnd).Current.ControlType != null; }
    catch { return false; }
  }

  // ---- Chrome 151 UIA-regression workarounds (2026-08-13) ----
  // Chrome 151.0.7922.137 refuses chrome:// URLs from the command line (opens
  // the NTP instead) and dropped WebUI from UIA only while OCCLUDED (off-screen
  // or fully covered). Workarounds: reach the page by UIA-SetValue into the
  // omnibox + a POSTED Enter (works on a non-occluded window; posted keys go
  // to OUR window, never the foreground — no keybd_event anywhere), keep the
  // window on-screen but alpha-0 (invisible, not occluded), and click buttons
  // by UIA Invoke (views exposes InvokePattern for chrome:// buttons).
  [DllImport("user32.dll")] static extern bool GetClientRect(IntPtr h, out RECT r);
  [DllImport("user32.dll")] static extern bool ClientToScreen(IntPtr h, out POINT p);
  [DllImport("user32.dll")] static extern IntPtr SendMessage(IntPtr h, uint m, IntPtr w, IntPtr l);
  [DllImport("user32.dll")] static extern int GetDpiForWindow(IntPtr h);
  [DllImport("user32.dll")] static extern IntPtr GetForegroundWindow();
  [DllImport("user32.dll")] static extern bool SetForegroundWindow(IntPtr h);
  [DllImport("user32.dll")] static extern bool AttachThreadInput(uint a, uint b, bool f);
  [DllImport("kernel32.dll")] static extern uint GetCurrentThreadId();
  [StructLayout(LayoutKind.Sequential)] struct POINT { public int x, y; }
  const uint WM_LBUTTONDOWN = 0x201, WM_LBUTTONUP = 0x202;

  public static IntPtr FgWindow() { return GetForegroundWindow(); }
  // Focus bounce: detect a foreground steal and hand focus back to the
  // user's window. AttachThreadInput grants our thread the foreground
  // thread's input queue, which makes SetForegroundWindow succeed (proven
  // live 2026-08-13). LockSetForegroundWindow is NOT usable — denied with
  // ERROR_ACCESS_DENIED even while attached on Win11 24H2.
  public static bool RestoreFg(IntPtr target) {
    if (GetForegroundWindow() == target) return true;
    try {
      uint pid; uint t = GetWindowThreadProcessId(target, out pid);
      bool attached = AttachThreadInput(GetCurrentThreadId(), t, true);
      bool ok = SetForegroundWindow(target);
      if (attached) { try { AttachThreadInput(GetCurrentThreadId(), t, false); } catch { } }
      return ok;
    } catch { return false; }
  }
  public static void Resize(IntPtr h, int w, int hh) {
    RECT r; GetWindowRect(h, out r);
    SetWindowPos(h, IntPtr.Zero, -32000, -32000, w, hh, SWP_NOZORDER | SWP_NOACTIVATE);
  }
  public static void Place(IntPtr h, int x, int y) {
    SetWindowPos(h, IntPtr.Zero, x, y, 0, 0, 0x0001 | SWP_NOZORDER | SWP_NOACTIVATE); // 0x0001 = SWP_NOSIZE
  }
  public static int DpiScale(IntPtr h) { int d = GetDpiForWindow(h); return d > 0 ? d : 96; }
  public static string ClickAt(IntPtr hwnd, int xDip, int yDip) {
    try {
      int dpi = DpiScale(hwnd);
      int physX = xDip * dpi / 96, physY = yDip * dpi / 96;
      IntPtr lp = (IntPtr)(((physY & 0xFFFF) << 16) | (physX & 0xFFFF));
      SendMessage(hwnd, WM_LBUTTONDOWN, (IntPtr)1, lp);
      SendMessage(hwnd, WM_LBUTTONUP, IntPtr.Zero, lp);
      return "CLICKED:" + physX + "," + physY;
    } catch (Exception ex) { return "ERR:" + ex.Message; }
  }
}
"@ -ReferencedAssemblies UIAutomationClient,UIAutomationTypes

$result = @{ ok = $false; installed = $false; extension_id = ''; error = $null }
$newWin = [IntPtr]::Zero
$sweepSpawned = $true   # finally sweeps our orphan only if discovery failed

try {
    Write-ProgressLog "auto-install: spawning a $Browser window at $($binfo.url)"
    $before = [ExtWin]::AllWindows()
    # Folder-picker identification: the spawn can create extra 'Nova guia'
    # windows (session-restore side effects), so accept ONLY windows that
    # look like the native picker (title/class), never generic browser tabs.
    function Test-IsPickerDialog($h) {
        $t = [ExtWin]::Title($h).Trim()
        if ($t.Length -eq 0) { return $false }
        if ($t -match 'Nova guia|New Tab|Sem título|Untitled|Restaurar|Restore pages') { return $false }
        if ($t -match 'Selecionar o diret|Select the direct|Selecionar pasta|Select Folder|escolher|Browse for|diretóri|director') { return $true }
        $c = [ExtWin]::ClassName($h)
        if ($c -eq '#32770' -and $t -match 'diret|direct|extens|Extension|pasta|folder') { return $true }
        return $false
    }
    # Event-driven (research-backed 2026-08-13): install the WinEvent hook
    # BEFORE the spawn; the callback fires the instant Chrome creates the
    # window (~2-10ms, before its first DWM frame) and EndWaitWindow pumps
    # this thread's message queue — no polling, no flat sleeps. Hide() then
    # makes frame #1 already transparent.
    $chromePids = @(Get-Process $Browser -ErrorAction SilentlyContinue | ForEach-Object { $_.Id })
    [ExtWin]::BeginWaitWindow($before, [int[]]$chromePids, 1) | Out-Null
    $p = Start-Process -FilePath $exe -ArgumentList '--new-window', '--window-position=32000,32000', $binfo.url -PassThru
    $newWin = [ExtWin]::EndWaitWindow(10000)
    if ($newWin -eq [IntPtr]::Zero) {
        # Fallback (rare event miss): one bounded scan, then give up.
        $swF = [System.Diagnostics.Stopwatch]::StartNew()
        while ($swF.ElapsedMilliseconds -lt 3000 -and $newWin -eq [IntPtr]::Zero) {
            $now = [ExtWin]::AllWindows()
            foreach ($h in $now) {
                if ($before -contains $h) { continue }
                if (-not [ExtWin]::IsWindowVisible($h)) { continue }
                if ($chromePids -notcontains [ExtWin]::Pid($h)) { continue }
                $t = [ExtWin]::Title($h)
                if ($t -match 'xtens|Nova guia|New Tab|Google Chrome') { $newWin = $h; break }
            }
            Start-Sleep -Milliseconds 40
        }
        if ($newWin -eq [IntPtr]::Zero) { throw 'new browser window not found' }
    }
    $sweepSpawned = $false
    Write-ProgressLog ("auto-install: window '" + [ExtWin]::Title($newWin) + "'")
    # Chrome 151.0.7922.137 regression (2026-08-13): an off-screen, alpha-0
    # window is treated as OCCLUDED — the WebUI renderer is throttled, the
    # page never paints beyond the first frame and exposes no UIA controls
    # (39 nodes, no loadUnpacked/devMode ids). SILENT MODE: the window is
    # placed ON-SCREEN (not occluded -> renders fully: 330 UIA nodes incl.
    # all ids) but set alpha-0 (LWA_ALPHA=0): invisible to the user, live to
    # UIA. Proven end-to-end live 2026-08-13 (full install while transparent;
    # nav + picker driven with zero keyboard and zero foreground change).
    # Alpha-0 FIRST (~5-15ms after creation — before the first DWM frame;
    # nothing visible ever paints). NOT SW_HIDE: a hidden window throttles
    # the renderer (UIA events never fire; the WebUI took ~10-12s to
    # populate — seen live). Probe17 proved on-screen alpha-0 windows render
    # FULLY (330 UIA nodes) at full speed. Resize moves the window
    # off-screen as a second invisibility layer; Chrome clobbers early
    # SetWindowPos with its own initial bounds, so size/pos are re-applied
    # after the title settles below.
    [ExtWin]::Invisible($newWin)
    [ExtWin]::Resize($newWin, 1280, 800)

    # If the page did not open on the extension URL, navigate SILENTLY:
    # chrome:// URLs are dropped from the CLI, and posted WM_KEYDOWN never
    # reaches the omnibox — but UIA SetValue + a POSTED Enter navigates a
    # non-occluded window reliably. No SetFocus (activates the window ->
    # focus steal), no keybd_event (routes to the user's foreground app).
    # Retry 3x; 151 navigation is occasionally flaky.
    $t0 = [ExtWin]::Title($newWin)
    if ($t0 -notmatch 'xtens') {
        # The WinEvent hook catches the window at CREATION, before Chrome
        # sets the tab title ('Sem título'/'Nova guia' arrive ~100-300ms
        # later). Wait for a settled title so the navigation below does not
        # race the still-loading page (bounded; exits on match).
        $swT = [System.Diagnostics.Stopwatch]::StartNew()
        while ($swT.ElapsedMilliseconds -lt 5000) {
            $tt = [ExtWin]::Title($newWin)
            if ($tt.Length -gt 0 -and $tt -notmatch 'Sem t|Untitled') { break }
            Start-Sleep -Milliseconds 20
        }
        # Chrome clobbers early SetWindowPos calls with its own initial
        # bounds; re-pin size and move on-screen (alpha-0 -> invisible but
        # NOT occluded -> full-speed render, UIA events fire).
        [ExtWin]::Resize($newWin, 1280, 800)
        [ExtWin]::Place($newWin, 400, 120)
        $navOk = $false
        for ($a = 0; $a -lt 3 -and -not $navOk; $a++) {
            $nav = [ExtWin]::Navigate($newWin, $binfo.url)
            if ($nav -ne 'SET') { throw "address-bar navigation failed: $nav" }
            [ExtWin]::PostEnter($newWin)
            $sw2 = [System.Diagnostics.Stopwatch]::StartNew()
            while ($sw2.ElapsedMilliseconds -lt 10000) {
                if ([ExtWin]::Title($newWin) -match 'xtens') { $navOk = $true; break }
                Start-Sleep -Milliseconds 20
            }
        }
        if (-not $navOk) { throw "could not navigate to $($binfo.url)" }
        Write-ProgressLog ("auto-install: window navigated -> '" + [ExtWin]::Title($newWin) + "' (the tab IS the extensions page)")
    }

    # Chrome 151: pin the window to a known size so coordinate-based clicks on
    # the extensions page are deterministic. ClickButton polls until the
    # control exists — no flat render sleep (dynamic, fast).

    # Already installed? Chrome 151 hides WebUI from UIA, so check the
    # profile's Secure Preferences for an extensions.settings entry whose
    # "path" equals the staged folder (works on every Chrome version).
    # NOTE: reloads NEVER route through this window — -ReloadOnly is handled
    # by the zero-window directive branch at the top of the script.
    if (-not $Force -and (Test-ExtInProfile $binfo.udd $ExtensionDir)) {
        Write-ProgressLog 'auto-install: extension already present in profile, nothing to do'
        $result.ok = $true
        $result.installed = $true
        $result.extension_id = Get-ExtIdFromProfile $binfo.udd $ExtensionDir
    } else {
        # The folder picker appears as a new top-level window. MUST be
        # initialized to IntPtr.Zero here: an unset ($null) variable makes
        # '$dlg -ne [IntPtr]::Zero' evaluate TRUE and breaks the discovery
        # loop after its first iteration (raced the picker's 400ms opening
        # delay, seen live).
        $dlg = [IntPtr]::Zero
        # Load unpacked (dev mode must be on — the toggle is `devMode`).
        # Focus bounce: the modal picker STEALS foreground when it opens
        # (~400ms after the click) and would eat the user's keystrokes.
        # Poll at ~10ms and hand focus back to the user's window (proven
        # live 2026-08-13: 2 steals restored, final fg == user's window).
        $prevFg = [ExtWin]::FgWindow()
        $chromePids = @(Get-Process $Browser -ErrorAction SilentlyContinue | ForEach-Object { $_.Id })
        # Event-driven picker wait: hook BEFORE the click. The picker's
        # create/show/name-change events fire as soon as Chrome opens it
        # (~400ms after the click); EndWaitWindow below pumps the queue.
        [ExtWin]::BeginWaitWindow($before, $null, 2) | Out-Null
        # Dev mode gates the 'Carregar sem compactação' button: query the
        # toggle state FIRST (a 12s wait for a button that cannot exist
        # while dev mode is off was pure dead time — seen live 2026-08-13),
        # toggle only when off.
        $dev = [ExtWin]::GetToggleState($newWin, '', 'devMode', 8000)
        Write-ProgressLog "auto-install: dev mode state -> $dev"
        if ($dev -eq 'OFF') {
            $r2 = [ExtWin]::ClickButton($newWin, '', 'devMode', 8000, $true)
            Write-ProgressLog "auto-install: dev mode toggle -> $r2"
        }
        $r = [ExtWin]::ClickButton($newWin, '', 'loadUnpacked', 15000, $false)
        Write-ProgressLog "auto-install: load unpacked -> $r"
        if ($r -eq 'NOT_FOUND') {
            # Defensive fallback for Chrome builds where the WebUI is absent
            # from the UIA tree even when visible. Synthetic clicks at layout
            # coordinates on the pinned 1280x800 window (dev drawer is
            # right-aligned under the search row); a native dialog opening is
            # the success signal.
            Write-ProgressLog 'auto-install: loadUnpacked not in UIA tree -> coordinate click fallback'
            $opened = $false
            $cands = @(
                @(1180, 142), @(1090, 142), @(1140, 142), @(1040, 142), @(990, 142),
                @(940, 142), @(890, 142), @(1180, 118), @(1180, 168), @(1180, 200),
                @(60, 142), @(120, 142), @(200, 142), @(60, 118), @(60, 168)
            )
            foreach ($c in $cands) {
                if ([ExtWin]::FgWindow() -ne $prevFg) { [ExtWin]::RestoreFg($prevFg) | Out-Null }
                $cl = [ExtWin]::ClickAt($newWin, $c[0], $c[1])
                Write-ProgressLog "auto-install: coord click ($($c[0]),$($c[1])) -> $cl"
                Start-Sleep -Milliseconds 900
                $now = [ExtWin]::AllWindows()
                foreach ($h2 in $now) {
                    if ($before -contains $h2 -or $h2 -eq $newWin) { continue }
                    if (-not [ExtWin]::IsWindowVisible($h2)) { continue }
                    if (Test-IsPickerDialog $h2) {
                        # Make sure it is the FOLDER picker, not Chrome's
                        # 'Pack extension' dialog (cancel that, keep clicking).
                        $isPack = $false
                        try {
                            $w = [System.Windows.Automation.AutomationElement]::FromHandle($h2)
                            $btns = $w.FindAll([System.Windows.Automation.TreeScope]::Descendants, (New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::ControlTypeProperty, [System.Windows.Automation.ControlType]::Button)))
                            foreach ($b in $btns) {
                                if ($b.Current.Name -match 'mpacotar|Pack') { $isPack = $true; break }
                            }
                        } catch { }
                        if ($isPack) {
                            Write-ProgressLog 'auto-install: pack dialog opened - cancelling, retrying'
                            [ExtWin]::ClickButton($h2, '', 'ancel', 5000, $false) | Out-Null
                        } else {
                            $dlg = $h2; $opened = $true
                        }
                        break
                    }
                }
                if ($opened) { break }
            }
            if (-not $opened) { throw 'Load unpacked never appeared (UIA and coordinate paths both failed)' }
        }

        # The native folder dialog appears as a new top-level window.
        # (The coordinate fallback above may already have found it.)
        # Event-driven: EndWaitWindow pumps until the hook catches the
        # picker; the fallback scan below is only for a missed event.
        if ($null -eq $dlg) { $dlg = [IntPtr]::Zero }
        if ($dlg -eq [IntPtr]::Zero) { $dlg = [ExtWin]::EndWaitWindow(8000) }
        else { [ExtWin]::EndWaitWindow(0) | Out-Null }   # clean up the hook
        if ($dlg -eq [IntPtr]::Zero) {
            for ($i = 0; $i -lt 30; $i++) {
                $now = [ExtWin]::AllWindows()
                foreach ($h in $now) {
                    if ($before -contains $h -or $h -eq $newWin) { continue }
                    if (-not [ExtWin]::IsWindowVisible($h)) { continue }
                    if (Test-IsPickerDialog $h) { $dlg = $h; break }
                }
                if ($dlg -ne [IntPtr]::Zero) { break }
                Start-Sleep -Milliseconds 100
            }
        }
        if ($dlg -eq [IntPtr]::Zero) { throw 'folder picker dialog never appeared' }
        Write-ProgressLog ("auto-install: dialog '" + [ExtWin]::Title($dlg) + "'")
        # Alpha-0 the dialog IMMEDIATELY — the user must never see it. The
        # bounce below runs while it is already invisible (proven live:
        # alpha-0 keeps the UIA tree intact and fully drivable).
        [ExtWin]::Invisible($dlg)
        # Focus bounce (steal-guard): the picker activates ~30-100ms after
        # creation and would eat the user's keystrokes. Restore the user's
        # foreground whenever the foreground is ANY chrome process (the
        # picker runs in a NEW chrome process NOT in the pre-spawn pid list
        # — the old pid-membership check silently missed it and the picker
        # kept focus for the whole drive; user's typing landed in the path
        # field, seen live 2026-08-13). Bounded 1.5s; exits fast on steal.
        # Early-exit: once the foreground is back on the user's window (or
        # never left it), the steal is over — no 1.5s dead wait (~1.4s saved).
        $swB = [System.Diagnostics.Stopwatch]::StartNew()
        while ($swB.ElapsedMilliseconds -lt 1500) {
            $fgB = [ExtWin]::FgWindow()
            if ($fgB -ne $prevFg) {
                $fgProc = Get-Process -Id ([ExtWin]::Pid($fgB)) -ErrorAction SilentlyContinue
                if ($fgB -eq $dlg -or ($fgProc -and $fgProc.ProcessName -eq $Browser)) {
                    [ExtWin]::RestoreFg($prevFg) | Out-Null
                }
            } else { break }
            Start-Sleep -Milliseconds 4
        }

        $dr = [ExtWin]::DrivePicker($dlg, $ExtensionDir, 25000)
        Write-ProgressLog "auto-install: picker -> $dr"

        # Verify via the profile: 151 hides the card from UIA, but the install
        # writes an extensions.settings entry into Secure Preferences.
        $found = $false
        for ($i = 0; $i -lt 50; $i++) {
            if (Test-ExtInProfile $binfo.udd $ExtensionDir) { $found = $true; break }
            Start-Sleep -Milliseconds 200
        }
        if (-not $found) { throw "extension not found in profile after install (picker: $dr)" }
        $result.extension_id = Get-ExtIdFromProfile $binfo.udd $ExtensionDir
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
    # Sweep stray folder pickers: the picker is a separate top-level window
    # and survives the extensions-window close (seen live: 3 orphans).
    foreach ($h in [ExtWin]::AllWindows()) {
        $t = [ExtWin]::Title($h)
        if ($t -match 'Selecionar o dir|Select the direct') {
            try { [ExtWin]::Close($h) } catch { }
        }
    }
    # Error path only: the window was spawned but discovery never set
    # $newWin (or the run failed before it). Close any chrome browser
    # window we created (not in $before) so a failed run never leaves a
    # visible stray window. NEVER on success/after-discovery — that would
    # kill a tab the user opened during the run (seen live: user's own new
    # tab was closed by the old unconditional sweep).
    if ($sweepSpawned) {
        foreach ($h in [ExtWin]::AllWindows()) {
            if ($before -contains $h) { continue }
            $t = [ExtWin]::Title($h)
            if ($t -match 'Nova guia|New Tab|xtens') {
                $proc = Get-Process -Id ([ExtWin]::Pid($h)) -ErrorAction SilentlyContinue
                if ($proc -and $proc.ProcessName -eq $Browser) {
                    Write-ProgressLog 'auto-install: closing spawned-but-undiscovered window'
                    try { [ExtWin]::Close($h) } catch { }
                }
            }
        }
    }
    Write-Output ($result | ConvertTo-Json -Compress)
}
