<#
.SYNOPSIS
  Read-only state report for the VOD.RIP workspace.

.DESCRIPTION
  One screen, machine-readable, with population and window on every number.
  This script NEVER writes: it does not clean, move, delete, merge, stash, install
  or prune anything. The only side effect it permits is reading.

  Every field is one of:
    * a measured value, printed with its population (n) and its window
    * an explicit `not_measured` / `unknown` WITH a reason

  A value that could not be established is never printed as 0, `-`, or omitted.

  EXIT CODES (a gate, distinguishable from "could not measure"):
    0  all gates measured and passing
    2  GATE FAILURE  - at least one gate was measured and is bad
    3  NOT MEASURED  - no gate failed, but at least one gate could not be measured
    1  unexpected internal error

.EXAMPLE
  .\scripts\workspace-status.ps1
  Read the current checkout (the worktree this script lives in).

.EXAMPLE
  .\scripts\workspace-status.ps1 -Repo 'C:\path\to\VOD.RIP' -Json
  Observe another checkout from here, as JSON, for an agent to parse.

.NOTES
  Honesty rules enforced here:
    * `not_measured` + reason, never 0
    * a `tsc`/`vitest` result reported while node_modules\.bin is missing is a
      FAILURE, not a pass - the deps gate says so out loud
    * an unmerged branch is never classified `done`
    * a registered worktree whose directory is gone is a problem, never skipped
    * two lanes owning the same file are flagged loudly
#>
[CmdletBinding()]
param(
  # Checkout to observe. Defaults to the repository this script lives in.
  [string]$Repo,

  # Emit the same report as JSON on stdout.
  [switch]$Json,

  # Skip the (slowest) byte-footprint walk of every worktree.
  [switch]$Fast,

  # Ownership manifest (tab separated). Defaults to <repo>\docs\lane-ownership.tsv
  [string]$LanesFile,

  # Liveness probe output. Defaults to <repo>\tmp\liveness.jsonl
  [string]$ProbeFile,

  # Global time budget for the per-worktree byte footprint walk, in seconds.
  # Worktrees not reached inside the budget print `not_measured` + reason.
  [int]$FootprintBudgetSec = 25,

  # How many worktrees to probe at once. Kept modest: the governor on this box
  # (Steady Watcher) shares the machine with everything else.
  [int]$ThrottleLimit = 6,

  # Gate: registered worktrees above this count are a collision/over-cap FAIL.
  [int]$MaxWorktrees = 12,

  # A lane dispatched longer ago than this with no commit since counts as quiet.
  [int]$StaleHours = 24,

  # How many trailing lines of the probe file to summarise.
  [int]$ProbeTail = 2000,

  # Gate: node_modules below this file count is treated as a partial install.
  [int]$DepsMinFiles = 1000,

  # How many recent merges to list.
  [int]$Merges = 5
)

# Read-only by policy. A single unreadable worktree must not abort the report.
$ErrorActionPreference = 'Continue'
# Do not let git refresh the index just because we looked at it.
$env:GIT_OPTIONAL_LOCKS = '0'

$script:VERDICT_OK = 'ok'
$script:VERDICT_FAIL = 'FAIL'
$script:VERDICT_WARN = 'WARN'
$script:VERDICT_NM = 'not_measured'

# ---------------------------------------------------------------- primitives

function Invoke-GitLines {
  <#  Run git, never throw. Returns exit code + stdout lines.  #>
  param(
    [Parameter(Mandatory = $true)][string]$GitDir,
    [Parameter(Mandatory = $true)][string[]]$GitArgs
  )
  $out = & git -C $GitDir @GitArgs 2> $null
  $code = $LASTEXITCODE
  $lines = @()
  foreach ($l in @($out)) { if ($null -ne $l -and "$l" -ne '') { $lines += "$l" } }
  return [pscustomobject]@{ Exit = $code; Lines = $lines }
}

function Format-MB {
  param([double]$Bytes)
  if ($Bytes -lt 0) { return 'unknown' }
  if ($Bytes -lt 1MB) { return ('{0} B' -f [int]$Bytes) }
  if ($Bytes -lt 1GB) { return ('{0} MB' -f [math]::Round($Bytes / 1MB, 1)) }
  return ('{0} GB' -f [math]::Round($Bytes / 1GB, 2))
}

function New-Field {
  <#  The one constructor for a reported value. A field is a measurement or it
      is an explicit refusal. There is no third option.  #>
  param(
    [Parameter(Mandatory = $true)][string]$Name,
    $Value,
    [string]$Verdict = $script:VERDICT_OK,
    [string]$N = '',
    [string]$Window = '',
    [string]$Reason = ''
  )
  if ($null -eq $Value) {
    $Value = $script:VERDICT_NM
    if (-not $Reason) { $Reason = 'not established' }
    if ($Verdict -eq $script:VERDICT_OK) { $Verdict = $script:VERDICT_NM }
  }
  return [pscustomobject]@{
    name = $Name; value = $Value; verdict = $Verdict
    n = $N; window = $Window; reason = $Reason
  }
}

# ------------------------------------------------------------------ globbing

function ConvertTo-GlobRegex {
  <#  gitignore-ish glob -> anchored regex. `*` stops at `/`, `**` does not.  #>
  param([Parameter(Mandatory = $true)][string]$Glob)
  $sentinel = [string][char]1   # cannot occur in Regex.Escape output
  $e = [regex]::Escape($Glob)
  $e = [regex]::Replace($e, '\\\*\\\*', $sentinel)
  $e = [regex]::Replace($e, '\\\*', '[^/]*')
  $e = [regex]::Replace($e, '\\\?', '[^/]')
  $e = $e.Replace($sentinel, '.*')
  return $e
}

function Test-SegmentOverlap {
  param([string]$A, [string]$B)
  if ($A -eq $B) { return $true }
  $aWild = $A -match '[\*\?]'
  $bWild = $B -match '[\*\?]'
  if (-not $aWild -and -not $bWild) { return $false }
  if ($aWild -and $bWild) { return $true }   # conservative: assume reachable
  $pat = if ($aWild) { ConvertTo-GlobRegex $A } else { ConvertTo-GlobRegex $B }
  $lit = if ($aWild) { $B } else { $A }
  return ($lit -match ('^' + $pat + '$'))
}

function Test-GlobOverlap {
  <#  True when SOME path could match both globs.
      Deliberately conservative: if one glob is a strict prefix of the other in
      path segments (`scripts` vs `scripts/foo.ps1`), we assume overlap. A
      false positive costs one message; a missed overlap costs a lane's work.  #>
  param([string]$A, [string]$B)
  $a = ($A -replace '\\', '/' -replace '^\./', '' -replace '/$', '').Trim()
  $b = ($B -replace '\\', '/' -replace '^\./', '' -replace '/$', '').Trim()
  if (-not $a -or -not $b) { return $false }
  $as = @($a -split '/')
  $bs = @($b -split '/')
  $n = [math]::Max($as.Count, $bs.Count)
  for ($i = 0; $i -lt $n; $i++) {
    $aseg = if ($i -lt $as.Count) { $as[$i] } else { $null }
    $bseg = if ($i -lt $bs.Count) { $bs[$i] } else { $null }
    if ($null -eq $aseg) { continue }   # shorter glob exhausted: assume reachable
    if ($null -eq $bseg) { continue }
    if (-not (Test-SegmentOverlap $aseg $bseg)) { return $false }
  }
  return $true
}

