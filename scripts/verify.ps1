<#
.SYNOPSIS
  VOD.RIP verification gate. ONE command, ONE honest exit code.

.DESCRIPTION
  Runs the whole gate and exits with a real code:
    1. the live liveness/latency probe verdict (real URLs, real video id)
    2. tsc --noEmit
    3. vitest run
    4. the probe's own unit tests
    5. the backend pytest suite, ONLY when no other pytest is live

  THE RULES THIS ENFORCES, each one bought with a real false green in this repo:

  * Never pipe a native command when you need its exit code. Every native call
    here is redirected to a file and the code is read from $LASTEXITCODE. A
    `cmd | Select-Object -First N` masks the code and has already produced two
    false "green" results in this project.

  * node_modules can exist and be EMPTY. It is counted before tsc/vitest are
    trusted. A previous agent reported "884 FE tests passed" against an empty
    tree.

  * ONE pytest suite at a time. The guard below is the house rule verbatim; a
    live pytest makes the backend suite not_measured with that reason, and the
    gate does NOT fail for it.

  * An unmeasured check is never a pass. It prints as NOT_MEASURED, it is listed
    in the verdict file, and -Strict turns it into exit 3.

.PARAMETER Strict
  not_measured exits 3 instead of 0.

.PARAMETER Fast
  Skip every pytest step (probe + tsc + vitest only).

.PARAMETER SkipProbe
  Do not call the live app. The probe becomes not_measured - it is NOT a pass.

.EXAMPLE
  pwsh -NoProfile -File scripts/verify.ps1

.EXAMPLE
  pwsh -NoProfile -File scripts/verify.ps1 -Strict

.NOTES
  Exit codes: 0 all pass · 1 something failed · 3 -Strict and something
  not_measured · 4 the runner itself could not run.
#>
[CmdletBinding()]
param(
  [switch]$Strict,
  [switch]$Fast,
  [switch]$SkipProbe,
  [string]$Repo = '',
  [string]$Verdict = '',
  [string]$Api = 'http://127.0.0.1:7897',
  [double]$HealthBudgetMs = 1000,
  [double]$PreviewBudgetMs = 3000,
  [double]$WindowMinutes = 30
)

$ErrorActionPreference = 'Stop'

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot = if ($Repo) { $Repo } else { Split-Path -Parent $here }
$verdictFile = if ($Verdict) { $Verdict } else { Join-Path $repoRoot 'tmp\verify-verdict.json' }
$python = 'C:\Program Files\Python311\python.exe'
if (-not (Test-Path $python)) {
  $python = (Get-Command python -ErrorAction SilentlyContinue).Source
}
if (-not $python) {
  Write-Output 'FATAL: no python interpreter found (looked for C:\Program Files\Python311\python.exe then PATH)'
  exit 4
}

# --------------------------------------------------------------------------
# PRECHECK 1 - one pytest at a time. The house rule, verbatim.
# --------------------------------------------------------------------------
$livePytest = @(Get-CimInstance Win32_Process -Filter "Name like '%python%'" |
  Where-Object { $_.CommandLine -match 'pytest' })
$livePytestExcludingSelf = @($livePytest | Where-Object {
  $_.CommandLine -notmatch 'verify_gate\.py|verify\.ps1'
})
$env:VODRIP_LIVE_PYTEST = $livePytestExcludingSelf.Count
Write-Output ("PRECHECK live pytest processes (excluding this gate): {0}" -f $livePytestExcludingSelf.Count)
foreach ($p in $livePytestExcludingSelf) {
  # Bounded output, and NOT via a pipe into a truncating cmdlet: a truncated
  # list here would hide the very process the guard exists to notice.
  $cmd = $p.CommandLine
  if ($cmd.Length -gt 160) { $cmd = $cmd.Substring(0, 160) + ' ...' }
  Write-Output ("  pid {0}  {1}  {2}" -f $p.ProcessId, $p.CreationDate, $cmd)
}
if ($livePytestExcludingSelf.Count -gt 0) {
  Write-Output 'PRECHECK backend suite will be NOT_MEASURED (skipped: another pytest is live). The gate does not fail for this.'
}

# --------------------------------------------------------------------------
# PRECHECK 2 - node_modules can exist and be empty.
# --------------------------------------------------------------------------
$nmPath = Join-Path $repoRoot 'node_modules'
$nmFiles = -1
if (Test-Path $nmPath) {
  # Counted, not probed. A directory that merely exists proves nothing: a prior
  # lane reported "884 FE tests passed" against an empty node_modules.
  $nmFiles = @(Get-ChildItem -LiteralPath $nmPath -Recurse -File -ErrorAction SilentlyContinue).Count
  Write-Output ("PRECHECK node_modules files: {0}" -f $nmFiles)
  if ($nmFiles -lt 500) {
    Write-Output 'PRECHECK node_modules looks empty; tsc and vitest will be NOT_MEASURED, not passes.'
  }
} else {
  Write-Output 'PRECHECK node_modules missing; tsc and vitest will be NOT_MEASURED, not passes.'
}
$env:VODRIP_NODE_MODULE_FILES = $nmFiles

# --------------------------------------------------------------------------
# RUN the gate. Redirect to a file, read $LASTEXITCODE. Never pipe.
# --------------------------------------------------------------------------
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $verdictFile) | Out-Null
$stdoutLog = Join-Path (Split-Path -Parent $verdictFile) 'verify-gate.out'
$stderrLog = Join-Path (Split-Path -Parent $verdictFile) 'verify-gate.err'

$gateArgs = @(
  (Join-Path $here 'observability\verify_gate.py'),
  '--repo', $repoRoot,
  '--verdict', $verdictFile,
  '--api', $Api,
  '--health-budget-ms', $HealthBudgetMs,
  '--preview-budget-ms', $PreviewBudgetMs,
  '--window-minutes', $WindowMinutes
)
if ($Strict)    { $gateArgs += '--strict' }
if ($Fast)      { $gateArgs += '--fast' }
if ($SkipProbe) { $gateArgs += '--skip-probe' }

& $python @gateArgs 1> $stdoutLog 2> $stderrLog
$gateExit = $LASTEXITCODE

Get-Content $stdoutLog
$errText = Get-Content $stderrLog -Raw -ErrorAction SilentlyContinue
if ($errText -and $errText.Trim().Length -gt 0) {
  Write-Output ''
  Write-Output '--- gate stderr ---'
  Write-Output $errText.Trim()
}
Write-Output ''
Write-Output ("verify.ps1: gate exit code {0} (stdout {1}, stderr {2})" -f $gateExit, $stdoutLog, $stderrLog)

# Propagate the real code. This is the only exit path.
exit $gateExit
