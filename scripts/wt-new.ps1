<#
.SYNOPSIS
  Create a private git worktree lane for a parallel agent.

.DESCRIPTION
  Parallel writers MUST NOT share the main tree. Each lane gets its own worktree
  under I:\TEMP\wt-<lane> with branch agent/<lane>. The main checkout stays clean
  and the manager merges lanes explicitly with wt-merge.ps1.

.EXAMPLE
  .\scripts\wt-new.ps1 -lane frame-snap -base main
#>
[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)][string]$Lane,
  [string]$Base = 'main',
  [string]$Root = 'I:\TEMP'
)

$ErrorActionPreference = 'Stop'

$repo = (git rev-parse --show-toplevel).Trim()
if (-not $repo) { throw 'not inside a git repository' }

# normalise lane into a safe name
$safe = ($Lane -replace '[^A-Za-z0-9._-]', '-').ToLower()
$branch = "agent/$safe"
$path = Join-Path $Root "wt-$safe"

if (Test-Path $path) {
  Write-Output "EXISTS path=$path"
} else {
  git -C $repo worktree add -b $branch $path $Base
  if ($LASTEXITCODE -ne 0) { throw "worktree add failed for lane $Lane" }
  Write-Output "CREATED path=$path branch=$branch"
}

# node_modules / build caches are shared by junction to avoid a multi-GB install per lane
$nm = Join-Path $path 'node_modules'
if (-not (Test-Path $nm)) {
  $src = Join-Path $repo 'node_modules'
  if (Test-Path $src) {
    cmd /c "mklink /J `"$nm`" `"$src`"" | Out-Null
    Write-Output "JUNCTION node_modules -> $src"
  }
}

Write-Output "LANE=$safe"
Write-Output "PATH=$path"
Write-Output "BRANCH=$branch"