function Get-GlobCollision {
  <#  none | possible_collision | collision
      `collision` is PROVED (a literal path sits inside the other glob).
      `possible_collision` is conservative: both sides are wildcards.  #>
  param([string]$A, [string]$B)
  if (-not (Test-GlobOverlap $A $B)) { return 'none' }
  $aWild = $A -match '[\*\?]'
  $bWild = $B -match '[\*\?]'
  if ($aWild -and $bWild) {
    $aPat = ConvertTo-GlobRegex $A
    $bPat = ConvertTo-GlobRegex $B
    $an = $A -replace '\\', '/'
    $bn = $B -replace '\\', '/'
    if (($bn -match ('^' + $aPat + '$')) -or ($an -match ('^' + $bPat + '$'))) { return 'collision' }
    return 'possible_collision'
  }
  return 'collision'
}

# --------------------------------------------------------------------- deps
# (Get-DepState is defined below Measure-TreeBytes, which it uses.)

# -------------------------------------------------------------------- probe

function Get-ProbeSummary {
  <#  Distribution of the live probe, never a single sample.
      Absent file => not_measured with a reason. NEVER 0.  #>
  param(
    [Parameter(Mandatory = $true)][string]$Path,
    [int]$Tail = 2000
  )
  if (-not (Test-Path -LiteralPath $Path)) {
    return [pscustomobject]@{
      measured = $false; reason = ('probe file absent: ' + $Path)
      n = 'not_measured'; window = ''; p50 = 'not_measured'; p95 = 'not_measured'; max = 'not_measured'
      failures = 'not_measured'; ok = 'not_measured'; lines = 'not_measured'; unparsable = 'not_measured'
      last_ts = ''; per_check = @()
    }
  }
  $all = @()
  try { $all = @(Get-Content -LiteralPath $Path -ErrorAction SilentlyContinue) } catch { $all = @() }
  $linesTotal = $all.Count
  if ($linesTotal -eq 0) {
    return [pscustomobject]@{
      measured = $false; reason = 'probe file exists but is empty (0 lines) - writer has not emitted yet'
      n = 'not_measured'; window = ''; p50 = 'not_measured'; p95 = 'not_measured'; max = 'not_measured'
      failures = 'not_measured'; ok = 'not_measured'; lines = 0; unparsable = 0
      last_ts = ''; per_check = @()
    }
  }
  $take = if ($Tail -gt 0 -and $linesTotal -gt $Tail) { $all[($linesTotal - $Tail)..($linesTotal - 1)] } else { $all }

  $samples = New-Object System.Collections.Generic.List[object]
  $unparsable = 0
  foreach ($ln in $take) {
    if (-not $ln) { continue }
    $s = "$ln"
    if ($s.Trim() -eq '') { continue }
    try { $o = $s | ConvertFrom-Json -ErrorAction Stop } catch { $unparsable++; continue }
    if ($null -eq $o) { $unparsable++; continue }
    $ev = $o.PSObject.Properties['event']
    if (-not $ev) { continue }
    $evName = "$($ev.Value)"
    if ($evName -ne 'health' -and $evName -ne 'preview') { continue }
    $msProp = $o.PSObject.Properties['ms']
    if (-not $msProp) { continue }
    $ms = 0.0
    if (-not [double]::TryParse("$($msProp.Value)", [ref]$ms)) { continue }
    $ok = $true
    $okProp = $o.PSObject.Properties['ok']
    if ($okProp -and ($null -ne $okProp.Value)) { $ok = [bool]$okProp.Value }
    $ts = ''
    $tsProp = $o.PSObject.Properties['ts']
    if ($tsProp) { $ts = "$($tsProp.Value)" }
    $samples.Add([pscustomobject]@{ event = $evName; ms = $ms; ok = $ok; ts = $ts })
  }

  if ($samples.Count -eq 0) {
    return [pscustomobject]@{
      measured = $false
      reason = ('no health/preview sample with a numeric `ms` in the tail ({0} lines scanned, {1} unparsable)' -f $take.Count, $unparsable)
      n = 'not_measured'; window = ''; p50 = 'not_measured'; p95 = 'not_measured'; max = 'not_measured'
      failures = 'not_measured'; ok = 'not_measured'; lines = $linesTotal; unparsable = $unparsable
      last_ts = ''; per_check = @()
    }
  }

  $ms = @($samples | ForEach-Object { $_.ms } | Sort-Object)
  $n = $ms.Count
  $fails = @($samples | Where-Object { -not $_.ok })
  $first = ($samples | Where-Object { $_.ts } | Select-Object -First 1)
  $last = ($samples | Where-Object { $_.ts } | Select-Object -Last 1)
  $window = if ($first -and $last) { "$($first.ts) .. $($last.ts)" } else { 'unknown (samples carry no ts)' }

  # nearest-rank percentiles
  function Get-Q([double[]]$A, [double]$Q) {
    if ($A.Count -eq 0) { return $null }
    $idx = [math]::Ceiling($Q * $A.Count) - 1
    if ($idx -lt 0) { $idx = 0 }
    if ($idx -ge $A.Count) { $idx = $A.Count - 1 }
    return [double]$A[$idx]
  }
  $p50 = Get-Q $ms 0.50
  $p95 = Get-Q $ms 0.95

  $perCheck = @()
  foreach ($grp in ($samples | Group-Object event)) {
    $g = @($grp.Group | ForEach-Object { $_.ms } | Sort-Object)
    $gf = @($grp.Group | Where-Object { -not $_.ok })
    $perCheck += [pscustomobject]@{
      check = $grp.Name; n = $g.Count
      p50 = [math]::Round((Get-Q $g 0.50), 1)
      p95 = [math]::Round((Get-Q $g 0.95), 1)
      max = [math]::Round(($g | Select-Object -Last 1), 1)
      failures = $gf.Count
    }
  }

  $reason = ''
  if ($n -lt 2) { $reason = 'n=1 - a single sample is not a distribution' }

  return [pscustomobject]@{
    measured = $true; reason = $reason
    n = $n; window = $window
    p50 = [math]::Round($p50, 1); p95 = [math]::Round($p95, 1)
    max = [math]::Round(($ms | Select-Object -Last 1), 1)
    failures = $fails.Count; ok = ($n - $fails.Count)
    lines = $linesTotal; unparsable = $unparsable
    tail_scanned = $take.Count; last_ts = $last.ts
    per_check = $perCheck
  }
}

# -------------------------------------------------------------------- lanes

function Read-LaneManifest {
  param([string]$Path)
  if (-not (Test-Path -LiteralPath $Path)) {
    return [pscustomobject]@{ present = $false; reason = 'no manifest at ' + $Path; lanes = @() }
  }
  $lanes = @()
  try {
    $rows = @(Import-Csv -LiteralPath $Path -Delimiter "`t" -ErrorAction Stop)
  } catch {
    return [pscustomobject]@{ present = $false; reason = 'manifest unreadable: ' + $_.Exception.Message; lanes = @() }
  }
  foreach ($r in $rows) {
    $id = ''
    foreach ($k in $r.PSObject.Properties.Name) { if ($k -and $k.Trim().ToLower() -eq 'lane_id') { $id = $r.$k } }
    if (-not $id) { continue }
    $f = @{}
    foreach ($k in $r.PSObject.Properties.Name) { $f[$k.Trim().ToLower()] = $r.$k }
    $owns = @()
    if ($f['owns']) { $owns = @("$($f['owns'])" -split ',' | ForEach-Object { $_.Trim() } | Where-Object { $_ }) }
    $dt = [datetime]::MinValue
    $parsed = $false
    if ($f['dispatched_at']) {
      $parsed = [datetime]::TryParse("$($f['dispatched_at'])", [ref]$dt)
    }
    $lanes += [pscustomobject]@{
      lane_id = "$id"
      scope = "$($f['scope'])"
      owns = $owns
      branch = "$($f['branch'])"
      worktree = "$($f['worktree'])"
      state = "$($f['state'])"
      dispatched_at = if ($parsed) { $dt } else { [datetime]::MinValue }
      dispatched_raw = "$($f['dispatched_at'])"
      contact = "$($f['contact'])"
    }
  }
  return [pscustomobject]@{ present = $true; reason = ''; lanes = $lanes }
}

