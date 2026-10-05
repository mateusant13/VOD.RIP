<#
.SYNOPSIS
  Tests for scripts/workspace-status.ps1 - the report logic, with no live repo.

.DESCRIPTION
  These are unit tests against the PURE parts of the report: the classifier and
  the gates. They need no repository, no network, and no node_modules - which
  matters here, because the shared node_modules in this workspace is currently
  damaged and must not be depended on to prove the instrument works.

  Run:
    pwsh -NoProfile -File .\scripts\workspace-status.tests.ps1

  Exit 0 = every test passed. Exit 1 = at least one failed.

.NOTES
  Scratch files are written under a dedicated directory OUTSIDE the repository
  and are reused by name, so repeated runs do not accumulate litter.
#>
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
. (Join-Path $here 'workspace-status.ps1')

$script:Scratch = 'I:\TEMP\wsstatus-scratch\tests'
New-Item -ItemType Directory -Force -Path $script:Scratch | Out-Null

$script:Pass = 0
$script:Fail = 0
$script:Failures = New-Object System.Collections.Generic.List[string]

function Assert-That {
  param(
    [Parameter(Mandatory = $true)][string]$Name,
    [Parameter(Mandatory = $true)][bool]$Condition,
    [string]$Detail = ''
  )
  if ($Condition) {
    $script:Pass++
    Write-Output ("  PASS  {0}" -f $Name)
  } else {
    $script:Fail++
    $msg = "{0}{1}" -f $Name, $(if ($Detail) { "  --  $Detail" } else { '' })
    $script:Failures.Add($msg)
    Write-Output ("  FAIL  {0}{1}" -f $Name, $(if ($Detail) { "  --  $Detail" } else { '' }))
  }
}

function Assert-Equal {
  param(
    [Parameter(Mandatory = $true)][string]$Name,
    $Expected,
    $Actual
  )
  $same = ("$Expected" -eq "$Actual")
  Assert-That -Name $Name -Condition $same -Detail ("expected=[{0}] actual=[{1}]" -f $Expected, $Actual)
}

function Write-ScratchFile {
  param([string]$Name, [string]$Content)
  $p = Join-Path $script:Scratch $Name
  [System.IO.File]::WriteAllText($p, $Content, (New-Object System.Text.UTF8Encoding($false)))
  return $p
}

function New-FakeNodeModules {
  <#  Build a node_modules with a controllable .bin and file count.  #>
  param([string]$Name, [switch]$NoBin, [int]$Files = 3)
  $root = Join-Path $script:Scratch $Name
  $nm = Join-Path $root 'node_modules'
  if (-not (Test-Path -LiteralPath $nm)) { New-Item -ItemType Directory -Force -Path $nm | Out-Null }
  $bin = Join-Path $nm '.bin'
  if ($NoBin) {
    # leave a stale marker so the absence is deliberate, not an accident
    [System.IO.File]::WriteAllText((Join-Path $nm 'STALE_NO_BIN'), 'x')
  } else {
    if (-not (Test-Path -LiteralPath $bin)) { New-Item -ItemType Directory -Force -Path $bin | Out-Null }
    [System.IO.File]::WriteAllText((Join-Path $bin 'tsc'), 'x')
  }
  for ($i = 1; $i -le $Files; $i++) {
    [System.IO.File]::WriteAllText((Join-Path $nm ("pkg$i.js")), 'x')
  }
  return $root
}

Write-Output ''
Write-Output 'workspace-status report logic'
Write-Output '=============================='

# ---------------------------------------------------------------------------
Write-Output ''
Write-Output '[probe] a missing probe file is not_measured, and is never 0'

$missing = Join-Path $script:Scratch 'does-not-exist.jsonl'
if (Test-Path -LiteralPath $missing) { throw 'scratch precondition failed: the "missing" probe file exists' }
$s = Get-ProbeSummary -Path $missing
Assert-That -Name 'missing probe file reports measured=false' -Condition ($s.measured -eq $false)
Assert-That -Name 'missing probe file reports n as not_measured, NOT 0' `
  -Condition ("$($s.n)" -eq 'not_measured') -Detail "n=[$($s.n)]"
Assert-That -Name 'missing probe file reports p50 as not_measured, NOT 0' `
  -Condition ("$($s.p50)" -eq 'not_measured') -Detail "p50=[$($s.p50)]"
Assert-That -Name 'missing probe file gives a reason' -Condition ([bool]$s.reason) -Detail "reason=[$($s.reason)]"

# ---------------------------------------------------------------------------
Write-Output ''
Write-Output '[probe] an empty probe file is not_measured, and is never 0'

