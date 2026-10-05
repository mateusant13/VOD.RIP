<#
.SYNOPSIS
  The ownership manifest CLI: add / release / list / quiet for agent lanes.

.DESCRIPTION
  A lane is one writer working in its own worktree on a disjoint set of files.
  This file records who owns what, so two writers cannot silently land on the
  same file. It is a COORDINATION RECORD, not an authority: git is the authority
  for what is merged; this is the authority for who is editing what.

  It is a plain tab-separated file, committed, so it diffs and reviews normally.

  Usage:
    .\scripts\lane-ownership.ps1 add -Lane preview-ui -Scope 'preview panel' `
        -Owns 'src/components/Preview.tsx,src/api/preview.ts' `
        -Branch agent/preview-ui -Worktree I:\TEMP\wt-preview-ui
    .\scripts\lane-ownership.ps1 quiet -StaleHours 24
    .\scripts\lane-ownership.ps1 release -Lane preview-ui

  `add` REFUSES (exit 2) when the new lane's globs overlap an existing lane's
  globs. Overlap is the failure that costs work here, so it is stopped at
  dispatch time rather than merely reported afterwards.

.EXAMPLE
  .\scripts\lane-ownership.ps1 list

.NOTES
  Staleness is a first-class question: a crashed lane leaves an entry behind, and
  an ownership record nobody prunes is worse than none. `quiet` answers
  "which lanes have gone quiet?" - worktree gone, branch gone, or dispatched
  long ago with no commit since.
#>
[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)]
  [ValidateSet('add', 'release', 'list', 'quiet')]
  [string]$Action,

  [string]$Lane,
  [string]$Scope = '',
  [string]$Owns = '',
  [string]$Branch = '',
  [string]$Worktree = '',
  [ValidateSet('wip', 'done', 'unsure', 'archive')]
  [string]$State = 'wip',
  [string]$Contact = '',

  # Overwrite an existing entry for the same lane id.
  [switch]$Force,

  # A lane dispatched longer ago than this with no commit since counts as quiet.
  [int]$StaleHours = 24,

  # Manifest location. Defaults to <repo>\docs\lane-ownership.tsv
  [string]$Manifest,

  # Repo to resolve branch/worktree existence against.
  [string]$Repo
)

$ErrorActionPreference = 'Stop'
$env:GIT_OPTIONAL_LOCKS = '0'
# Same reason as the report: "0,1h" beside "18 lanes" is unreadable.
[System.Threading.Thread]::CurrentThread.CurrentCulture = [System.Globalization.CultureInfo]::InvariantCulture

# $here is the directory this script lives in, which is scripts/. The report it
# reuses is a sibling, not its parent.
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = Split-Path -Parent $here

# Capture the caller's arguments BEFORE the dot-source below. Dot-sourcing runs
# workspace-status.ps1's own param block in THIS scope, which resets $Repo and
# $Manifest to that script's defaults and silently discards whatever was passed
# to this CLI. Captured first, and under names the dot-source cannot overwrite.
$cliRepo = $Repo
$cliManifest = $Manifest

. (Join-Path $here 'workspace-status.ps1')

if (-not $cliRepo) {
  $cliRepo = (git -C $here rev-parse --show-toplevel 2> $null | Select-Object -First 1)
  if ($cliRepo) { $cliRepo = "$cliRepo".Trim() }
}
if (-not $cliRepo) { Write-Error 'cannot resolve a repository (pass -Repo <path>)'; exit 1 }
if (-not $cliManifest) { $cliManifest = Join-Path $cliRepo 'docs\lane-ownership.tsv' }

$HEADER = @('lane_id', 'scope', 'owns', 'branch', 'worktree', 'dispatched_at', 'state', 'contact')

function Get-ManifestRows {
  if (-not (Test-Path -LiteralPath $cliManifest)) { return @() }
  $rows = @(Import-Csv -LiteralPath $cliManifest -Delimiter "`t" -ErrorAction Stop)
  if (-not $rows) { return @() }
  return @($rows | Where-Object { $_.'lane_id' })
}

