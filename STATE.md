# STATE.md — what is actually measured about VOD.RIP right now

**Read this file's rules before quoting anything from it.**

Every number below was produced by a command on this machine, at the timestamp
stated, with the population and window stated. A number that has not been
measured is written as **unmeasured**, with the reason. Nothing here is carried
over from another document: where a prior figure existed, it was re-derived or
it is listed under "FACTS THAT WERE WRONG" as retracted.

The reason this file exists: a prior STATE-style list in this project carried
four wrong numbers for hours, and reporting a point-in-time `200` as "healthy"
is how they survived.

- Repo: `VOD.RIP` · main at `b789bba` · this file written from worktree `agent/obsv`
- Written: 2026-10-05 (local, UTC-3)
- Instruments: `scripts/observability/probe.py`, `scripts/verify.ps1`

---

## 1. The live app: latency against a budget

**Instrument:** the continuous probe running against the live API at
`http://127.0.0.1:7897` (the earlier `tmp/liveness_probe.py` draft, whose sample
log this tool reads).
**Window:** the 240 minutes ending `2026-10-05T05:31:11Z`.
**Command:**

```powershell
python scripts/observability/probe.py --report-only --window-minutes 240 `
  --jsonl "<repo>/tmp/liveness.jsonl" --verdict tmp/draft-window.json