$empty = Write-ScratchFile -Name 'empty.jsonl' -Content ''
$s = Get-ProbeSummary -Path $empty
Assert-That -Name 'empty probe file reports measured=false' -Condition ($s.measured -eq $false)
Assert-That -Name 'empty probe file reports n as not_measured, NOT 0' `
  -Condition ("$($s.n)" -eq 'not_measured') -Detail "n=[$($s.n)]"
Assert-That -Name 'empty probe file gives a reason' -Condition ([bool]$s.reason)

# ---------------------------------------------------------------------------
Write-Output ''
Write-Output '[probe] a real distribution is summarised, not sampled'

$lines = @()
1..5 | ForEach-Object {
  $lines += ('{{"event":"health","seq":{0},"ok":true,"status":200,"ms":{1}.0,"ts":"2026-10-05T05:4{0}:00+00:00"}}' -f $_, ($_ * 10))
}
$lines += '{"event":"health","seq":6,"ok":false,"status":500,"ms":900.0,"ts":"2026-10-05T05:46:00+00:00"}'
$good = Write-ScratchFile -Name 'good.jsonl' -Content (($lines -join "`n") + "`n")
$s = Get-ProbeSummary -Path $good
Assert-Equal -Name 'distribution n counts every sample' -Expected 6 -Actual $s.n
Assert-Equal -Name 'distribution p50 is the median' -Expected 30 -Actual $s.p50
Assert-Equal -Name 'distribution max is the worst sample' -Expected 900 -Actual $s.max
Assert-Equal -Name 'failures are counted, not inferred' -Expected 1 -Actual $s.failures
Assert-Equal -Name 'ok count is n minus failures' -Expected 5 -Actual $s.ok
Assert-That -Name 'distribution carries a window' -Condition ([bool]$s.window)

# ---------------------------------------------------------------------------
Write-Output ''
Write-Output '[probe] n=1 is a sample, not a distribution, and says so'

$one = Write-ScratchFile -Name 'one.jsonl' -Content ('{"event":"health","seq":1,"ok":true,"ms":12.0,"ts":"2026-10-05T05:40:00+00:00"}' + "`n")
$s = Get-ProbeSummary -Path $one
Assert-That -Name 'single sample is still measured' -Condition ($s.measured -eq $true)
Assert-That -Name 'single sample is flagged as not a distribution' `
  -Condition ("$($s.reason)" -match 'single sample') -Detail "reason=[$($s.reason)]"

# ---------------------------------------------------------------------------
Write-Output ''
Write-Output '[deps] node_modules without .bin is a FAILURE, not a warning'

$noBin = New-FakeNodeModules -Name 'repo-nobin' -NoBin -Files 5
$d = Get-DepState -Root $noBin -MinFiles 1
Assert-Equal -Name 'missing .bin makes the install incomplete' -Expected 'incomplete' -Actual $d.state
Assert-That -Name 'missing .bin is detected' -Condition ($d.bin -eq $false)
Assert-That -Name 'missing .bin is called out as a false green' `
  -Condition ("$($d.reason)" -match 'FALSE GREEN|false green') -Detail "reason=[$($d.reason)]"