function Write-ManifestRows {
  param([Parameter(Mandatory = $true)]$Rows)
  $dir = Split-Path -Parent $cliManifest
  if ($dir -and -not (Test-Path -LiteralPath $dir)) {
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
  }
  $sb = New-Object System.Text.StringBuilder
  [void]$sb.Append(($HEADER -join "`t"))
  foreach ($r in $Rows) {
    $cells = @(
      "$(($r.PSObject.Properties['lane_id']).Value)",
      "$(($r.PSObject.Properties['scope']).Value)",
      "$(($r.PSObject.Properties['owns']).Value)",
      "$(($r.PSObject.Properties['branch']).Value)",
      "$(($r.PSObject.Properties['worktree']).Value)",
      "$(($r.PSObject.Properties['dispatched_at']).Value)",
      "$(($r.PSObject.Properties['state']).Value)",
      "$(($r.PSObject.Properties['contact']).Value)"
    )
    # tabs and newlines would corrupt the record: flatten them
    $cells = @($cells | ForEach-Object { ("$_" -replace "`t", ' ' -replace "`r?`n", ' ') })
    [void]$sb.Append("`n")
    [void]$sb.Append(($cells -join "`t"))
  }
  [void]$sb.Append("`n")
  [System.IO.File]::WriteAllText($cliManifest, $sb.ToString(), (New-Object System.Text.UTF8Encoding($false)))
}

