# VOD.RIP observability substrate

Two tools, one command, and a set of rules that exist because this project has
already published false greens.

| file | what it is |
|---|---|
| `probe.py` | continuous liveness + latency probe against **real** URLs and a **real** video id |
| `probe_core.py` | the judgement layer: verdicts, budgets, restarts, gaps, percentiles. No I/O, so it is unit-testable |
| `verify_gate.py` | the gate: probe verdict, `tsc`, `vitest`, probe tests, backend suite — with a real exit code |
| `../verify.ps1` | the one command. PowerShell entry point; owns the one-pytest-at-a-time guard |
| `tests/` | 69 tests, including mutation arms that prove the gate can go red |

## The one command

```powershell
pwsh -NoProfile -File scripts/verify.ps1              # the gate
pwsh -NoProfile -File scripts/verify.ps1 -Strict      # not_measured is red too (exit 3)
pwsh -NoProfile -File scripts/verify.ps1 -Fast        # probe + tsc + vitest, no pytest
pwsh -NoProfile -File scripts/verify.ps1 -SkipProbe   # offline gate; probe is NOT a pass
```

Exit codes: **0** all pass · **1** something failed · **3** `-Strict` and
something `not_measured` · **4** the runner itself could not run.

The machine-readable answer is always `tmp/verify-verdict.json`:

```powershell
Get-Content tmp/verify-verdict.json -Raw | ConvertFrom-Json |
  Select-Object verdict, exit_code, generated_at
```

## The probe on its own

```powershell
# continuous (the daemon): samples forever, refreshes the verdict every cycle
python scripts/observability/probe.py

# one fresh sample of each check, grade the window, exit
python scripts/observability/probe.py --once

# grade an existing window without touching the app
python scripts/observability/probe.py --report-only

# bounded run
python scripts/observability/probe.py --duration-s 45

# prove the gate bites: point a check at a closed port
python scripts/observability/probe.py --selftest-arm
```

Budgets (defaults, both configurable):

| check | budget | where the number comes from |
|---|---|---|
| `/api/health` | **1000 ms** | the degraded call measured **10,891 ms**; a warm loopback call measures 0–15 ms, so 1 s is ~60× the warm floor |
| `/api/preview/session` | **3000 ms** | a real YouTube preview took **22,875 ms** server-side when degraded and 32–922 ms warm |

```powershell
python scripts/observability/probe.py --health-budget-ms 1500 --preview-budget-ms 5000
```

## The honesty rules, and what each one is for

These are the whole point. Do not "simplify" one away.

1. **A point-in-time 200 is not health.** `summarise` labels a single sample
   `SINGLE SAMPLE, this is a point-in-time reading, not a distribution`. The
   measured history: `/api/health` returned 200 in **10,891 ms**, then 200 in
   **0.0 ms** thirty seconds later. Reporting either as "healthy" is how four
   wrong numbers survived for hours.
2. **Unreachable is its own state — `not_measured`, never a pass, never 0 ms.**
   A transport failure has no `status`, so no measurement exists. `classify`
   returns `not_measured` with `reason_class: "unreachable"`, and the latency
   distribution only ever includes samples that actually answered.
3. **Over budget is a `fail`, not a note.** The budget a sample was judged
   against is recorded on the sample and in the verdict file, so "slow" is
   always attributable to a number.
4. **`n = 0` is `not_measured`,** never a 0 ms pass. `percentile([], 50)` is
   `None`, not `0.0`.
5. **A restart is an event, not a guess.** Every sample carries the serving
   process `(pid, start_time)`. A change in that pair is a `restart` — a pid
   alone is not an identity, because Windows recycles pids. An unknown identity
   claims neither continuity nor restart.
6. **A gap is an `unreachable_window` with a duration,** not silence. The
   threshold is 3 missed intervals, so a slow-but-working probe is never
   accused of an outage. An 18 h stale heartbeat was reported as an owner
   mystery for hours; this is what prevents that shape.
7. **Real inputs only.** The preview check POSTs a real YouTube id read from the
   **live** archive, `mode=ro`. The two orphaned `archive.db` files
   (`%APPDATA%\VOD.RIP\archive.db`, `G:\VOD.RIP-data\archive.db`) are refused by
   name — they are the source of wrong numbers in this project. Nothing here ever
   writes the database.
8. **The probe is never `pass` because it ran.** The gate reads the probe's
   *verdict file*, not its exit code, because the probe exits 0 whenever it
   managed to grade at all.

## Rules the runner enforces, each bought with a real false green

- **Never pipe a native command when you need its exit code.** Every native call
  is redirected to a file and the code is read from `$LASTEXITCODE` /
  `returncode`. A `cmd | Select-Object -First N` masks the code; that has already
  produced two false "green" results here. There is a test that runs a command
  which prints 1,000 lines and exits 7, and asserts it is still caught.
- **`node_modules` can exist and be EMPTY.** It is counted before `tsc`/`vitest`
  are trusted (floor: 500 files). A previous agent reported "884 FE tests passed"
  against an empty tree. The counter follows directory **junctions**, because a
  worktree's `node_modules` is one — measured: `os.walk` saw 709 files where
  PowerShell saw 10,850 for the same directory, and 709 is close enough to the
  floor that the two counters could disagree across a safety threshold.
- **One pytest suite at a time.** Before the backend suite,
  `Get-CimInstance Win32_Process -Filter "Name like '%python%'" | Where-Object {
  $_.CommandLine -match 'pytest' }` decides. A live pytest makes the suite
  `not_measured` with that reason and the gate does **not** fail for it. A guard
  that cannot be evaluated does not authorise a run.
- **The gate validates its own output.** `validate_verdict` re-reads the
  document it just wrote and exits 4 if a verdict has no population, no reason,
  an unknown token, or a `pass` with nothing measured. A gate that cannot check
  itself is the original problem in miniature.

## Re-verification that fires on change

`Stop` and `SubagentStop` Plugin hooks in
`C:\Users\Administrador\.minimax\plugins\vodrip-verify-on-stop\` run this gate
automatically when a lane ends. See that plugin's `README.md` for the ledger
(the liveness question), the three anti-stampede guards, and how to disable it.

## Tests

```powershell
python -m pytest scripts/observability/tests -q
```

69 tests. The ones worth reading first:

- `test_unreachable_does_not_pollute_the_latency_distribution`
- `test_percentile_of_an_empty_population_is_none_not_zero`
- `test_a_command_that_cannot_start_is_not_measured_not_pass`
- `test_selftest_arm_goes_red_if_a_probe_lied_about_an_unreachable_port`
- `test_empty_repo_gate_is_all_not_measured_and_exits_zero`