$gate = New-Gate -Name 'deps_complete' -Verdict $script:VERDICT_FAIL -Detail $d.reason
Assert-Equal -Name 'an incomplete install yields exit code 2 (GATE FAILURE)' `
  -Expected 2 -Actual (Get-GateVerdict -Gates @($gate))

# ---------------------------------------------------------------------------
Write-Output ''
Write-Output '[deps] .bin present and file count above the floor is complete'

$withBin = New-FakeNodeModules -Name 'repo-withbin' -Files 5
$d = Get-DepState -Root $withBin -MinFiles 1
Assert-Equal -Name 'a whole install is complete' -Expected 'complete' -Actual $d.state
Assert-That -Name 'a whole install measures its file count' -Condition ($d.files -ge 5) -Detail "files=$($d.files)"
$gate = New-Gate -Name 'deps_complete' -Verdict $script:VERDICT_OK -Detail 'ok'
Assert-Equal -Name 'a whole install yields exit code 0' -Expected 0 -Actual (Get-GateVerdict -Gates @($gate))

# ---------------------------------------------------------------------------
Write-Output ''
Write-Output '[deps] an install below the file floor is incomplete even with .bin'

$d = Get-DepState -Root $withBin -MinFiles 100000
Assert-Equal -Name 'too few files under node_modules is incomplete' -Expected 'incomplete' -Actual $d.state
Assert-That -Name 'the floor reason is reported' -Condition ("$($d.reason)" -match 'pruned|partial')

# ---------------------------------------------------------------------------
Write-Output ''
Write-Output '[deps] a repo with no node_modules at all is a FAILURE'

$bare = Join-Path $script:Scratch 'repo-bare'
if (-not (Test-Path -LiteralPath $bare)) { New-Item -ItemType Directory -Force -Path $bare | Out-Null }
$d = Get-DepState -Root $bare -MinFiles 1
Assert-Equal -Name 'an absent node_modules is absent, not zero files' -Expected 'absent' -Actual $d.state
Assert-That -Name 'an absent node_modules gives a reason' -Condition ([bool]$d.reason)

# ---------------------------------------------------------------------------
Write-Output ''
Write-Output '[worktrees] an UNMERGED worktree is never classified done'

$st = Resolve-WorktreeState -Path 'I:\TEMP\wt-x' -ManifestState 'done' -Merged 'no'
Assert-That -Name 'declared done but not merged is NOT done' -Condition ($st -ne 'done') -Detail "state=[$st]"
Assert-Equal -Name 'declared done but not merged falls back to wip' -Expected 'wip' -Actual $st

$st = Resolve-WorktreeState -Path 'I:\TEMP\wt-x' -ManifestState 'done' -Merged 'yes'
Assert-Equal -Name 'declared done AND merged IS done' -Expected 'done' -Actual $st

# an unestablished merge state is not a proof of merging
$st = Resolve-WorktreeState -Path 'I:\TEMP\wt-x' -ManifestState 'done' -Merged 'unknown'
Assert-That -Name 'declared done with UNKNOWN merge state is NOT done' -Condition ($st -ne 'done') -Detail "state=[$st]"

$st = Resolve-WorktreeState -Path 'I:\TEMP\wt-x' -ManifestState 'archive' -Merged 'no'
Assert-Equal -Name 'archive is preserved regardless of merge state' -Expected 'archive' -Actual $st

# a directory literally named "done" on disk does not make an unmerged lane done
$st = Resolve-WorktreeState -Path 'C:\wt\done' -ManifestState '' -Merged 'no'
Assert-That -Name 'a path segment named done does not override an unmerged head' `
  -Condition ($st -ne 'done') -Detail "state=[$st]"

$st = Resolve-WorktreeState -Path 'C:\wt\done' -ManifestState '' -Merged 'yes'
Assert-Equal -Name 'a path segment named done with a merged head IS done' -Expected 'done' -Actual $st

$st = Resolve-WorktreeState -Path 'C:\wt\wip' -ManifestState '' -Merged 'no'
Assert-Equal -Name 'a wip path segment is honoured' -Expected 'wip' -Actual $st
$st = Resolve-WorktreeState -Path 'I:\TEMP\wt-ar' -ManifestState '' -Merged 'no'
Assert-Equal -Name 'an unclassified worktree is unmanaged, not guessed' -Expected 'unmanaged' -Actual $st

# ---------------------------------------------------------------------------
Write-Output ''
Write-Output '[worktrees] a registered worktree whose directory is gone is a PROBLEM'

$v = Get-WorktreeVerdict -NRegistered 47 -MaxWorktrees 12 -MissingDir @('I:\TEMP\wt-gone') -CollisionCount 0
Assert-That -Name 'a vanished worktree directory fails the worktree gate' `
  -Condition ($v.verdict -eq $script:VERDICT_FAIL) -Detail "verdict=[$($v.verdict)]"
Assert-That -Name 'the vanished directory is named in the reason' `
  -Condition ("$($v.reason)" -match 'wt-gone') -Detail "reason=[$($v.reason)]"

$v = Get-WorktreeVerdict -NRegistered 47 -MaxWorktrees 12 -MissingDir @() -CollisionCount 0
Assert-That -Name 'over the worktree cap fails the gate' -Condition ($v.verdict -eq $script:VERDICT_FAIL)
Assert-That -Name 'the cap reason names the cap' -Condition ("$($v.reason)" -match 'cap')

$v = Get-WorktreeVerdict -NRegistered 5 -MaxWorktrees 12 -MissingDir @() -CollisionCount 0
Assert-That -Name 'under the cap with no problems passes' -Condition ($v.verdict -eq $script:VERDICT_OK)
Assert-That -Name 'a pass carries no reason' -Condition (-not [bool]$v.reason)

# ---------------------------------------------------------------------------
Write-Output ''
Write-Output '[lanes] two lanes claiming the same file are flagged'