function Get-LaneQuiet {
  <#  Pure. Given the world, decide which lanes have gone quiet.
      A stale ownership record nobody prunes is worse than none, so this asks:
        worktree gone / branch gone / dispatched long ago with no commit since.  #>
  param(
    [Parameter(Mandatory = $true)]$Lanes,
    [string[]]$ExistingWorktrees = @(),
    [string[]]$ExistingBranches = @(),
    [hashtable]$BranchLastCommit = @{},
    [datetime]$Now = [datetime]::UtcNow,
    [int]$StaleHours = 24
  )
  $out = @()
  foreach ($l in $Lanes) {
    $quiet = @()
    if ($l.worktree) {
      $norm = $l.worktree -replace '/', '\'
      $hit = $false
      foreach ($w in $ExistingWorktrees) {
        if ((($w -replace '/', '\').TrimEnd('\')) -eq $norm.TrimEnd('\')) { $hit = $true; break }
      }
      if (-not $hit) { $quiet += 'worktree_gone' }
    }
    if ($l.branch) {
      $b = $l.branch -replace '^refs/heads/', ''
      if ($ExistingBranches.Count -gt 0 -and ($ExistingBranches -notcontains $b)) { $quiet += 'branch_gone' }
    }
    $ageH = $null
    if ($l.dispatched_at -ne [datetime]::MinValue) {
      $ageH = [math]::Round(($Now - $l.dispatched_at.ToUniversalTime()).TotalHours, 1)
      if ($ageH -gt $StaleHours) {
        $hasCommit = $false
        $b = $l.branch -replace '^refs/heads/', ''
        if ($BranchLastCommit.ContainsKey($b)) {
          $lc = $BranchLastCommit[$b]
          if ($lc -is [datetime] -and $lc -gt $l.dispatched_at) { $hasCommit = $true }
        }
        if (-not $hasCommit) { $quiet += ('stale_{0}h_no_commit' -f [int]$ageH) }
      }
    } else {
      if ($l.dispatched_raw) { $quiet += 'dispatched_at_unparsable' }
    }
    $out += [pscustomobject]@{
      lane = $l.lane_id; scope = $l.scope; owns = $l.owns; branch = $l.branch
      worktree = $l.worktree; state = $l.state
      dispatched_at = $l.dispatched_raw
      age_hours = if ($null -eq $ageH) { 'unknown' } else { $ageH }
      quiet = $quiet
      verdict = if ($quiet.Count -eq 0) { 'active' } else { 'QUIET' }
    }
  }
  return $out
}

function Get-LaneCollisions {
  <#  Pure. Proved vs possible overlap between any two lanes' owned globs.  #>
  param([Parameter(Mandatory = $true)]$Lanes)
  $hits = @()
  for ($i = 0; $i -lt $Lanes.Count; $i++) {
    for ($j = $i + 1; $j -lt $Lanes.Count; $j++) {
      $a = $Lanes[$i]; $b = $Lanes[$j]
      if ($a.lane_id -eq $b.lane_id) { continue }
      foreach ($ga in $a.owns) {
        foreach ($gb in $b.owns) {
          $kind = Get-GlobCollision $ga $gb
          if ($kind -ne 'none') {
            $hits += [pscustomobject]@{
              lane_a = $a.lane_id; glob_a = $ga
              lane_b = $b.lane_id; glob_b = $gb
              kind = $kind
            }
          }
        }
      }
    }
  }
  return $hits
}

# ---------------------------------------------------------------- worktrees

function Resolve-WorktreeState {
  <#  wip | done | unsure | archive | unmanaged

      HARD RULE, applied to the manifest AND to the path: an UNMERGED head is
      never `done`. A lane that has not reached main has not delivered, whatever
      its directory is called and whatever the manifest claims.  #>
  param(
    [string]$Path,
    [string]$ManifestState = '',
    # 'yes' = proven merged into main, 'no' = proven unmerged, 'unknown' = not established
    [string]$Merged = 'unknown'
  )
  $merged = "$Merged".Trim().ToLower()
  $m = ''
  if ($ManifestState) { $m = "$ManifestState".Trim().ToLower() }
  if (-not $m) {
    $p = ($Path -replace '/', '\')
    foreach ($seg in @('wip', 'done', 'unsure', 'archive')) {
      if ($p -match ('(?i)(^|\\)' + $seg + '(\\|$)')) { $m = $seg; break }
    }
  }
  if (-not $m) { return 'unmanaged' }
  if ($m -eq 'done' -and $merged -ne 'yes') { return 'wip' }
  return $m
}

function Get-LaneStateOverrides {
  <#  Map worktree path -> manifest state, so a registered worktree with a
      matching lane entry is classified from the manifest, not from its name.  #>
  param($Lanes)
  $m = @{}
  if (-not $Lanes) { return $m }
  foreach ($l in $Lanes) {
    if ($l.worktree -and $l.state) {
      $m[($l.worktree -replace '/', '\').TrimEnd('\')] = "$($l.state)".Trim().ToLower()
    }
  }
  return $m
}

function Measure-TreeBytes {
  <#  Bounded recursive byte walk. Skips the shared node_modules junction: it
      points at the MAIN install, so counting it per worktree would multiply
      one tree by 48 and report a lie.  #>
  param(
    [Parameter(Mandatory = $true)][string]$Path,
    [datetime]$Deadline = [datetime]::MaxValue,
    [string[]]$Skip = @('node_modules', '.git', 'dist', '.vite', '__pycache__')
  )
  $bytes = 0L
  $files = 0
  $stack = New-Object System.Collections.Generic.Stack[string]
  $stack.Push($Path)
  while ($stack.Count -gt 0) {
    if ([datetime]::UtcNow -gt $Deadline) {
      return [pscustomobject]@{ bytes = $bytes; files = $files; truncated = $true }
    }
    $dir = $stack.Pop()
    $items = @()
    try { $items = @(Get-ChildItem -LiteralPath $dir -Force -ErrorAction SilentlyContinue) } catch { continue }
    foreach ($it in $items) {
      if ($it.PSIsContainer) {
        if ($Skip -contains $it.Name) { continue }
        if ($it.LinkType) { continue }   # junction/symlink: not this worktree's bytes
        $stack.Push($it.FullName)
      } else {
        try { $bytes += $it.Length; $files++ } catch { }
      }
    }
  }
  return [pscustomobject]@{ bytes = $bytes; files = $files; truncated = $false }
}

function Get-DepState {
  <#  Dependency install state. `.bin` absent == FAILURE: a `tsc`/`vitest`
      result produced without it is not a pass, it is a broken measurement.

      The `.bin` check needs no directory walk and is the load-bearing one.
      The file count is reported with its own budget, and is `not_measured`
      (never 0) if the walk runs out of time.  #>
  param(
    [Parameter(Mandatory = $true)][string]$Root,
    [int]$MinFiles = 1000,
    [int]$BudgetSec = 20
  )
  $nm = Join-Path $Root 'node_modules'
  if (-not (Test-Path -LiteralPath $nm)) {
    return [pscustomobject]@{
      state = 'absent'; files = 'not_measured'; bin = $false
      reason = 'node_modules does not exist'; timed_out = $false
    }
  }
  $bin = Join-Path $nm '.bin'
  $hasBin = Test-Path -LiteralPath $bin

  $files = 'not_measured'
  $bytes = 'not_measured'
  $timedOut = $false
  try {
    $m = Measure-TreeBytes -Path $nm -Deadline ([datetime]::UtcNow.AddSeconds($BudgetSec)) -Skip @('.cache')
    if ($m.truncated) {
      $timedOut = $true
    } else {
      $files = $m.files
      $bytes = $m.bytes
    }
  } catch {
    $timedOut = $true
  }

  if (-not $hasBin) {
    return [pscustomobject]@{
      state = 'incomplete'; files = $files; bin = $false; bytes = $bytes; timed_out = $timedOut
      reason = 'node_modules\.bin is ABSENT - tsc/vitest cannot run; any green reported now is a false green'
    }
  }
  if ($timedOut) {
    return [pscustomobject]@{
      state = 'unknown'; files = $files; bin = $true; bytes = $bytes; timed_out = $true
      reason = ("node_modules\.bin is present, but the file count exceeded the {0}s budget and is not_measured" -f $BudgetSec)
    }
  }
  if ($files -lt $MinFiles) {
    return [pscustomobject]@{
      state = 'incomplete'; files = $files; bin = $true; bytes = $bytes; timed_out = $false
      reason = ('only {0} files under node_modules, below floor {1} - partial or pruned install' -f $files, $MinFiles)
    }
  }
  return [pscustomobject]@{
    state = 'complete'; files = $files; bin = $true; bytes = $bytes; timed_out = $false; reason = ''
  }
}

function Test-Junction {
  <#  A node_modules JUNCTION is a hard stop for `git worktree move` and
      `git worktree remove` - following it destroys the shared install.  #>
  param([string]$Path)
  if (-not $Path) { return $false }
  try {
    $i = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
    if ($i.LinkType) { return $true }
    return ($null -ne $i.Target)
  } catch { return $false }
}

# --------------------------------------------------------------------- gates

function Get-GateVerdict {
  <#  Pure. Turns a set of gate results into the exit code.
      0 = all pass, 2 = at least one measured FAIL, 3 = nothing failed but
      something could not be measured. 2 and 3 are deliberately different.  #>
  param([Parameter(Mandatory = $true)]$Gates)
  $fail = @($Gates | Where-Object { $_.verdict -eq $script:VERDICT_FAIL })
  $nm = @($Gates | Where-Object { $_.verdict -eq $script:VERDICT_NM })
  if ($fail.Count -gt 0) { return 2 }
  if ($nm.Count -gt 0) { return 3 }
  return 0
}

function New-Gate {
  param(
    [Parameter(Mandatory = $true)][string]$Name,
    [Parameter(Mandatory = $true)][string]$Verdict,
    [string]$Detail = '',
    [string]$Reason = ''
  )
  return [pscustomobject]@{ name = $Name; verdict = $Verdict; detail = $Detail; reason = $Reason }
}

function Get-WorktreeVerdict {
  <#  Pure. Decides the worktree gate, and names EVERY problem rather than the
      first one - a gate that reports one fault at a time makes the owner run
      it repeatedly.
      A registered worktree whose directory is GONE is a problem, never a
      silent skip: the registration is a claim about the disk, and a claim that
      no longer matches the disk is exactly the kind of drift that costs work.  #>
  param(
    [int]$NRegistered = 0,
    [int]$MaxWorktrees = 12,
    [string[]]$MissingDir = @(),
    [int]$CollisionCount = 0
  )
  $why = New-Object System.Collections.Generic.List[string]
  if ($NRegistered -gt $MaxWorktrees) {
    $why.Add(('{0} registered worktrees is over the cap of {1}; they are not being released' -f $NRegistered, $MaxWorktrees)) | Out-Null
  }
  if ($MissingDir -and $MissingDir.Count -gt 0) {
    $why.Add(('{0} registered worktree(s) whose directory is gone: {1}' -f $MissingDir.Count, ($MissingDir -join ', '))) | Out-Null
  }
  if ($CollisionCount -gt 0) {
    $why.Add(('{0} ownership collision(s) between live lanes' -f $CollisionCount)) | Out-Null
  }
  if ($why.Count -eq 0) { return [pscustomobject]@{ verdict = $script:VERDICT_OK; reason = '' } }
  return [pscustomobject]@{ verdict = $script:VERDICT_FAIL; reason = ($why -join '; ') }
}

# =====================================================================  main

function Invoke-WorkspaceStatus {
  $lines = New-Object System.Collections.Generic.List[string]
  $add = { param([string]$s = '') $lines.Add($s) | Out-Null }

  $stamp = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')

  # --- resolve the checkout -------------------------------------------------
  if (-not $Repo) {
    $Repo = (git rev-parse --show-toplevel 2> $null | Select-Object -First 1)
    if ($Repo) { $Repo = "$Repo".Trim() }
  }
  if (-not $Repo -or -not (Test-Path -LiteralPath $Repo)) {
    Write-Error 'cannot resolve a repository to observe (pass -Repo <path>)'
    $script:WS_EXIT = 1
    return
  }
  if (-not $LanesFile) { $LanesFile = Join-Path $Repo 'docs\lane-ownership.tsv' }
  if (-not $ProbeFile) { $ProbeFile = Join-Path $Repo 'tmp\liveness.jsonl' }

  $model = [ordered]@{
    tool = 'workspace-status'
    read_only = $true
    generated_at = $stamp
    repo = $Repo
    git = $null; worktrees = $null; deps = $null
    disk = $null; probe = $null; lanes = $null; gates = $null
  }

  # ==================================================================  GIT
  $branch = 'unknown'
  $sha = 'unknown'
  $gBranch = Invoke-GitLines $Repo @('rev-parse', '--abbrev-ref', 'HEAD')
  if ($gBranch.Exit -eq 0 -and $gBranch.Lines.Count) { $branch = $gBranch.Lines[0] }
  $gSha = Invoke-GitLines $Repo @('rev-parse', '--short', 'HEAD')
  if ($gSha.Exit -eq 0 -and $gSha.Lines.Count) { $sha = $gSha.Lines[0] }

  $ahead = $null; $behind = $null
  $gAB = Invoke-GitLines $Repo @('rev-list', '--left-right', '--count', 'origin/main...HEAD')
  if ($gAB.Exit -eq 0 -and $gAB.Lines.Count) {
    $parts = $gAB.Lines[0] -split '\s+'
    if ($parts.Count -ge 2) {
      $behind = [int]$parts[0]
      $ahead = [int]$parts[1]
    }
  }

  # dirty, classified by owner. The watcher's file is the governor's, not ours.
  $watcherFiles = @('.steady-watcher.json')
  $dirty = @()
  $gDirty = Invoke-GitLines $Repo @('status', '--porcelain')
  foreach ($l in $gDirty.Lines) {
    if ($l.Length -lt 4) { continue }
    $xy = $l.Substring(0, 2)
    $path = $l.Substring(3).Trim()
    if ($path -match ' -> ') { $path = ($path -split ' -> ')[-1].Trim() }
    $owner = 'agent'
    foreach ($w in $watcherFiles) {
      if (($path -replace '\\', '/') -eq $w) { $owner = 'watcher' }
    }
    $dirty += [pscustomobject]@{ path = $path; xy = $xy; owner = $owner }
  }
  $dirtyAgent = @($dirty | Where-Object { $_.owner -eq 'agent' })
  $dirtyWatcher = @($dirty | Where-Object { $_.owner -eq 'watcher' })

  $stashList = Invoke-GitLines $Repo @('stash', 'list')
  $stashN = if ($stashList.Exit -eq 0) { $stashList.Lines.Count } else { $null }

  $mergeLog = @()
  $gMerges = Invoke-GitLines $Repo @('log', '--merges', '--date=short', '--pretty=format:%h %ad %s', "-n", "$Merges", 'HEAD')
  if ($gMerges.Exit -eq 0) { $mergeLog = $gMerges.Lines }

  $model['git'] = [ordered]@{
    branch = $branch; sha = $sha
    ahead_of_origin_main = $ahead
    behind_origin_main = $behind
    dirty_total = $dirty.Count
    dirty_agent_owned = $dirtyAgent.Count
    dirty_watcher_owned = $dirtyWatcher.Count
    dirty_files = @($dirty | ForEach-Object { [ordered]@{ path = $_.path; xy = $_.xy; owner = $_.owner } })
    stash_count = $stashN
    stash_entries = @($stashList.Lines)
    recent_merges = $mergeLog
  }

  # ===========================================================  WORKTREES
  $gWT = Invoke-GitLines $Repo @('worktree', 'list', '--porcelain')
  $wtRecords = @()
  if ($gWT.Exit -eq 0) {
    $cur = $null
    foreach ($l in $gWT.Lines) {
      if ($l -like 'worktree *') {
        if ($cur) { $wtRecords += $cur }
        $cur = [ordered]@{ path = ($l -replace '^worktree ', '').Trim(); head = ''; branch = ''; detached = $false }
      } elseif ($l -like 'HEAD *' -and $cur) { $cur['head'] = ($l -replace '^HEAD ', '').Trim() }
      elseif ($l -like 'branch *' -and $cur) { $cur['branch'] = ($l -replace '^branch ', '').Trim() }
      elseif ($l -eq 'detached' -and $cur) { $cur['detached'] = $true }
    }
    if ($cur) { $wtRecords += $cur }
  }

  # lanes first: worktree states can be declared in the manifest
  $manifest = Read-LaneManifest -Path $LanesFile
  $stateOverrides = Get-LaneStateOverrides -Lanes $manifest.lanes

  $branches = @()
  $gBr = Invoke-GitLines $Repo @('for-each-ref', '--format=%(refname:short)', 'refs/heads')
  if ($gBr.Exit -eq 0) { $branches = $gBr.Lines }
  $branchLast = @{}
  $gBl = Invoke-GitLines $Repo @('for-each-ref', '--format=%(refname:short)|%(committerdate:iso8601-strict)', 'refs/heads')
  if ($gBl.Exit -eq 0) {
    foreach ($l in $gBl.Lines) {
      $p = $l -split '\|'
      if ($p.Count -ge 2) {
        $d = [datetime]::MinValue
        if ([datetime]::TryParse($p[1], [ref]$d)) { $branchLast[$p[0]] = $d.ToUniversalTime() }
      }
    }
  }

  # One throttled pass over every worktree. Sequentially this is ~2 git calls
  # plus a directory walk per worktree, which is minutes on this box's HDD;
  # throttled parallel keeps the report inside one screen without going wild.
  $repoNorm = ($Repo -replace '/', '\').TrimEnd('\')
  $jobs = @()
  foreach ($w in $wtRecords) {
    $jp = $w['path']
    $jobs += [pscustomobject]@{
      path = $jp
      head = $w['head']
      branch = ($w['branch'] -replace '^refs/heads/', '')
      is_main = ((($jp -replace '/', '\').TrimEnd('\')) -eq $repoNorm)
    }
  }

  $uRepo = $Repo
  $uFast = [bool]$Fast
  $uBudget = $FootprintBudgetSec
  $uThrottle = [math]::Max(1, $ThrottleLimit)
  $uGitTimeoutMs = [math]::Max(500, $GitTimeoutMs)

  if ($PSVersionTable.PSVersion.Major -lt 7) {
    # Windows PowerShell 5.1 has no -Parallel. Same measurements, serialised.
    $probe = @($jobs | ForEach-Object {
      $j = $_
      $p = $j.path
      $exists = Test-Path -LiteralPath $p
      $merged = $null
      if ($j.is_main) { $merged = $true }
      elseif ($exists) {
        $null = & git -C $uRepo merge-base --is-ancestor $j.head main 2> $null
        $merged = ($LASTEXITCODE -eq 0)
      }
      $dcount = $null
      if ($exists) {
        $s = & git -C $p status --porcelain 2> $null
        if ($LASTEXITCODE -eq 0) { $dcount = @($s | Where-Object { $null -ne $_ -and "$_" -ne '' }).Count }
      }
      $junc = $false
      if ($exists) {
        try {
          $it = Get-Item -LiteralPath (Join-Path $p 'node_modules') -Force -ErrorAction Stop
          if ($it.LinkType) { $junc = $true }
        } catch { $junc = $false }
      }
      $fBytes = $null; $fFiles = $null; $fReason = ''
      if ($uFast) { $fReason = '-Fast: footprint walk skipped' }
      elseif (-not $exists) { $fReason = 'directory is gone; nothing to measure' }
      else {
        $m = Measure-TreeBytes -Path $p -Deadline ([datetime]::UtcNow.AddSeconds($uBudget))
        if ($m.truncated) { $fReason = 'footprint budget exhausted; a partial walk is not a measurement' }
        else { $fBytes = $m.bytes; $fFiles = $m.files }
      }
      [pscustomobject]@{
        path = $p; head = $j.head; branch = $j.branch; is_main = $j.is_main
        dir_exists = $exists; merged = $merged; dirty = $dcount
        junction = $junc; bytes = $fBytes; files = $fFiles; fReason = $fReason
      }
    })
  } else {
    $probe = @($jobs | ForEach-Object -Parallel {
      $j = $_
      $p = $j.path
      $gRepo = $using:uRepo
      $fast = $using:uFast
      $budget = $using:uBudget
      $gitMs = $using:uGitTimeoutMs

      # Bounded git. Under load a worktree probe can hang, and a status tool
      # that hangs is not a gate. Anything over budget is reported
      # not_measured with a reason - never silently dropped, never 0.
      function Invoke-GitBounded {
        param([string]$Dir, [string[]]$A, [int]$TimeoutMs)
        $psi = [System.Diagnostics.ProcessStartInfo]::new()
        $psi.FileName = 'git'
        foreach ($x in $A) { [void]$psi.ArgumentList.Add($x) }
        $psi.RedirectStandardOutput = $true
        $psi.RedirectStandardError = $true
        $psi.RedirectStandardInput = $true
        $psi.UseShellExecute = $false
        $psi.CreateNoWindow = $true
        try { $pr = [System.Diagnostics.Process]::Start($psi) } catch {
          return [pscustomobject]@{ Exit = -1; Text = ''; TimedOut = $true }
        }
        $reader = $pr.StandardOutput.ReadToEndAsync()
        if (-not $pr.WaitForExit($TimeoutMs)) {
          try { $pr.Kill($true) } catch { }
          try { $pr.WaitForExit(2000) | Out-Null } catch { }
          return [pscustomobject]@{ Exit = -1; Text = ''; TimedOut = $true }
        }
        return [pscustomobject]@{ Exit = $pr.ExitCode; Text = $reader.Result; TimedOut = $false }
      }

      $exists = Test-Path -LiteralPath $p

      # merged is PROVED, never guessed: is this worktree's HEAD an ancestor of main?
      $merged = $null
      $mergedWhy = ''
      if ($j.is_main) { $merged = $true }
      elseif (-not $exists) { $mergedWhy = 'directory is gone, so its HEAD cannot be compared' }
      else {
        $r = Invoke-GitBounded -Dir $gRepo -A @('merge-base', '--is-ancestor', $j.head, 'main') -TimeoutMs $gitMs
        if ($r.TimedOut) { $mergedWhy = "merge-base exceeded $gitMs ms on a loaded machine" }
        else { $merged = ($r.Exit -eq 0) }
      }

      $dcount = $null
      $dirtyWhy = ''
      if (-not $exists) { $dirtyWhy = 'directory is gone' }
      else {
        $r = Invoke-GitBounded -Dir $p -A @('status', '--porcelain') -TimeoutMs $gitMs
        if ($r.TimedOut) { $dirtyWhy = "git status exceeded $gitMs ms on a loaded machine" }
        else { $dcount = @($r.Text -split "`n" | Where-Object { $_ -ne '' }).Count }
      }

      # a node_modules JUNCTION is a hard stop for worktree move/remove
      $junc = $false
      if ($exists) {
        try {
          $it = Get-Item -LiteralPath (Join-Path $p 'node_modules') -Force -ErrorAction Stop
          if ($it.LinkType) { $junc = $true }
        } catch { $junc = $false }
      }

      $fBytes = $null; $fFiles = $null; $fReason = ''
      if ($fast) {
        $fReason = '-Fast: footprint walk skipped'
      } elseif (-not $exists) {
        $fReason = 'directory is gone; nothing to measure'
      } else {
        # Bounded walk. node_modules/.git/dist are skipped: node_modules is a
        # junction onto the MAIN install, so counting it here would multiply
        # one tree by the number of worktrees and report a lie.
        $deadline = [datetime]::UtcNow.AddSeconds($budget)
        $bytes = 0L; $files = 0; $trunc = $false
        $stack = New-Object System.Collections.Generic.Stack[string]
        $stack.Push($p)
        $skip = @('node_modules', '.git', 'dist', '.vite', '__pycache__')
        while ($stack.Count -gt 0) {
          if ([datetime]::UtcNow -gt $deadline) { $trunc = $true; break }
          $dir = $stack.Pop()
          $kids = @()
          try { $kids = @(Get-ChildItem -LiteralPath $dir -Force -ErrorAction SilentlyContinue) } catch { continue }
          foreach ($k in $kids) {
            if ($k.PSIsContainer) {
              if ($skip -contains $k.Name) { continue }
              if ($k.LinkType) { continue }
              $stack.Push($k.FullName)
            } else { try { $bytes += $k.Length; $files++ } catch { } }
          }
        }
        if ($trunc) {
          $fReason = "footprint budget of $budget s exhausted; a partial walk of $($files) files is not a measurement"
        } else {
          $fBytes = $bytes; $fFiles = $files
        }
      }

      [pscustomobject]@{
        path = $j.path; head = $j.head; branch = $j.branch; is_main = $j.is_main
        dir_exists = $exists; merged = $merged; merged_why = $mergedWhy
        dirty = $dcount; dirty_why = $dirtyWhy
        junction = $junc; bytes = $fBytes; files = $fFiles; fReason = $fReason
      }
    } -ThrottleLimit $uThrottle)
  }

  $wtOut = @()
  $wtMissingDir = @()
  $wtUnmerged = @()
  $wtDirty = @()
  $wtJunctions = @()
  $totalWtBytes = 0L
  $wtBytesKnown = 0

  foreach ($r in $probe) {
    if (-not $r.dir_exists) { $wtMissingDir += $r.path }
    if ($r.merged -eq $false) { $wtUnmerged += $r.path }
    if ($null -ne $r.dirty -and $r.dirty -gt 0) { $wtDirty += $r.path }
    if ($r.junction) { $wtJunctions += $r.path }
    if ($null -ne $r.bytes) { $totalWtBytes += [long]$r.bytes; $wtBytesKnown++ }

    $norm = ($r.path -replace '/', '\')
    $ms = ''
    if ($stateOverrides.ContainsKey($norm.TrimEnd('\'))) { $ms = $stateOverrides[$norm.TrimEnd('\')] }
    $mergedWord = if ($null -eq $r.merged) { 'unknown' } elseif ($r.merged) { 'yes' } else { 'no' }
    $state = Resolve-WorktreeState -Path $norm -ManifestState $ms -Merged $mergedWord

    $wtOut += [pscustomobject]@{
      path = $r.path
      branch = $r.branch
      head = $r.head
      is_main = $r.is_main
      dir_exists = $r.dir_exists
      merged_into_main = $r.merged
      dirty_files = $r.dirty
      state = $state
      node_modules_junction = $r.junction
      footprint_bytes = $r.bytes
      footprint_files = $r.files
      footprint_reason = $r.fReason
    }
  }

  $model['worktrees'] = [ordered]@{
    n_registered = $wtRecords.Count
    n_dir_missing = $wtMissingDir.Count
    n_unmerged = $wtUnmerged.Count
    n_dirty = $wtDirty.Count
    n_node_modules_junction = $wtJunctions.Count
    footprint_bytes_total = if ($wtBytesKnown -eq $wtRecords.Count) { $totalWtBytes } else { $null }
    footprint_bytes_known_for = $wtBytesKnown
    footprint_excludes = 'node_modules (shared junction), .git, dist, .vite, __pycache__'
    items = @($wtOut | ForEach-Object {
        [ordered]@{
          path = $_.path; branch = $_.branch; head = $_.head; is_main = $_.is_main
          dir_exists = $_.dir_exists; merged_into_main = $_.merged_into_main
          dirty_files = $_.dirty_files; state = $_.state
          node_modules_junction = $_.node_modules_junction
          footprint_bytes = $_.footprint_bytes; footprint_files = $_.footprint_files
          footprint_reason = $_.footprint_reason
        }
      })
  }

  # ==================================================================  DEPS
  $dep = Get-DepState -Root $Repo -MinFiles $DepsMinFiles
  $model['deps'] = [ordered]@{
    state = $dep.state
    files = $dep.files
    bin_present = $dep.bin
    bytes = $(if ($dep.PSObject.Properties['bytes']) { $dep.bytes } else { $null })
    min_files_floor = $DepsMinFiles
    reason = $dep.reason
    verdict = $(switch ($dep.state) { 'complete' { $script:VERDICT_OK } 'incomplete' { $script:VERDICT_FAIL } 'absent' { $script:VERDICT_FAIL } default { $script:VERDICT_NM } })
  }

  # =================================================================  DISK
  $drives = @()
  try {
    foreach ($d in (Get-PSDrive -PSProvider FileSystem -ErrorAction SilentlyContinue)) {
      if ($d.Name -notmatch '^[A-Z]$') { continue }
      if ($null -eq $d.Free) { continue }
      $drives += [pscustomobject]@{
        drive = "$($d.Name):"
        used_gb = [math]::Round(($d.Used / 1GB), 1)
        free_gb = [math]::Round(($d.Free / 1GB), 1)
      }
    }
  } catch { }
  $drives = @($drives | Sort-Object { $_.drive })
  $freeTotal = 0.0
  foreach ($d in $drives) { $freeTotal += $d.free_gb }

  $tmpPath = Join-Path $Repo 'tmp'
  $tmpBytes = $null
  $tmpFiles = $null
  $tmpReason = 'tmp/ does not exist'
  if (Test-Path -LiteralPath $tmpPath) {
    $tm = Measure-TreeBytes -Path $tmpPath -Deadline ([datetime]::UtcNow.AddSeconds([math]::Max(10, $FootprintBudgetSec)))
    $tmpBytes = $tm.bytes; $tmpFiles = $tm.files
    $tmpReason = $(if ($tm.truncated) { 'tmp/ walk hit the budget; partial' } else { '' })
  }
  $model['disk'] = [ordered]@{
    drives = @($drives | ForEach-Object { [ordered]@{ drive = $_.drive; used_gb = $_.used_gb; free_gb = $_.free_gb } })
    drives_n = $drives.Count
    free_gb_total = [math]::Round($freeTotal, 1)
    tmp_bytes = $tmpBytes
    tmp_files = $tmpFiles
    tmp_reason = $tmpReason
    worktree_footprint_bytes = $(if ($wtBytesKnown -eq $wtRecords.Count) { $totalWtBytes } else { $null })
    combined_bytes = $(if (($null -ne $tmpBytes) -and ($wtBytesKnown -eq $wtRecords.Count)) { $totalWtBytes + $tmpBytes } else { $null })
  }

  # ================================================================= PROBE
  $probe = Get-ProbeSummary -Path $ProbeFile -Tail $ProbeTail
  $model['probe'] = [ordered]@{
    path = $ProbeFile
    measured = $probe.measured
    n = $probe.n
    window = $probe.window
    p50_ms = $probe.p50
    p95_ms = $probe.p95
    max_ms = $probe.max
    ok_count = $probe.ok
    failure_count = $probe.failures
    file_lines_total = $probe.lines
    tail_scanned = $probe.tail_scanned
    unparsable_lines = $probe.unparsable
    last_sample_ts = $probe.last_ts
    reason = $probe.reason
    per_check = @($probe.per_check | ForEach-Object {
        [ordered]@{ check = $_.check; n = $_.n; p50_ms = $_.p50; p95_ms = $_.p95; max_ms = $_.max; failures = $_.failures }
      })
  }

  # ================================================================= LANES
  $existingWts = @($wtRecords | ForEach-Object { $_.path })
  $laneRows = Get-LaneQuiet -Lanes $manifest.lanes -ExistingWorktrees $existingWts `
    -ExistingBranches $branches -BranchLastCommit $branchLast -StaleHours $StaleHours
  $collisions = Get-LaneCollisions -Lanes $manifest.lanes
  $quietLanes = @($laneRows | Where-Object { $_.verdict -eq 'QUIET' })

  $model['lanes'] = [ordered]@{
    manifest_path = $LanesFile
    manifest_present = $manifest.present
    manifest_reason = $manifest.reason
    n_declared = $manifest.lanes.Count
    n_quiet = $quietLanes.Count
    n_collision = $collisions.Count
    stale_window_hours = $StaleHours
    items = @($laneRows | ForEach-Object {
        [ordered]@{
          lane = $_.lane; scope = $_.scope; owns = $_.owns; branch = $_.branch
          worktree = $_.worktree; state = $_.state; dispatched_at = $_.dispatched_at
          age_hours = $_.age_hours; verdict = $_.verdict; quiet_reasons = $_.quiet
        }
      })
    collisions = @($collisions | ForEach-Object {
        [ordered]@{ lane_a = $_.lane_a; glob_a = $_.glob_a; lane_b = $_.lane_b; glob_b = $_.glob_b; kind = $_.kind }
      })
  }

  # ================================================================  GATES
  $gates = @()

  $gates += New-Gate -Name 'tree_clean' -Verdict $(if ($dirtyAgent.Count -eq 0) { $script:VERDICT_OK } else { $script:VERDICT_FAIL }) `
    -Detail ('{0} agent-owned dirty file(s); {1} watcher-owned' -f $dirtyAgent.Count, $dirtyWatcher.Count) `
    -Reason $(if ($dirtyAgent.Count) { 'uncommitted work by an agent; only the watcher may dirty .steady-watcher.json' } else { '' })
  foreach ($d in $dirtyAgent) { $gates += New-Gate -Name ('tree_clean:' + $d.path) -Verdict $script:VERDICT_FAIL -Detail ('{0} {1}' -f $d.xy, $d.owner) }

  $gates += New-Gate -Name 'deps_complete' -Verdict $model['deps'].verdict `
    -Detail ('state={0} files={1} .bin={2}' -f $dep.state, $(if ($null -eq $dep.files) { 'unknown' } else { $dep.files }), $(if ($dep.bin) { 'present' } else { 'ABSENT' })) `
    -Reason $dep.reason

  $wtDecision = Get-WorktreeVerdict -NRegistered $wtRecords.Count -MaxWorktrees $MaxWorktrees `
    -MissingDir $wtMissingDir -CollisionCount $collisions.Count
  $wtVerdict = $wtDecision.verdict
  $wtReason = $wtDecision.reason
  $gates += New-Gate -Name 'worktrees_sane' -Verdict $wtVerdict `
    -Detail ('n={0} unmerged={1} dirty={2} missing_dir={3} junction={4} cap={5} collisions={6}' -f $wtRecords.Count, $wtUnmerged.Count, $wtDirty.Count, $wtMissingDir.Count, $wtJunctions.Count, $MaxWorktrees, $collisions.Count) `
    -Reason $wtReason

  $gates += New-Gate -Name 'probe_measured' -Verdict $(if ($probe.measured) { $script:VERDICT_OK } else { $script:VERDICT_NM }) `
    -Detail $(if ($probe.measured) { 'n={0} window={1} p50={2}ms p95={3}ms max={4}ms fail={5}' -f $probe.n, $probe.window, $probe.p50, $probe.p95, $probe.max, $probe.failures } else { 'no distribution' }) `
    -Reason $probe.reason

  $gates += New-Gate -Name 'lanes_recorded' -Verdict $(if ($manifest.present) { $script:VERDICT_OK } else { $script:VERDICT_NM }) `
    -Detail ('declared={0} quiet={1}' -f $manifest.lanes.Count, $quietLanes.Count) `
    -Reason $manifest.reason

  $exit = Get-GateVerdict -Gates $gates
  $model['gates'] = @($gates | ForEach-Object { [ordered]@{ name = $_.name; verdict = $_.verdict; detail = $_.detail; reason = $_.reason } })
  $model['exit_code'] = $exit
  $model['exit_meaning'] = switch ($exit) {
    0 { 'all gates measured and passing' }
    2 { 'GATE FAILURE - measured, and bad' }
    3 { 'NOT MEASURED - nothing failed, but a gate could not be measured' }
    default { 'unexpected' }
  }

  if ($Json) {
    ($model | ConvertTo-Json -Depth 8)
    $script:WS_EXIT = $exit
    return
  }

  # =================================================================  text
  $reportWidth = 78
  & $add ("=" * $reportWidth)
  & $add ('VOD.RIP WORKSPACE STATUS   read-only   generated {0}' -f $stamp)
  & $add ('observing: {0}' -f $Repo)
  & $add ("=" * $reportWidth)

  & $add ''
  & $add 'GIT'
  & $add ('  branch            {0}   sha {1}' -f $branch, $sha)
  if ($null -ne $ahead -and $null -ne $behind) {
    & $add ('  vs origin/main    ahead={0}  behind={1}   (n=1 ref pair, at {2})' -f $ahead, $behind, $stamp)
  } else {
    & $add ('  vs origin/main    not_measured  reason=origin/main not resolvable (no remote ref, or offline)')
  }
  & $add ('  dirty             total={0}  agent-owned={1}  watcher-owned={2}   (n=porcelain lines)' -f $dirty.Count, $dirtyAgent.Count, $dirtyWatcher.Count)
  foreach ($d in $dirty) {
    & $add ('    [{0}] {1,-9} {2}' -f $d.owner, $d.xy, $d.path)
  }
  & $add ('  stash             n={0}   (window=entire stash reflog)   UNTOUCHABLE - do not pop/drop/push' -f $(if ($null -eq $stashN) { 'unknown' } else { $stashN }))
  & $add ('  recent merges     n={0} requested, showing {1}' -f $Merges, $mergeLog.Count)
  foreach ($m in $mergeLog) { & $add ('    ' + $m) }

  & $add ''
  & $add ('WORKTREES   population = all {0} registered worktrees (git worktree list, at {1})' -f $wtRecords.Count, $stamp)
  & $add ('  unmerged={0}  dirty={1}  dir-gone={2}  node_modules-junction={3}  cap={4}' -f $wtUnmerged.Count, $wtDirty.Count, $wtMissingDir.Count, $wtJunctions.Count, $MaxWorktrees)
  & $add ('  state column: wip|done|unsure|unmanaged  (an UNMERGED worktree is never shown as done)')
  & $add ('  footprint excludes: node_modules (shared junction), .git, dist, .vite, __pycache__')
  & $add ('  {0,-3} {1,-9} {2,-8} {3,-7} {4,-11} {5}' -f 'DIR', 'MERGED', 'DIRTY', 'JUNC', 'STATE', 'PATH')
  $i = 0
  foreach ($w in $wtOut) {
    $i++
    $dirTxt = 'ok'
    if (-not $w.dir_exists) { $dirTxt = 'GONE' }
    $mTxt = 'unknown'
    if ($null -ne $w.merged_into_main) { $mTxt = if ($w.merged_into_main) { 'yes' } else { 'NO' } }
    $dTxt = 'unknown'
    if ($null -ne $w.dirty_files) { $dTxt = "$($w.dirty_files)" }
    $jTxt = if ($w.node_modules_junction) { 'yes' } else { 'no' }
    $sizeTxt = 'not_measured'
    if ($null -ne $w.footprint_bytes) { $sizeTxt = Format-MB $w.footprint_bytes }
    $flag = ''
    if (-not $w.dir_exists) { $flag += ' <<DIR-GONE' }
    elseif (-not $w.merged_into_main -and $w.merged_into_main -ne $null -and -not $w.is_main) { $flag += ' <<UNMERGED' }
    if ($null -ne $w.dirty_files -and $w.dirty_files -gt 0) { $flag += ' <<DIRTY' }
    if ($w.node_modules_junction) { $flag += ' <<JUNCTION' }
    & $add ('  {0,-3} {1,-9} {2,-8} {3,-7} {4,-11} {5}  {6}{7}' -f $i, $mTxt, $dTxt, $jTxt, $w.state, $w.path, $sizeTxt, $flag)
  }
  if ($wtBytesKnown -ne $wtRecords.Count) {
    & $add ('  footprint: not_measured for all - reason=partial walk ({0}/{1} measured, budget {2}s, or -Fast)' -f $wtBytesKnown, $wtRecords.Count, $FootprintBudgetSec)
  } else {
    & $add ('  footprint total     {0}   (sum of {1} measured worktrees)' -f (Format-MB $totalWtBytes), $wtBytesKnown)
  }

  & $add ''
  & $add 'DEPENDENCIES'
  & $add ('  node_modules      state={0}   files={1}   .bin={2}' -f $dep.state, $(if ($null -eq $dep.files) { 'not_measured' } else { $dep.files }), $(if ($dep.bin) { 'present' } else { 'ABSENT' }))
  & $add ('  floor             {0} files   (population = full recursive count of node_modules, at {1})' -f $DepsMinFiles, $stamp)
  if ($dep.reason) { & $add ('  reason            {0}' -f $dep.reason) }
  if ($dep.state -ne 'complete') {
    & $add '  >> Any `tsc`/`vitest` PASS reported right now is a FALSE GREEN.'
    & $add '     The test binary lives in node_modules\.bin, which is missing.'
  }

  & $add ''
  & $add ('DISK   population = {0} fixed drives (Get-PSDrive FileSystem, at {1})' -f $drives.Count, $stamp)
  foreach ($d in $drives) { & $add ('  {0,-6} free {1,8} GB   used {2,8} GB' -f $d.drive, $d.free_gb, $d.used_gb) }
  & $add ('  free total        {0} GB across {1} drives' -f [math]::Round($freeTotal, 1), $drives.Count)
  if ($null -ne $tmpBytes) {
    & $add ('  tmp/              {0} in {1} files   ({2})' -f (Format-MB $tmpBytes), $tmpFiles, $tmpPath)
  } else {
    & $add ('  tmp/              not_measured   reason={0}' -f $tmpReason)
  }
  if ($null -ne $model['disk'].combined_bytes) {
    & $add ('  worktrees+tmp     {0}   (sum of measured parts)' -f (Format-MB $model['disk'].combined_bytes))
  } else {
    & $add ('  worktrees+tmp     not_measured   reason=at least one worktree footprint was not measured')
  }
  & $add '  heavy data belongs on G:/H:/I: - never the C: system NVMe'

  & $add ''
  & $add 'LIVE PROBE'
  & $add ('  file              {0}' -f $ProbeFile)
  if ($probe.measured) {
    & $add ('  distribution      n={0}  window={1}' -f $probe.n, $probe.window)
    & $add ('  p50={0}ms  p95={1}ms  max={2}ms  ok={3}  failures={4}' -f $probe.p50, $probe.p95, $probe.max, $probe.ok, $probe.failures)
    & $add ('  population        {0} sample(s) from the last {1} of {2} file line(s); {3} unparsable' -f $probe.n, $probe.tail_scanned, $probe.lines, $probe.unparsable)
    foreach ($c in $probe.per_check) {
      & $add ('    {0,-10} n={1,-4} p50={2,-7} p95={3,-7} max={4,-7} fail={5}' -f $c.check, $c.n, $c.p50, $c.p95, $c.max, $c.failures)
    }
    if ($probe.reason) { & $add ('  caveat            {0}' -f $probe.reason) }
  } else {
    & $add '  distribution      not_measured'
    & $add ('  reason            {0}' -f $probe.reason)
    & $add '  (this is NOT zero. there is no measurement to report.)'
  }

  & $add ''
  & $add 'LANES   population = the ownership manifest (a coordination record; git is the authority on merges)'
  & $add ('  manifest          {0}   present={1}' -f $LanesFile, $manifest.present)
  if (-not $manifest.present) { & $add ('  reason            {0}' -f $manifest.reason) }
  & $add ('  declared={0}  quiet={1}  collisions={2}  stale-window={3}h' -f $manifest.lanes.Count, $quietLanes.Count, $collisions.Count, $StaleHours)
  if ($manifest.lanes.Count -eq 0) {
    & $add '  no lane is registered as owning anything. Four writers with no manifest is not a clean workspace.'
  }
  foreach ($l in $laneRows) {
    $ownsTxt = if ($l.owns -and $l.owns.Count) { ($l.owns -join ' ') } else { '(owns nothing declared)' }
    & $add ('  [{0}] {1}  state={2} age={3}h' -f $l.verdict, $l.lane, $l.state, $l.age_hours)
    & $add ('        scope     {0}' -f $l.scope)
    & $add ('        owns      {0}' -f $ownsTxt)
    & $add ('        branch    {0}   worktree {1}' -f $l.branch, $l.worktree)
    & $add ('        dispatched {0}' -f $l.dispatched_at)
    if ($l.quiet -and $l.quiet.Count) { & $add ('        QUIET     {0}' -f ($l.quiet -join ', ')) }
  }
  if ($collisions.Count -gt 0) {
    & $add '  !! FILE OWNERSHIP COLLISIONS - two lanes claim overlapping paths. This is how work is destroyed.'
    foreach ($c in $collisions) {
      & $add ('     {0}  {1}  <->  {2}  {3}   [{4}]' -f $c.lane_a, $c.glob_a, $c.lane_b, $c.glob_b, $c.kind)
    }
  }

  & $add ''
  & $add 'GATES   (exit code)'
  foreach ($g in $gates) {
    & $add ('  {0,-4} {1}' -f $g.verdict, $g.name)
    if ($g.detail) { & $add ('        {0}' -f $g.detail) }
    if ($g.reason) { & $add ('        reason: {0}' -f $g.reason) }
  }
  & $add ("-" * $reportWidth)
  switch ($exit) {
    0 { & $add 'VERDICT  PASS (exit 0)   every gate was measured, and every gate passed' }
    2 { & $add 'VERDICT  GATE FAILURE (exit 2)   something was measured, and it is bad' }
    3 { & $add 'VERDICT  NOT MEASURED (exit 3)   nothing failed, but something could not be measured' }
  }
  & $add 'This tool changes nothing. It only reads.'
  & $add ("=" * $reportWidth)

  # The report goes to stdout; the exit code travels out of band in
  # $script:WS_EXIT. Returning it would mix it into the output pipeline and
  # swallow the report.
  foreach ($l in $lines) { Write-Output $l }
  $script:WS_EXIT = $exit
  return
}

# Run only when invoked, not when dot-sourced by the test harness.
if ($MyInvocation.InvocationName -ne '.') {
  $script:WS_EXIT = 1
  Invoke-WorkspaceStatus
  exit $script:WS_EXIT
}
