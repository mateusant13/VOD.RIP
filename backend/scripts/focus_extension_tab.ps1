<#
focus_extension_tab.ps1 — switch an already-open chrome://extensions tab to the
front instead of spawning a duplicate. Used by the Cookie Bridge "Open
extensions" button.

Method: UI Automation. Chrome exposes its tab strip as TabItem elements whose
Name is the (localized) page title, and TabItem supports SelectionItemPattern,
so we can select the tab and raise its window — no CDP, no remote debugging.

Exit codes:
  0 — an Extensions tab was found, selected, and its window raised
  1 — no Extensions tab found (caller should open a new tab)
#>
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes

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
$procNames = @{}
foreach ($pn in (Get-Process -ErrorAction SilentlyContinue | Select-Object -ExpandProperty ProcessName -Unique)) {
  if ($browsers -contains $pn.ToLower()) { $procNames[$pn.ToLower()] = $true }
}

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
      if (-not $procNames.ContainsKey($procName)) { continue }
      $name = ($t.Current.Name -as [string]).Trim()
      if ($names -contains $name) { $match = $t; break }
    }
  }
} catch {
  exit 1
}

if (-not $match) { exit 1 }

try {
  $sel = $match.GetCurrentPattern([System.Windows.Automation.SelectionItemPattern]::Pattern)
  $sel.Select()
} catch {
  # Some Chromium builds expose tabs without a working selection pattern —
  # treat as not-found so the caller opens a fresh tab.
  exit 1
}

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