$laneA = [pscustomobject]@{ lane_id = 'alpha'; owns = @('src/**'); scope = 'a'; branch = 'agent/alpha'; worktree = 'I:\TEMP\wt-alpha'; state = 'wip'; dispatched_at = [datetime]::UtcNow; dispatched_raw = '' }
$laneB = [pscustomobject]@{ lane_id = 'beta'; owns = @('src/App.tsx'); scope = 'b'; branch = 'agent/beta'; worktree = 'I:\TEMP\wt-beta'; state = 'wip'; dispatched_at = [datetime]::UtcNow; dispatched_raw = '' }
$c = @(Get-LaneCollisions -Lanes @($laneA, $laneB))
Assert-Equal -Name 'a glob and a file inside it collide' -Expected 1 -Actual $c.Count
Assert-That -Name 'the collision names both lanes' `
  -Condition ($c[0].lane_a -eq 'alpha' -and $c[0].lane_b -eq 'beta') -Detail "$($c[0].lane_a)/$($c[0].lane_b)"
Assert-Equal -Name 'a literal inside a glob is a proved collision' -Expected 'collision' -Actual $c[0].kind

$laneB2 = [pscustomobject]@{ lane_id = 'beta'; owns = @('backend/app.py'); scope = 'b'; branch = 'agent/beta'; worktree = 'I:\TEMP\wt-beta'; state = 'wip'; dispatched_at = [datetime]::UtcNow; dispatched_raw = '' }
$c = @(Get-LaneCollisions -Lanes @($laneA, $laneB2))
Assert-Equal -Name 'disjoint trees do not collide' -Expected 0 -Actual $c.Count

# the same file claimed by two lanes verbatim
$laneC = [pscustomobject]@{ lane_id = 'gamma'; owns = @('src/App.tsx'); scope = 'c'; branch = 'agent/gamma'; worktree = 'I:\TEMP\wt-gamma'; state = 'wip'; dispatched_at = [datetime]::UtcNow; dispatched_raw = '' }
$c = @(Get-LaneCollisions -Lanes @($laneB, $laneC))
Assert-Equal -Name 'the same file claimed twice collides' -Expected 1 -Actual $c.Count

# ---------------------------------------------------------------------------
Write-Output ''
Write-Output '[lanes] which lanes have gone quiet'

$now = [datetime]::new(2026, 10, 5, 6, 0, 0, [System.DateTimeKind]::Utc)
$old = [pscustomobject]@{
  lane_id = 'ghost'; scope = 's'; owns = @('src/**'); branch = 'agent/ghost'
  worktree = 'I:\TEMP\wt-ghost'; state = 'wip'
  dispatched_at = $now.AddHours(-30); dispatched_raw = '2026-10-04T00:00:00Z'
}
$rows = Get-LaneQuiet -Lanes @($old) -ExistingWorktrees @('I:\TEMP\wt-other') `
  -ExistingBranches @('agent/other') -BranchLastCommit @{} -Now $now -StaleHours 24
Assert-That -Name 'a lane whose worktree is gone is quiet' -Condition ('worktree_gone' -in $rows[0].quiet)
Assert-That -Name 'a lane whose branch is gone is quiet' -Condition ('branch_gone' -in $rows[0].quiet)
Assert-That -Name 'a lane quiet for a missing worktree AND branch is QUIET' -Condition ($rows[0].verdict -eq 'QUIET')

$live = [pscustomobject]@{
  lane_id = 'live'; scope = 's'; owns = @('src/**'); branch = 'agent/live'
  worktree = 'I:\TEMP\wt-live'; state = 'wip'
  dispatched_at = $now.AddHours(-1); dispatched_raw = '2026-10-05T05:00:00Z'
}
$rows = Get-LaneQuiet -Lanes @($live) -ExistingWorktrees @('I:\TEMP\wt-live') `
  -ExistingBranches @('agent/live') -BranchLastCommit @{'agent/live' = $now.AddMinutes(-5)} -Now $now -StaleHours 24
Assert-That -Name 'a fresh lane with a live worktree is active' -Condition ($rows[0].verdict -eq 'active')
Assert-Equal -Name 'an active lane reports its age in hours' -Expected 1 -Actual $rows[0].age_hours

$stale = [pscustomobject]@{
  lane_id = 'stale'; scope = 's'; owns = @('src/**'); branch = 'agent/stale'
  worktree = 'I:\TEMP\wt-stale'; state = 'wip'
  dispatched_at = $now.AddHours(-30); dispatched_raw = '2026-10-04T00:00:00Z'
}
$rows = Get-LaneQuiet -Lanes @($stale) -ExistingWorktrees @('I:\TEMP\wt-stale') `
  -ExistingBranches @('agent/stale') -BranchLastCommit @{} -Now $now -StaleHours 24