switch ($Action) {

  'add' {
    if (-not $Lane) { Write-Error 'add requires -Lane <id>'; exit 1 }
    $rows = @(Get-ManifestRows)
    $existing = @($rows | Where-Object { $_.'lane_id' -eq $Lane })

    $newOwns = @()
    if ($Owns) {
      $newOwns = @($Owns -split ',' | ForEach-Object { $_.Trim() } | Where-Object { $_ })
    }
    if ($newOwns.Count -eq 0) {
      Write-Error ("lane '{0}' owns nothing. Declare -Owns, or the manifest cannot stop a collision." -f $Lane)
      exit 1
    }

    if ($existing.Count -gt 0 -and -not $Force) {
      Write-Error ("lane '{0}' is already in the manifest. Use -Force to replace it, or 'release' first." -f $Lane)
      exit 1
    }

    $parsed = Read-LaneManifest -Path $cliManifest
    $others = @($parsed.lanes | Where-Object { $_.lane_id -ne $Lane })
    $clashes = @()
    foreach ($o in $others) {
      foreach ($a in $newOwns) {
        foreach ($b in $o.owns) {
          $kind = Get-GlobCollision $a $b
          if ($kind -ne 'none') {
            $clashes += [pscustomobject]@{ lane = $o.lane_id; glob = $b; mine = $a; kind = $kind }
          }
        }
      }
    }
    if ($clashes.Count -gt 0) {
      Write-Output 'REFUSED - file ownership collision. Two writers on one file has already destroyed work in this repo.'
      foreach ($c in $clashes) {
        Write-Output ("  lane '{0}' already owns {1}; '{2}' wants {3}   [{4}]" -f $c.lane, $c.glob, $Lane, $c.mine, $c.kind)
      }
      Write-Output 'Narrow -Owns, or release the other lane first. Nothing was written.'
      exit 2
    }

    $row = [pscustomobject]@{
      lane_id = $Lane; scope = $Scope; owns = ($newOwns -join ',')
      branch = $Branch; worktree = $Worktree
      dispatched_at = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
      state = $State; contact = $Contact
    }
    $kept = @($rows | Where-Object { $_.'lane_id' -ne $Lane })
    Write-ManifestRows -Rows (@($kept) + @($row))
    Write-Output ("ADDED lane={0} state={1} owns={2}" -f $Lane, $State, ($newOwns -join ' '))
    Write-Output ("MANIFEST={0}" -f $cliManifest)
    exit 0
  }

  'release' {
    if (-not $Lane) { Write-Error 'release requires -Lane <id>'; exit 1 }
    $rows = @(Get-ManifestRows)
    $kept = @($rows | Where-Object { $_.'lane_id' -ne $Lane })
    if ($kept.Count -eq $rows.Count) {
      Write-Output ("NOT-IN-MANIFEST lane={0}" -f $Lane)
      exit 1
    }
    Write-ManifestRows -Rows $kept
    Write-Output ("RELEASED lane={0}   (git, not this file, decides whether the work landed)" -f $Lane)
    exit 0
  }

  'list' {
    $rows = @(Get-ManifestRows)
    if (-not (Test-Path -LiteralPath $cliManifest)) {
      Write-Output ("no manifest at {0} - no lane is recorded as owning anything" -f $cliManifest)
      exit 0
    }
    Write-Output ("MANIFEST {0}   n={1}" -f $cliManifest, $rows.Count)
    Write-Output ('{0,-18} {1,-9} {2,-30} {3}' -f 'LANE', 'STATE', 'OWNS', 'BRANCH')
    foreach ($r in $rows) {
      Write-Output ('{0,-18} {1,-9} {2,-30} {3}' -f $r.'lane_id', $r.'state', $r.'owns', $r.'branch')
    }
    exit 0
  }

  'quiet' {
    $parsed = Read-LaneManifest -Path $cliManifest
    if (-not $parsed.present) {
      Write-Output ("not_measured  reason={0}" -f $parsed.reason)
      exit 3
    }
    $branches = @()
    $gBr = Invoke-GitLines -GitDir $cliRepo -GitArgs @('for-each-ref', '--format=%(refname:short)', 'refs/heads')
    if ($gBr.Exit -eq 0) { $branches = $gBr.Lines }

    $branchLast = @{}
    $gBl = Invoke-GitLines -GitDir $cliRepo -GitArgs @('for-each-ref', '--format=%(refname:short)|%(committerdate:iso8601-strict)', 'refs/heads')
    if ($gBl.Exit -eq 0) {
      foreach ($l in $gBl.Lines) {
        $p = $l -split '\|'
        if ($p.Count -ge 2) {
          $d = [datetime]::MinValue
          if ([datetime]::TryParse($p[1], [ref]$d)) { $branchLast[$p[0]] = $d.ToUniversalTime() }
        }
      }
    }
    $gWT = Invoke-GitLines -GitDir $cliRepo -GitArgs @('worktree', 'list', '--porcelain')
    $wtPaths = @()
    if ($gWT.Exit -eq 0) {
      foreach ($l in $gWT.Lines) { if ($l -like 'worktree *') { $wtPaths += ($l -replace '^worktree ', '').Trim() } }
    }

    $rows = Get-LaneQuiet -Lanes $parsed.lanes -ExistingWorktrees $wtPaths `
      -ExistingBranches $branches -BranchLastCommit $branchLast -StaleHours $StaleHours
    $quiet = @($rows | Where-Object { $_.verdict -eq 'QUIET' })
    Write-Output ("declared={0}  quiet={1}  stale-window={2}h" -f $rows.Count, $quiet.Count, $StaleHours)
    foreach ($r in $rows) {
      Write-Output ("  [{0,-6}] {1,-18} age={2}h  {3}" -f $r.verdict, $r.lane, $r.age_hours, ($r.quiet -join ','))
    }
    $coll = @(Get-LaneCollisions -Lanes $parsed.lanes)
    if ($coll.Count -gt 0) {
      Write-Output ("COLLISIONS n={0}" -f $coll.Count)
      foreach ($c in $coll) {
        Write-Output ("  {0} {1} <-> {2} {3}  [{4}]" -f $c.lane_a, $c.glob_a, $c.lane_b, $c.glob_b, $c.kind)
        Write-Output ("     two lanes hold the same repo-relative path uncommitted; separate worktrees today, but a merge conflict the moment both commit it")
      }
    }
    # A collision is as actionable as a quiet lane, so it must not exit 0.
    if ($quiet.Count -gt 0 -or $coll.Count -gt 0) { exit 2 }
    exit 0
  }
}