```

| check | budget | attempts (n) | answered | p50 | p95 | max | over budget | verdict |
|---|---|---|---|---|---|---|---|---|
| `GET /api/health` | 1000 ms | **94** | 89 | **15.0 ms** | **59.6 ms** | **10,891.0 ms** | 1 | **FAIL** |
| `POST /api/preview/session` (real YouTube id) | 3000 ms | **40** | 20 | **16.0 ms** | **106.0 ms** | **922.0 ms** | 0 | PASS |

5 of 94 health attempts and 20 of 40 preview attempts never reached the endpoint
at all; those are `not_measured`, and they are excluded from the latency
percentiles rather than counted as 0 ms.

### What this means, and the trap in it

The health check is **red**, and it is red on a single sample: a **10,891 ms**
response inside a window whose p50 is 15 ms. A tool that samples once and sees
`200` reports this app as healthy. That is the exact inversion that produced
four wrong numbers, and it is why the verdict here is FAIL.

The preview check passes on latency **for the 20 attempts that answered**. It is
not evidence that previews are reliable: **half of the attempts (20/40) never
reached the endpoint**, which is a reachability problem, not a speed problem, and
a speed verdict cannot speak to it.

### Gaps detected in the sampling itself

`unreachable_window` events, same 240-minute window — real, not synthetic:

| check | duration | expected max |
|---|---|---|
| health | **346.8 s**, **255.2 s**, **71.4 s** | 60.0 s |
| preview | **344.8 s** | 270.0 s |

These are intervals with no sample at all. They are the shape of the incident
this substrate exists for: a live-stream heartbeat went stale for ~18 h and was
reported as an unexplained owner mystery, because nothing was watching and
nothing recorded the silence. The draft probe does not record which process was
serving, so a genuine crash/restart inside these windows **cannot be
distinguished from a starved probe** — see §3.

---

## 2. The gate

**Instrument:** `pwsh -NoProfile -File scripts/verify.ps1` · **Window:** single
runs, 2026-10-05, times per row noted.

| check | result | measured cost | population |
|---|---|---|---|
| `tsc --noEmit` | **exit 0**, 0 bytes of output | 6.5 s / 8.2 s / 15.6 s | 3 runs |
| `vitest run` | **exit 0** | 36.5 s / 42.1 s / 45.0 s | 3 runs |
| probe + gate unit tests | **74 passed**, 0 failed | 10.1 s | 1 run |
| Plugin self-test | **21 arms passed**, 0 failed | < 2 s | 1 run |
| backend suite | see §5 | — | — |

`node_modules` population, counted two independent ways that now agree:
**10,850 files** (PowerShell `Get-ChildItem -Recurse -File`; and the gate's own
`count_files`, which follows directory junctions). Floor for trusting
`tsc`/`vitest` is 500 files.

---

## 3. Unmeasured — written as unmeasured

| field | state | why |
|---|---|---|
| **crash / restart detection on the live app** | **unmeasured** | The running draft probe records no `pid`/`start_time`, so the 346 s and 255 s gaps in §1 cannot be attributed to a crash versus a starved probe. `probe.py` records `(pid, start_time)` on every sample and emits a `restart` event when the pair changes; it is **verified by unit test, not yet evidenced on the live app** — it needs the promoted probe to run for longer than one restart. |
| **Vite proxy `ECONNRESET` on `/api/settings`** | **unmeasured** | Nothing in this substrate watches the Vite log. |
| **stale live-stream heartbeat** | **unmeasured** | No heartbeat check exists in this substrate. |
| **preview reliability over a long window** | **partially measured** | 20/40 attempts unreachable in the 240-minute window (§1); cause not identified. |
| **whether the `Plugin` hook fires in a real session** | **unmeasured for this Plugin** | The hook is installed and its logic is verified by 21 self-test arms, but this Plugin's own ledger has no records from a real session boundary yet. It activates on the next MiniMax Code process start. See §6 for the exact command that answers it. |

**A field that never varies is decaying, not healthy.** The health p50 has sat
at 0.0–15.0 ms for the whole window and p95 at 59.6 ms, while the max moved
10891 ms. A metric whose floor never moves is not tracking the app's behaviour;
it is only tracking the loopback. Treat the floor as insensitive and the tail as
the signal until a longer window says otherwise.

---

## 4. Environment facts that were re-derived, not assumed

| fact | value | instrument |
|---|---|---|
| serving process for `:7897` | **pid 39848**, started `2026-10-05T04:57:40Z` | `netstat -ano` for the pid, `kernel32.GetProcessTimes` for the start time; cross-checked against WMI `CreationDate`, agreeing to the second on 3 pids |
| live archive | `H:\VOD.RIP-data\archive.db` | the path `probe.py` opens, `mode=ro` |
| real video id used by the preview check | `hVeReEg5f5c` (channel `whindersson`, 622 s) | `SELECT ... FROM videos WHERE platform='youtube' ORDER BY rowid DESC LIMIT 1`, read-only, from the live path |
| orphaned archives, refused by name | `%APPDATA%\VOD.RIP\archive.db`, `G:\VOD.RIP-data\archive.db` | `probe.ORPHAN_ARCHIVES` |

**No table counts from any `archive.db` appear in this file on purpose.** The
live database keeps growing, the two orphans were each read by an agent and
reported as the archive, and three wrong reports came out of that. The selected
video id is recorded; row totals are not, because they were not re-derived here.

---

## 5. Backend suite

Result recorded by the gate run whose log is `tmp/verify-logs/pytest_backend.log`.
Population and exact count: see that log; the authoritative line is the pytest
summary, not this file.

> Filled in from the run recorded below.

---

## 6. Re-verification on change — what exists and what is proven

A MiniMax Code **Plugin** at
`C:\Users\Administrador\.minimax\plugins\vodrip-verify-on-stop\` registers
`Stop` and `SubagentStop` hooks that spawn `scripts/verify.ps1` whenever an agent
session ends in a VOD.RIP checkout or worktree.

**That the runtime supports this is measured, not assumed.** The neighbouring
`mcode-dispatch-guard` Plugin records every invocation to
`C:\Users\Administrador\.minimax\v2\plugin-data\hooks\mcode-dispatch-guard\invocations.jsonl`:

| event | records | window |
|---|---|---|
| `Stop` | 1084 | 2026-10-04T03:46:30Z → 2026-10-05T05:08:16Z |
| **`SubagentStop`** | **736** | same window |
| `PreToolUse` | 178 | same window |
| `SubagentStart` | 136 | same window |
| **total** | **2134** | same window |

The most recent `SubagentStop` record is `2026-10-05T05:08:03Z`. The event this
substrate needs has fired 736 times on this host.

**The mechanical liveness question for the new Plugin** (a green self-test is
evidence about a script, not about a hook firing):

```powershell
$p = "$env:USERPROFILE\.minimax\v2\plugin-data\hooks\vodrip-verify-on-stop\invocations.jsonl"
if (Test-Path $p) { $l = Get-Content $p; "records $($l.Count)"; $l | Select-Object -Last 5 } else { "NO LEDGER - the hook never ran" }
```

**A measured trap, recorded because it fails silently.** The hook cannot use
`detached: true` when spawning the gate. Measured on this host: with
`detached: true` the spawned PowerShell exited **0** in under a second with **no
output and no gate run**, while the hook had already logged `action: "spawned"`
with a real pid. With `detached: false, stdio: "ignore"` + `unref()` the gate ran
to completion and survived the hook's exit. **So `spawned` in the ledger is not
evidence of a run — the verdict file is.**

---

## FACTS THAT WERE WRONG, do not reintroduce

Each of these was published and had to be retracted. Re-derive before use.

1. **"`/api/health` returned 200, so the app is healthy."** Measured false in this
   very window: p50 15.0 ms, **max 10,891.0 ms**, 5 of 94 attempts unreachable.
   A 200 is a sample. The verdict is FAIL.
2. **"`SubagentStop` has never been observed firing on this host."** This is in
   the `mcode-dispatch-guard` README and is now **false**: 736 records, most
   recent `2026-10-05T05:08:03Z` (§6). Do not cite the README's claim.
3. **"node_modules holds 709 files."** That was `os.walk` refusing to descend into
   the worktree's junction. PowerShell counted **10,850** for the same directory
   at the same second. The gate now follows junctions and both agree.
4. **"A green self-test proves the gate works."** It does not. A self-test that
   cannot go red is decoration; the mutation arms in
   `scripts/observability/tests/` and the closed-port selftest arm exist to make
   it go red.
5. **"vitest passed."** In one full-gate run on 2026-10-05 vitest reported
   `Test Files 1 failed (1) / Tests no tests` — every fork worker failed to
   start under concurrent load. The gate graded that **FAIL** rather than
   reporting a green, which is the behaviour that matters. Three uncontended
   runs measured exit 0 at 36.5 s / 42.1 s / 45.0 s.
6. **Retracted figures from the previous project state list** — a "27,631 h
   remaining" that was in fact the **total** library hours, and a claim that an
   encoding bug existed which measurement later disproved. **I have not
   re-derived either number**, so neither appears in this file. They are listed
   here only so they are not re-published. Re-derive from the live archive before
   quoting anything in that family.
