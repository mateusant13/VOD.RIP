<#
.SYNOPSIS
  Merge a finished agent lane back into main and clean up its worktree.

.DESCRIPTION
  Verifies the lane is committed and clean, fast-forwards main onto the lane branch,
  then removes the worktree. Refuses to merge a dirty or uncommitted lane so a
  half-finished agent never lands on main.

.EXAMPLE
  .\scripts\wt-merge.ps1 -lane frame-snap
#>
[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)][string]$Lane,
  [string]$Target = 'main',
  [string]$Root = 'I:\TEMP'
)

$ErrorActionPreference = 'Stop'

$safe = ($Lane -replace '[^A-Za-z0-9._-]', '-').ToLower()
$branch = "agent/$safe"
$path = Join-Path $Root "wt-$safe"
$repo = (git rev-parse --show-toplevel).Trim()

if (-not (Test-Path $path)) { throw "lane worktree not found: $path" }

Push-Location $path
try {
  $dirty = git status --porcelain
  if ($dirty) {
    throw "lane '$safe' has uncommitted changes; agent must commit before merge:`n$($dirty -join "`n")"
  }
  $behind = git log --oneline "$Target..HEAD"
  if (-not $behind) { Write-Output "NOTHING-TO-MERGE lane=$safe" }
  else {
    Write-Output "COMMITS:`n$($behind -join "`n")"
    Push-Location $repo
    try {
      git merge --ff-only $branch
      if ($LASTEXITCODE -ne 0) {
        throw "fast-forward merge failed; resolve on target branch manually"
      }
      Write-Output "MERGED branch=$branch into $Target"
    }
    finally { Pop-Location }
  }
}
finally { Pop-Location }

git -C $repo worktree remove $path --force
git -C $repo branch -D $branch | Out-Null
Write-Output "CLEANED path=$path branch=$branch"