Assert-That -Name 'a lane dispatched 30h ago with no commit since is quiet' `
  -Condition ($rows[0].verdict -eq 'QUIET') -Detail "quiet=[$($rows[0].quiet -join ',')]"

$rows = Get-LaneQuiet -Lanes @($stale) -ExistingWorktrees @('I:\TEMP\wt-stale') `
  -ExistingBranches @('agent/stale') -BranchLastCommit @{'agent/stale' = $now.AddHours(-1)} -Now $now -StaleHours 24
Assert-That -Name 'a 30h-old lane WITH a recent commit stays active' -Condition ($rows[0].verdict -eq 'active')

# ---------------------------------------------------------------------------
Write-Output ''
Write-Output '[gates] gate failure and not-measured are different exit codes'

Assert-Equal -Name 'all gates passing is exit 0' -Expected 0 -Actual (Get-GateVerdict -Gates @(
  (New-Gate -Name 'a' -Verdict $script:VERDICT_OK),
  (New-Gate -Name 'b' -Verdict $script:VERDICT_OK)))

Assert-Equal -Name 'one gate failing is exit 2' -Expected 2 -Actual (Get-GateVerdict -Gates @(
  (New-Gate -Name 'a' -Verdict $script:VERDICT_OK),
  (New-Gate -Name 'b' -Verdict $script:VERDICT_FAIL)))

Assert-Equal -Name 'one gate unmeasured is exit 3, NOT 2' -Expected 3 -Actual (Get-GateVerdict -Gates @(
  (New-Gate -Name 'a' -Verdict $script:VERDICT_OK),
  (New-Gate -Name 'probe' -Verdict $script:VERDICT_NM -Reason 'probe file absent')))

Assert-Equal -Name 'a failure outranks an unmeasured gate' -Expected 2 -Actual (Get-GateVerdict -Gates @(
  (New-Gate -Name 'a' -Verdict $script:VERDICT_FAIL),
  (New-Gate -Name 'probe' -Verdict $script:VERDICT_NM)))

Assert-Equal -Name 'an empty gate set is exit 0' -Expected 0 -Actual (Get-GateVerdict -Gates @())

# ---------------------------------------------------------------------------
Write-Output ''
Write-Output '[glob] overlap detection is conservative in the safe direction'

Assert-That -Name 'src/** overlaps src/App.tsx' -Condition (Test-GlobOverlap 'src/**' 'src/App.tsx')
Assert-That -Name 'src/** overlaps src/**' -Condition (Test-GlobOverlap 'src/**' 'src/**')
Assert-That -Name 'a shallower glob conservatively overlaps a deeper one under it' `
  -Condition (Test-GlobOverlap 'scripts' 'scripts/workspace-status.ps1') `
  -Detail 'a file `scripts` and a dir `scripts` cannot coexist, but assuming overlap is the safe direction'
Assert-That -Name 'different trees do not overlap' -Condition (-not (Test-GlobOverlap 'src/**' 'backend/**'))
Assert-That -Name 'siblings under the same parent do not overlap' -Condition (-not (Test-GlobOverlap 'src/a.ts' 'src/b.ts'))
Assert-That -Name 'a single star does not cross a slash' -Condition (-not (Test-GlobOverlap 'src/*.ts' 'src/deep/a.ts'))
Assert-That -Name 'a single star does match within a segment' -Condition (Test-GlobOverlap 'src/*.ts' 'src/a.ts')
Assert-That -Name 'backslashes normalise to slashes' -Condition (Test-GlobOverlap 'src\**' 'src/App.tsx')
Assert-That -Name 'two wildcards on both sides are a possible collision' `
  -Condition ((Get-GlobCollision 'src/*.ts' 'src/*.tsx') -eq 'possible_collision') `
  -Detail "kind=[$(Get-GlobCollision 'src/*.ts' 'src/*.tsx')]"
Assert-Equal -Name 'wildcards that cannot meet report none' -Expected 'none' -Actual (Get-GlobCollision 'src/*.ts' 'backend/*.py')

# ---------------------------------------------------------------------------
Write-Output ''
Write-Output '=============================='
Write-Output ("passed {0}   failed {1}" -f $script:Pass, $script:Fail)
if ($script:Fail -gt 0) {
  Write-Output ''
  Write-Output 'FAILURES:'
  foreach ($f in $script:Failures) { Write-Output ("  - {0}" -f $f) }
  exit 1
}
Write-Output 'all green'
exit 0
