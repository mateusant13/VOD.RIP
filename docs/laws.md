# Laws — checks that must be able to fail

A **law** in this repo is a check that is measured twice: once on the tree as it
is, and once with a mutation that is supposed to break it. A law that passes both
times has told us nothing. The runner that does this is
[`scripts/laws/lawrunner.py`](../scripts/laws/lawrunner.py) and it is wired into the
one verification gate, [`scripts/verify.ps1`](../scripts/verify.ps1) →
[`scripts/observability/verify_gate.py`](../scripts/observability/verify_gate.py).

The three laws this repo currently ships are listed at the bottom with the
pinned numbers each one asserts.

## Why this exists

The gate has always been able to answer "is it green?". It could not answer
"would any check have cried out if the thing it guards were broken?". A guard
that cannot fail is a decoration: it prints green, the owner reads green, and a
regression lands. Two of the laws below guard defects that **already happened in
this repo** and were reported as working: a preview deadline message spelled as a
literal at its call site, and a first-output ceiling above the stage total that
collapses the stalled/timeout distinction into one error box. Both looked fine
while broken.

The rule this file enforces: **an unmeasured check is never a pass, and a law
that cannot fail is not evidence.**

## The protocol

Each law runs as its own process, twice, from the repo root:

```text
clean arm   VODRIP_LAW_CWD=<repo root>   VODRIP_LAW_SCRATCH=<scratch>/<name>.clean
red arm     VODRIP_LAW_CWD=<repo root>   VODRIP_LAW_SCRATCH=<scratch>/<name>.red
            VODRIP_LAW_RED_INPUT=<path to the red JSON>      # set only here
```

Both arms also get `VODRIP_LAW_NAME` and `VODRIP_LAW_EXPECT_RC`. The red input is
a JSON document of literal text substitutions;
[`lawchecks.py`](../scripts/laws/lawchecks.py) applies it to an **in-memory copy**
of the source tree, so the red mutation never touches the working tree. The
checker judges the image in memory, and the caller tree stays clean. This is the
part that makes the discipline cheap enough to run on every gate pass: no
`git stash`, no restore step, no risk of leaving a red tree behind.

A checker answers with one word and one reason. Exit codes:

| word | exit | meaning |
|---|---|---|
`PASS` | 0 | the tree satisfies the law, and the red input was applied and the law still held (see below) |
`FAIL` | 1 | the tree violates the law |
`NO_VERDICT` | 2 | could not measure — no verdict, never a pass |

and exactly one stdout line, ASCII only, `PYTHONIOENCODING=utf-8`:

```text
law-verdict: PASS <law-name> | <reason>
```

Runner exit codes: `0` all pass, `1` a law failed, `3` unusable runner or
contract violation, `4` the report could not be trusted (no usable rows).
`3` and `4` fold into the gate as **failed**, not as anything softer: a gate
whose law step cannot be read does not get to look green.

## How the two words combine into a verdict

| clean word | red word | verdict | why |
|---|---|---|---|
 PASS | RED | **PASS** | the law fires on clean and on red: it can fail, and it did |
 PASS | PASS | **RED** (red not reproduced) | the mutation did not break it: the green is unearned |
 FAIL | RED | **RED** | the law fails on clean: a real defect, in red, by design |
 FAIL | PASS | **MISMATCH** | fails clean but not red: the checker is not reading what it claims |
 ? | NO_VERDICT | **NO_VERDICT** | the red arm declined: green is unearned, not skipped |

`MISMATCH` is the honest case and it is the one worth reading: it means the
checker's inputs and its assertions are not connected. It counts as a failure
in the gate, so it cannot be ignored — but its reason names the repair.

## Exit-code discipline

Each law declares `expect_rc` (normally `0`). The runner also records the real
process exit code of the clean arm. A word is only trusted when it agrees with
the code the process actually returned:

- clean word `PASS` **and** real rc == `expect_rc` → pass;
- real rc **≠** `expect_rc` → **FAIL `law_word_contradicts_rc`**, whatever the
  word said. A checker that prints `PASS` and returns `3` has not passed;
- real rc == `expect_rc` but `expect_rc` ≠ `0` → `NOT_MEASURED
  `law_pass_not_measured` — "an unearned pass is not a pass". A law
  configured to exit `0` on failure has not measured anything.

The same rule is what makes the runner's own refusals loud: over the
`MAX_LAWS` cap, a law with no red input, or an unreadable red document is
reported per row, not silently dropped.

## What the runner refuses to do (fail-closed, by construction)

| situation | verdict |
|---|---|
red-input file missing | `NO_VERDICT` — "the red arm is absent" |
red-input file unreadable | `NO_VERDICT` |
clean arm never produced a verdict | `NO_VERDICT` |
red arm never finished (killed, or failed to start) | `NO_VERDICT` — an arm nobody observed is not a mismatch (lawrunner.py:887-896) |
red arm printed no verdict word | `NO_VERDICT` — red not evaluated (lawrunner.py:912-925) |
red arm ran but the mutation did not reproduce | `MISMATCH` — the law is unfalsifiable, so its green is not evidence (lawrunner.py:936) |
red word is `NO_VERDICT` | `NO_VERDICT` — "the red arm declined to measure, so this law's green is unearned" |
the run window closed before an arm started | `NO_VERDICT` — `window_closed` (checked at the loop, lawrunner.py:1043, twice at the spawn seam: 701-706 and again immediately before Popen, 727-732) |
unknown verdict word | `NOT_MEASURED` (fail-closed — never mapped to PASS) |
a red mutation replaces text with itself | `NO_VERDICT` — a mutation that cannot change anything would measure the clean tree (lawchecks.py:246-254) |
a checker returns without printing a verdict | `NO_VERDICT` — the checker refuses its own silent exit 0 (lawchecks.py:126-132) |
a checker is handed an unknown verdict word | refused **before** anything is printed, so there is never a second line (lawchecks.py:99-105) |
a stored sample line is not JSON | `NO_VERDICT` — the mirror cannot be computed over a file it only partly read (check_probe_concurrent_platforms.py:180-205) |

## Writing a law

1. Pick a behaviour whose regression would **silently** pass every healthy
   probe. If it would show up as a red test or a thrown error, it does not need
   a law.
2. Write a checker that uses [`lawchecks.py`](../scripts/laws/lawchecks.py):
   `repo_root()` for the tree, `load_tree()` for the in-memory image,
   `findings_by_file()` / `fmt_findings()` for output, `apply_red_input()` to
   force the mutation, `run()` so that any exception becomes `NO_VERDICT`
   rather than a crash.
3. Write the red input as `{"mutations": [{"file", "find", "put", "expect"}]}`
   with `expect` an integer ≥ 1 and `file` repo-relative and present in the tree.
   `find` must occur exactly `expect` times or the law reports "no longer
   applies" — that is the runner noticing its own mutation went stale.
4. Add one entry to [`scripts/laws/laws.vodrip.json`](../scripts/laws/laws.vodrip.json):`<python>` expands to the running interpreter
   (`sys.executable`), paths are relative to the repo root, and every law needs
   a `name`, a `cmd` list, `expect_rc`, and a `red_input`.
5. Add the law to [`scripts/observability/tests/test_laws.py`](../scripts/observability/tests/test_laws.py) so the registry and the red fixtures are
   covered by the same suite that runs in the gate.

Then run it, run the gate, and confirm the fold line says the law count you
expected:

```powershell
$env:PYTHONIOENCODING='utf-8'; $env:PYTHONUTF8='1'
& 'C:\Program Files\Python311\python.exe' scripts/laws/lawrunner.py
```

## The three laws that ship

All three are declarative, all three read only, all three judge an in-memory
image. Each red fixture lives in [`scripts/laws/red/`](../scripts/laws/red).

### 1. `preview-deadline-call-sites-resolve-to-table`

**Claim:** every site that fires a preview-start deadline resolves its visible
wording from `PREVIEW_DEADLINE_MESSAGE_KEYS` **by asking for a key** — no call
site may spell a deadline sentence, and the guard must be both constructed and
consulted.

It loads `src/` (skipping `*.test.ts`, which are mirrors, not sources),
classifies each occurrence of a deadline literal as orphan or table-arm, and
requires `src/App.tsx` and `src/ChannelExplorePopup.tsx` to still arm the guard.
Red fixture `red/deadline-call-site-literal.json` moves one arm at
`check_preview_deadline_key.py:86` into a spelled-out literal.

This is a law, not a test, because the failure mode it guards is a **string that
looks right in code review**. The user-visible effect of the regression is that
one window retries with wording from the wrong table while every probe, every
test and the mouse still see a working retry.

### 2. `preview-ceiling-order-first-output-under-stage-total`

**Claim:** for every preview stage, `FIRST_OUTPUT_CEILING_MS <= TOTAL_CEILING_MS`.

If the first-output ceiling is above the stage total, `previewDeadlineFire`
fires the **total** deadline first and the stalled-vs-timeout distinction
collapses into one box. The app still shows an error, still retries, and every
healthy probe still passes.

The checker asserts five things: both tables declared exactly once, same stage
keys in both, literal positive millisecond values, the order relation per stage,
and that `previewDeadlineFire` actually reads both tables. The companion
mirror test [`src/previewRetryCeilings.test.ts`](../src/previewRetryCeilings.test.ts)
exhaustively grids the same relation — 2 stages × 9 arrivals × 8 elapseds
= 144 rows against an independent oracle — so the law text and the tested
behaviour are anchored to each other.

### 3. `probe-concurrent-platforms-printout-is-measured`

**Claim:** the concurrent-platforms check in the observability probe is real and
honest: declared in `probe.py`, sampled by the offline run, and the printed
document equals what the trusted `probe_core.summarise` says about the **same
samples**, with every platform arm recorded and an unreachable arm reported as
`not_measured` — never as a pass with empty percentiles.

This law runs the probe itself offline (a synthetic archive and a closed port),
which is why it carries the largest timeout. Its red fixture makes an
unreachable arm print as a pass.

## The Bend layer is deferred, and here is why

The plan for this repo once described a **Bend** layer — a second stage above
the laws, where a law in one language could be re-expressed as a pure function
in another and cross-checked for agreement. That work is **deferred**, and the
reason is measured rather than speculated.

**1. The toolchain is not on this box.** `bend`, `cbend` and `hvm` are all
absent from `PATH`. A gate stage that shells out to a compiler nobody has
installed is a stage that cannot run.

**2. A missing compiler is indistinguishable from a red law — and both would
read as "not measured".** That is fine for one stage. It is not fine for a
stage whose whole job is to be the trustworthy one. The runner is built so an
unusable toolchain produces exit `3` and folds to FAILED rather than passing;
shipping the same runner with a dependency that is absent by default would
produce exactly the "law suite is red, ignore it" posture the gate exists to
prevent.

**3. Portability is the actual requirement.** The checkers here are stdlib-only
Python that run under both this box's `C:\Program Files\Python311\python.exe`
and a stock `python3`, with `PYTHONIOENCODING=utf-8` set by the caller. Bend
would add a language runtime to every clone of the repo for a benefit that has
not been demonstrated on any of the three laws above.

**What would end the deferral:** a demonstrable bug that the Bend layer would
catch and the current one cannot, or a measured need to check the same
invariant across two languages. Neither exists today. The note stays in the plan
documents as deferred-with-reasons, not silently dropped — see
[`todo.md`](../todo.md).

## What the numbers were, when this file was written

Measured on 2026-10-11, all green, from the repo root:

| what | result |
|---|---|
`vitest run src/previewRetryCeilings.test.ts` | **24 passed**, exit 0 |
7 targeted mutations of `src/previewRetry.ts` | **7 RED**, source restored byte-identical |
`tsc --noEmit` | exit 0 |
`pytest scripts/observability/tests` | probe-core 47 + laws 63 + gate 34 = **144 passed** |
law suite, two arms × three laws | **3 PASS** |
gate exit code (negative fixture: a law that cannot fail) | **1**, reported FAILED |

The last row is the one that makes the rest meaningful: the gate was pointed at
a law whose red arm deliberately cannot fail, and it failed the gate with

```text
failed: ["law.negative-mutation-is-a-noop"]
exit_code: 1
```

That is the whole discipline in one line: a law that cannot fail does not
produce a pass, it produces a failed gate.

## Re-measured the same day, after the probe and preview fixes

The rows above were re-measured on 2026-10-11, after two product fixes (the
probe fixture picker and the YouTube playability classification) and one
runner fix. A number that predates a change to the thing it measures is not
evidence about that change, so the whole set was re-run:

| what | result |
|---|---|
| pytest scripts/observability/tests | probe-core 47 + laws 74 + gate 34 = **155 passed** in 19.15s, exit 0 |
| law suite, two arms x three laws | **3 PASS**, every red arm RED_CONFIRMED, exit 0 |
| backend suite, full serial run | **2203 passed, 3 skipped, 51 deselected** in 707.05s, exit 0 |

The law suite's own verdict lines from that run (laws=3 arms=6,
verdict=ADMITTED):

```text
LAW-VERDICT PASS preview-deadline-call-sites-resolve-to-table | red=RED_CONFIRMED clean_rc=0 red_rc=1 elapsed=0.187s remaining=899.813s
LAW-VERDICT PASS probe-concurrent-platforms-printout-is-measured | red=RED_CONFIRMED clean_rc=0 red_rc=1 elapsed=12.984s remaining=887.016s
LAW-VERDICT PASS preview-ceiling-order-first-output-under-stage-total | red=RED_CONFIRMED clean_rc=0 red_rc=1 elapsed=13.109s remaining=886.891s
population=3 laws  PASS=3 RED=0 MISMATCH=0 NO_VERDICT=0
```

**Why the suite first returned NO_VERDICT on this box, and what fixed it.**
The runner hands cwd, VODRIP_LAW_SCRATCH and --repo to a child process. It
used to hand out the relative path it was given, and the child resolved that
against its own working directory, so an arm could not find the scratch it
had been handed and reported no verdict word at all. Every path is now
.resolve()d at the seam that hands it out (lawrunner.py:1021-1022, 1288,
1341, 1386). The invariant to keep: a path that crosses a process boundary is
absolute, or it is not a path.

**One live residual, measured and left in place.** probe-concurrent-platforms
first failed at 2985 ms because the probe picked a synthetic video id, YouTube
answered "unavailable", and the arm saw HTTP 500 where it expected a measured
printout. The picker now selects a real row by shape (PICK_CANDIDATE_LIMIT =
200, newest-first) and the classification fix makes a dead video answer 404
with "This video is unavailable." rather than a retryable 500. Both are
covered by the same law: it is the arm that would catch a regression of either.

**The picker's cap is real, and its consequence is bounded.** It scans only the
newest 200 rows per platform (`ORDER BY rowid DESC LIMIT ?`, probe.py:490-496)
and accepts only an id that platform's own extractor can parse. A platform whose
newest 200 rows are all unparseable therefore yields `NOT_MEASURED` rather than a
pass, and the refusal names the scope it searched — "no kick video with a
parseable id in the newest 200 rows of <archive>" (probe.py:509-512). That is a
false negative the check cannot turn into a green; the alternative (scanning the
whole table on every gate run) buys a shorter refusal, not a truer one.

## The window had three seams, and only one of them was checked

The run window is a budget: after `--run-timeout` (default 900s, capped at
3600s) the runner stops *starting* laws. A law that started inside the window is
never killed for overrunning it, and a law that would start outside it is never
started at all.

The check used to live in the loop only. Between that check and the
`subprocess.Popen`, `evaluate_declaration` reads the red-input document, builds
two environments and creates three directories, so a law that passed the check
just inside the window could still be spawned after it closed — which makes the
window decorative for exactly the laws it exists to refuse, and a refusal the
runner never measured is not a refusal. The absolute deadline now travels with
both arms down to the spawn seam and is re-checked there, before the capture
directory is created, and once more immediately before `Popen`
(lawrunner.py:701-706 and 727-732; the loop's own check is lawrunner.py:1043).
The second check exists because the first one is not a check on the spawn: the
capture directory and its two files are created in between. A userspace check
cannot be atomic with process creation, so what is claimed here is exactly what
is measured — the race is the few instructions between the last check and
`Popen`, and nothing wider.

Three regressions pin it, and all three are the same shape — "nothing ran" has
to be measurable, not asserted:

| test | what it measures |
|---|---|
| `test_a_spawn_after_the_deadline_starts_nothing_at_all` | `_spawn(deadline=0.0)` returns `rc=None` and a `window_closed` cause, the file the command would have written does not exist, and the capture directory was never created |
| `test_a_law_that_reaches_its_spawn_after_the_deadline_is_refused` | `evaluate_declaration(deadline=0.0)` refuses both arms (`window_closed` in each cause) and `_capture` holds no arm directory and no `_arm_stdout.txt` |
| `test_a_spawn_refuses_at_the_last_moment_before_the_process_exists` | an injected clock that is inside the window on the first reading and past it on the second: the capture dir exists (so the first check passed), and the process that would have written a marker never started |

**The printout's two numbers came from two different roundings.** `remaining`
was derived from the already-rounded `elapsed`, so a run could print
`remaining=0.0` a moment before the window closed, and the pair could disagree
with itself by a millisecond. Both figures now come from one raw reading of the
clock and the clamp is applied to the raw difference (lawrunner.py:1060-1066),
so a closed window prints `0.0` and never a negative.

**The run's own summary had the same defect in a smaller form.** `elapsed_s`
and `remaining_s_at_end` each called `now()` separately, so the final window
dictionary could report a pair that did not add up to the window it named. Both
now come from one final reading (lawrunner.py:1077-1083).

## Two more claims from a second review, one accepted and one rejected

1. **"A loader `OSError` escapes the runner."** Rejected, and now measured
   rather than read. `discover_law_file` refuses a `--laws` path that is not a
   readable file with `LawError` (lawrunner.py:307); `load_law_file` wraps both
   `read_text`'s `OSError` and its JSON errors into `LawError`
   (lawrunner.py:425-426, 432-433); an unknown CLI argument is refused with
   `EXIT_USAGE` and a `see --help` line (lawrunner.py:1344-1346). The three
   tests that drive those paths are
   `test_a_laws_path_that_is_not_a_file_is_refused_with_a_reason`,
   `test_a_law_file_that_cannot_be_read_is_a_law_error_not_an_oserror` (which
   calls `load_law_file` on a directory, because `discover_law_file` would
   reject it first) and `test_an_unknown_argument_is_a_usage_refusal`.
2. **"`except Exception` around `evaluate_declaration` is too broad."**
   Rejected, with the reason written down instead of the code changed. The loop
   promises that one bad law cannot take the whole run down; without that catch
   an unexpected failure aborts the run and discards the verdicts of every law
   already evaluated. It deliberately does not catch `SystemExit` or
   `KeyboardInterrupt`: the laws run as child processes, so a `SystemExit`
   raised inside a law cannot reach this frame at all, and an operator interrupt
   must abort the run rather than be filed as one law's `NO_VERDICT`. What it
   does catch becomes a `LAW-REFUSED` row carrying the exception in
   `real_cause`, which the gate folds to `NOT_MEASURED` — never to a pass.

## Three claims from an outside review, checked against this code

A review of this directory asserted three things. Each is answered with a line
in this repo, because an assurance that we checked is not a citation:

1. **The red arm may not actually run, or may not be reported.** It runs as its
   own process with VODRIP_LAW_RED_INPUT set, and its printed verdict word is
   mandatory: a red arm that prints no law-verdict line gets red_word = None and
   is forced to NO_VERDICT with RED_NOT_EVALUATED (lawrunner.py:912-925). That
   block also records the false green it was written to kill: before the fix a
   silent red arm fell through to red_rc != expect_rc, was stamped
   RED_CONFIRMED, and the law came out PASS on the strength of an arm that had
   printed nothing at all.
2. **The runner fails open on a refusal.** A refusal raises NoVerdict inside the
   arm, which is exit 2 (lawchecks.py:39, 101) -> the word NO_VERDICT -> the gate
   maps it to core.NOT_MEASURED (verify_gate.py:302), and -Strict turns that into
   exit 3. A word the gate does not recognise is not even that: it becomes
   unknown_verdict_word (verify_gate.py:303-309) and is never a pass.
3. **Silent passes are possible.** There are two independent guards, one on each
   side of the process boundary: the runner refuses a PASS word whose exit code
   contradicts it (lawrunner.py:906-911), and the gate refuses a PASS whose
   numbers do not match (verify_gate.py:319-343) - law_word_contradicts_rc is a
   FAIL and law_pass_not_measured is NOT_MEASURED. The negative fixture in the
   table above is the measurement: a law that cannot fail produced gate exit 1
   with failed: ["law.negative-mutation-is-a-noop"].
## Re-measured again, after the instrument fixes (2026-10-11)

Every number in this file above this section was taken before the probe
instrument and the runner's window were changed. A number that predates a
change to the thing it measures is history, not evidence, so the set was
re-measured against the current revision:

| what | result |
|---|---|
| pytest scripts/observability/tests | probe-core 60 + laws 80 + gate 37 = **177 passed** in 21.96s, exit 0 |
| law suite, two arms x three laws | laws=3 arms=6 verdict=ADMITTED, **3 PASS**, every red arm RED_CONFIRMED, exit 0 (`tmp/laws-post4.txt`) |

The law suite's own verdict lines from that run:

```text
LAW-VERDICT PASS preview-deadline-call-sites-resolve-to-table | red=RED_CONFIRMED clean_rc=0 red_rc=1 elapsed=0.187s remaining=899.813s
LAW-VERDICT PASS probe-concurrent-platforms-printout-is-measured | red=RED_CONFIRMED clean_rc=0 red_rc=1 elapsed=12.937s remaining=887.063s
LAW-VERDICT PASS preview-ceiling-order-first-output-under-stage-total | red=RED_CONFIRMED clean_rc=0 red_rc=1 elapsed=13.078s remaining=886.922s
```

`remaining` is now computed from the same raw clock reading as `elapsed`, which
is why the three lines above add up to the 900s window instead of drifting.


## Two guards that were not guards, and the red arm that proves it (2026-10-11)

A third review round read the probe instrument's own exception handlers, and both
of its findings are reproducible in one line of Python on this box:

```text
int(float("inf"))                                       -> OverflowError
datetime.fromisoformat("9999-12-31T23:59:59").timestamp() -> OSError [Errno 22]
```

`probe_core._as_status` (probe_core.py:97) and `probe._status_code`
(probe.py:525) caught `(TypeError, ValueError)`, so the one status value that
says "no number at all" escaped a function whose entire contract is that it never
raises — and `_status_code` sits on the arm-validation path, where an escaping
exception loses a whole arm. Both now catch `OverflowError` as well
(probe_core.py:106, probe.py:534).

`normalise_record` (probe.py:320) caught the same two types around
`datetime.fromisoformat(ts).timestamp()`. The `OSError` above is neither of
them, and `read_window` wraps its whole read in `except OSError: return []`
(probe.py:370-371) — so **one record with an absurd `ts` emptied the entire
window**, which is precisely the blindness the shim in that function was written
to fix, one line further in. It now catches `OverflowError` and `OSError` too
(probe.py:336).

**The red arm is measured, not asserted.** A mutant copy of the two modules with
the original handlers restored lives in `tmp/mut-probe/observability/`, next to
a copy of the test file:

```text
mutant:  2 failed, 58 deselected
         probe_core.py:105  OverflowError: cannot convert float infinity to integer
         probe.py:335       OSError: [Errno 22] Invalid argument
shipped: 2 passed
```

Both tests fail for exactly the reason they were written, on the same machine, in
the same session — which is what makes their green worth something. The two
regressions are `test_classify_never_raises_on_a_status_of_infinity` and
`test_an_absurd_ts_costs_that_record_not_the_whole_history`.


## The checkers were asked to fail, and four of six claims held (2026-10-11)

A fourth review round read the checker side - `lawchecks.py`, the three checkers
and their red documents - and returned three defects in the shared protocol and
three claims about the checkers themselves. Every claim was re-measured against
this tree before it was acted on.

**Accepted and fixed.**

1. `finish()` printed the verdict line *before* looking the word up in its exit
   table (`lawchecks.py:88`). An unknown word therefore emitted a line the runner
   cannot read and then raised `KeyError`, whose handler in `run()` - `except
   Exception` - printed a **second** line. Two lines is `ambiguous_verdict_word`,
   so the outcome was never a false pass, but the module contract is "exactly one
   line, ever" and it was false. The word is now validated first
   (`lawchecks.py:99-105`).
2. `run()` returned whatever the checker's `main()` returned (`lawchecks.py:112`).
   A checker that returned without printing exited **0**, which a shell reads as
   success. The runner refuses that arm (`no_verdict_word` -> `NO_VERDICT`,
   lawrunner.py:906-911), but the checker is also a thing a human runs by hand, and
   it now refuses its own silent return (`lawchecks.py:126-132`).
3. `apply_red_input` accepted a mutation whose `put` equals its `find`
   (`lawchecks.py:246-254`). That mutation changes nothing, so the red arm would
   measure the clean tree. The runner already refuses the *result* - `red_rc ==
   expect_rc` is `RED_NOT_REPRODUCED`, hence `MISMATCH` (lawrunner.py:928-936) - so
   this was never a false green; the refusal names the actual mistake in the
   document instead of reporting a generically unfalsifiable law.
4. `check_probe_concurrent_platforms._run_probe` skipped any JSONL line that would
   not parse. The law's whole job is to mirror the printed verdict against the
   samples that produced it, so a line it cannot read is evidence lost and the
   mirror was computed over the survivors. It is now `NO_VERDICT`
   (`check_probe_concurrent_platforms.py:180-205`).

**Accepted as a gap in what the law could see, and closed.**

5. The ceiling law compared the two tables against **each other** and against the
   union, but never required the union to be *covered*. Adding a stage to
   `PreviewRetryStage` and to neither table left the law green about a stage the
   law's own HOLDS sentence names. The universe must now be covered by the two
   tables together (`check_preview_ceiling_order.py:193-199`).
6. The deadline law collapsed duplicate stage blocks with `dict(...)` and took the
   first `stalled:`/`timeout:` it found. A duplicate is a key that wins silently,
   so it is now a finding, per stage and per outcome
   (`check_preview_deadline_key.py:185-209`).

**Rejected, with the measurement.**

- "the ceiling checker does not establish that its tables are the declarations
  actually consumed" - true, and not a defect: no TypeScript is executed, by
  design, and the runtime half is `src/previewRetryCeilings.test.ts`, which
  imports the real tables and the real `previewDeadlineFire`. Both checkers now
  say so themselves under **WHAT THIS LAW IS NOT**, so a reader cannot mistake
  the law for a runtime proof.
- "the concurrent checker can accept multiple arms for one platform paired with a
  missing arm for another" - false. `seen = sorted(...)` is a **multiset**
  comparison against `sorted(PLATFORMS)`
  (`check_probe_concurrent_platforms.py:303-305`), so a duplicate necessarily
  displaces an expected name and is reported.
- "the registry cannot be shown to refuse a duplicate or a dropped law" - the
  duplicate half is measured in the runner (`LawError`) and in the suite
  (`test_the_registry_declares_itself`, test_laws.py:975-994); the dropped half is
  now pinned by name (`test_the_registry_cannot_silently_shrink`), and
  `test_every_red_document_still_applies` already asserts each red document still
  changes its subject.

**The red arms are measured, not asserted.** Two mutant trees with the original
code restored sit next to copies of the shipped assertions:

```text
tmp/mut-lawchecks/  (finish prints before lookup, run returns silently, find==put allowed)
                    3 failed - assert 0 == 2, DID NOT RAISE (x2)
tmp/mut-checkers/   (universe coverage, both duplicate refusals, the JSONL skip reverted)
                    4 failed, 2 passed
```

The two that still pass in `tmp/mut-checkers` are the quiet controls - the real
tree must produce no finding in either version - which is what makes the four
failures attributable to the guard and not to the fixture.

`scripts/observability/tests` was **188 tests** at that point (probe core 60,
laws 91, gate 37), and the real law suite was still 3 x `PASS` with
`red=RED_CONFIRMED` (`tmp/laws-post5.txt`, `LAWS rc=0`). The section below counts
them again after the second review round.

## The mirror was widened, and the widening found exactly one field (2026-10-11)

A second review pass on the same three checkers produced six more accepted fixes
and one accepted *widening*, each measured against a mutant that reverts it. The
measurement that matters is the widening, because it failed first and then
passed, and the failure is the interesting part.

**The mirror now grades every field `summarise()` returns**, not the eleven
`GRADED_FIELDS` it used to grade. Two fields stay exempt, each for a reason a
reader can check: `restarts` and `unreachable_windows` come from an event fold
that needs the probe's own nominal poll interval, which the printout does not
carry, and `check` is the name the caller already matched on. Everything else
must match, so a field added to `summarise()` later cannot appear in the verdict
file ungraded just because this list was written before it existed.

The first widened run went **RED** (`tmp/laws-post7.txt`, `LAWS rc=1`) on
exactly one field:

```text
1 finding(s): concurrent-platforms.reason printed 'not measured: 1
concurrent-platforms attempt(s) in the last 30.0 min window, 0 of them reached
the endpoint', the 1 sample(s) say '... in the law-window window, ...'
```

The checker had been passing its own synthetic window label into `summarise()`
while the printout carried the probe's. The fix is to read the window label FROM
the printout and hand it back, which grades the whole reason sentence - label and
all - instead of exempting it. Re-run: `LAWS rc=0`, 3 x `PASS`, all three
`red=RED_CONFIRMED` (`tmp/laws-post8.txt`). A field that had to be exempted to
make a green run would have been the wrong fix; this one removed the need for an
exemption.

**`mirror_findings(core, entry, samples)` is now a pure function** the tests call
directly - no clock, no network, no file - so the mirror itself can be shown to
fail rather than assumed to work. Five new tests drive it: quiet on a printout
that matches its samples, and a finding for a `pass` with `n_measured=0` ("an
empty window is not a pass"), a percentile the samples do not carry, an arm set
that is not the three platforms, and an arm that answered while the summary says
nothing was measured.

**The ceiling checker grew three fail-closed guards**, each with a test:
an empty stage universe (`export type PreviewRetryStage = never;`) is a finding
instead of a silently skipped coverage check; a declaration that never reaches a
semicolon is a finding instead of a `ValueError`; and a table with no object
literal is a finding instead of a crash. A fourth fix is about honesty of the
green sentence: the `session 45000<= 75000` summary is now built only when there
are no findings, so a mismatched key set reads as the finding it is instead of
raising `KeyError` while describing itself.

**The deadline pins count occurrences, not lines** (measured first: the pinned
needle is 1 line and 1 occurrence, so the stricter count keeps the same verdict
today), and its PASS sentence now says "every deadline call site this text reader
can see" - the checker reads text, and a claim it cannot support is the defect
this whole file is about.

**`-Verdict` and `-Repo` are resolved to absolute paths** in
`scripts/verify.ps1` (`Resolve-AbsolutePath`). A relative `-Verdict` used to
depend on the caller's CWD, so the gate could write its verdict - and read
`node_modules` - somewhere the caller never meant.

**The frontend ceiling oracle is now policy-owned.** `POLICY_FIRST` and
`POLICY_TOTAL` are literals in `src/previewRetryCeilings.test.ts`, and the
boundary grid and its oracle are built from those, never from the imported
tables; one test is the single seam that asserts the production tables equal the
policy. A coordinated edit to a table and the classifier can no longer move the
oracle along with it. The stage list is guarded at compile time too: `tsc` fails
if `PreviewRetryStage` gains a stage the test does not enumerate, or if the list
names a stage the union lacks.

Measured red arms, re-run against the current code: `tmp/mut-checkers` reverts
eight guards and the extracted test class reports **8 failed, 6 passed** (the six
that pass are the quiet controls and the rules that mutant does not touch), while
the shipped tree reports **14 passed**. The widened mirror was also measured by
its own failure: the one-field mismatch above is the proof that grading more
fields was not a no-op.

`scripts/observability/tests` is now **206 tests** (probe core 65, laws 102, gate
39); `node node_modules/typescript/bin/tsc --noEmit` is clean, and the ceilings
test file is 25 tests.

### What was rejected in that round, and why

A rejected finding is not a dismissed one: each line below names the code that
makes the claim false, so the next reader can re-check it instead of re-raising
it.

- **"The playability cache can pair a 404 with a retry message."** Rejected:
  `youtube_error_response` (`backend/services/youtube_diag.py:567-582`) performs
  ONE `structured_playability` lookup and hands the same tuple to both mappers,
  which is the pairing fix, and the only production call site is that function.
- **"An unexpected playability hint could pair a 503 with a generic message."**
  Rejected: the producer's domain is closed - `structured_playability`
  (`:394-398`) returns only `None`, `("unavailable", ...)` or `("gate", ...)` -
  and an out-of-domain hint lands on 503 plus the generic retry text, which is
  the safe side of the documented bias ("a wrong 503 costs one retry").
- **"The frontend guard lifecycle is unverifiable from the reviewed ranges."**
  Rejected as a scope statement: the ranges were the diff hunks. The coverage
  claim is carried by the exhaustively enumerated boundary grid and the new
  compile-time stage-list guard in `src/previewRetryCeilings.test.ts`.
- **"A red mutation only proves *a* change, not the semantic subject."**
  Rejected: `test_every_red_document_still_applies` requires each mutation's
  `find` to occur exactly `expect` times and its `put` to differ, and the
  clean/red pair is what must flip the verdict - a mutation that changes nothing
  meaningful produces `RED_NOT_REPRODUCED`, which the runner refuses.
- **"Registry uniqueness is not schema-validated."** Rejected: the runner raises
  `LawError` on a duplicate law name, and
  `test_the_registry_declares_itself` pins the three names so the registry cannot
  shrink silently.
- **"Removing the total-deadline guard is not asserted at the decisive
  boundary."** Rejected: the grid enumerates `elapsedMs` at
  `{0, 1, first-1, first, first+1, total-1, total, total+1}` for every stage
  (`src/previewRetryCeilings.test.ts:252-257`) and the oracle's `aliveTooLong`
  predicate is evaluated at `>= total` (`:238`), so the boundary is a row of the
  grid, not a sampled case.
- **"The frontend assertions are partly redundant."** Accepted as harmless and
  left alone: a redundant assertion widens the mutation surface, and deleting it
  would remove a way for the test to go red for a smaller gain than it costs.

### The one review this round that could not be obtained

`scripts/laws/lawchecks.py` was sent to `gpt` three times. The first two runs
returned `rc=7` - no answer captured - and the third reported
`ERROR: ask() wall-clock cap reached mid-Layer-3` (`tmp/gpt-b1b.txt`,
`tmp/gpt-b1c.txt`). That is the tool's own cap, not a finding about the file.
The honest statement is: **`lawchecks.py` has no external review from this
round.** What it does have is a measured red arm: `tmp/mut-lawchecks` reverts three of its
guards and the shipped assertions report **3 failed, 3 passed** against the
shipped code's **6 passed**, so each refusal is attributable to a guard and not
to the fixture. The third attempt (the narrower line-range injection) did answer,
and two of its three findings were real and are fixed: `finish()` now refuses a
second call instead of printing a second line, and `run()` resets `_FINISHED` at
entry so a second invocation in one interpreter cannot inherit the first one's
verdict. The third finding - "a PASS can be emitted without measuring anything" -
is accurate about `finish()` alone and is the one claim the library cannot fix:
lawchecks has no idea what a measurement is, so each checker owns its own PASS
rule (the concurrent mirror refuses a pass with `n_measured=0`, the ceiling
checker refuses an unreadable universe, the deadline checker refuses an
unpinned literal) and the runner refuses a red arm that is not reproduced.

### The frontend suite was flaky, and the fix was measured (2026-10-11)

The strict gate failed on vitest with two failing files. Re-running the two files
alone: **10 passed**. Re-running the whole suite: **3 files / 5 tests failed** -
and the set was *different* from the gate's. Three consecutive full runs, three
different failing pairs:

| run | failing set | why |
|---|---|---|
| gate | ArchiveSearchPopupAgeParked, PreviewChatPanelParked | timeout / one `waitFor` pass |
| full, alone | ArchiveSearchPopupAgeParked, PreviewChatPanel x2, PreviewChatPanelParked | timeouts + two sync lookups that raced |
| full, alone | LivePlayerPopup, SettingsTab | timeouts |

Every failure was either `Test timed out in 5000ms` or an element lookup that ran
out of `waitFor`'s 1s budget, and every one of those files passes alone. That is
a deadline problem, not a component problem, so it was fixed as one:

- **`vite.config.ts`** now sets `testTimeout` and `hookTimeout` to 20s. The 5s
  default is a deadline, not an assertion: a genuinely hung test still fails, a
  correct test on a loaded box stops failing. Measured after the change:
  **67 files, 1090 tests, rc=0**.
- **Four tests** got the deadline they actually need: the two jsdom-heavy tests
  that measure ~2-4s alone now declare 20s, and two lookups that raced a second
  render pass (`Chat history is incomplete` and the Cookie Bridge button) are now
  retried instead of asserted once. No assertion was weakened - the same text and
  the same role are required.

The reviewer's caveat - "a longer deadline can mask slow rendering or leaked
resources" - is the right question to ask, and the answer is in the table above:
the failures rotate across unrelated files, which is contention, not one slow
component. A single component that had genuinely become slow would fail in every
run and would fail alone.

The same review asked about `const API_PORT = process.env.PORT || "7897"` in
`vite.config.ts`. Rejected with the coupling named: `scripts/dev-all.mjs:12`
reads the same `process.env.PORT || 7897` and passes `PORT` into the API child's
environment (`:461`), so the dev proxy target and the API port are moved by one
variable on purpose. A separate variable would let them drift apart.

### The Photon sidecar's hang test could leave the orphan it catches

The reviewer read `backend/tests/test_photon_asr_sidecar.py` and found a real
test-hygiene defect: `test_hang_times_out_and_the_process_is_gone` asserts the
child is gone, and had no cleanup - so the run that FINDS the orphan bug would be
the run that leaves the orphan (an hour-long child holding ~2 GB of VRAM) behind
for the rest of the suite. Fixed:

- `_reap_sidecar(pid)` kills with the production killer (`photon_asr.kill_tree`)
  and waits for the pid to disappear, so cleanup and the code under test cannot
  disagree about what killing means;
- the hang test's assertions now sit in a `try` whose `finally` reaps the pid;
- `test_the_orphan_cleanup_reaps_a_real_survivor` starts a real sleeping child
  and requires the helper to reap it.

Measured, not assumed: with `_reap_sidecar` replaced by a no-op, that new test
fails with `the cleanup did not reap a live child` (`tmp/mut-photon-cleanup.py`),
and the file is **33 passed** with the helper in place.

The reviewer also suggested the same guard for
`test_decode_batch_falls_back_to_sherpa_on_timeout`. Rejected: that test never
learns its child's pid (the production path kills it before `PhotonTimeout` is
raised), so a test-level cleanup would have to reach into production internals
for a pid the test does not own - and the kill itself is exactly what the hang
test above measures.

### Two false reds in the gate's own probe (2026-10-11)

The gate failed twice on `concurrent-platforms` with the **same 4282.0 ms** - to
the tenth of a millisecond, in runs twenty minutes apart. An identical number
across runs is a fingerprint, not a coincidence: the check was grading a
30-minute window over a persistent sample JSONL (`probe.py --window-minutes 30`,
the daemon's default), so the *previous* run's slow sample - taken while vitest
and a browser were hammering the box - was folded into a quiet run. `n=2` was one
fresh sample plus one twenty-minute-old sample.

The same window produced a **phantom unreachable window**: 1424 s, over the 900 s
floor, spanning the gap between the two gate runs. That is "nobody probed" read
as "the app was down", and the app answered 200 at 15:35 and again at 15:45.

Fixed by grading what the gate just measured: `scripts/verify.ps1` defaults to
`-WindowMinutes 1` and `verify_gate.py --window-minutes` defaults to `1.0`. The
daemon keeps its 30-minute default. Measured:
`probe.py --once --window-minutes 1` reports `n=1` fresh sample per check, all
PASS, rc=0, and no unreachable window.

What that gives up, stated plainly: the gate no longer folds 30 minutes of
restart/unreachable *history* into its verdict. That history is the daemon's job;
the gate's question is "is the app answering now", and it now answers only that.
`-WindowMinutes 30` restores the old behaviour for anyone who wants the history.

### The liveness test was asserting disk warmth

`pytest_backend` failed on
`tests/test_settings_no_loop_block.py::test_liveness_endpoints_stay_live_during_busy_settings_save`:
`/api/asr/runtime` took **1273 ms** against a 100 ms liveness budget while a
WAL-busy save spun.

The test's own docstring had already measured that endpoint cold at ~1030 ms and
warned that timing it "would assert on disk warmth, not liveness" - and the single
warm call did not make it steady-state for the ten seconds that followed, in a
13-minute parallel run. Two changes, both aimed at the precondition rather than
the assertion:

- the warm-up now **retries** (bounded at 5 attempts) until the endpoint answers
  inside its own budget, and fails *by name* if it is slow while **idle** - so a
  genuinely slow endpoint is reported as slow, not as a blocked event loop;
- `/api/asr/runtime` - a pure filesystem stat that never touches the loop - gets
  a 1 s ceiling, while the three loop-offload endpoints keep the 100 ms contract.
  The wedge this test exists to catch froze the loop for ~10 s and queues every
  probe of every endpoint, so 1 s still catches it.

Measured after the change: the file is **3 passed**, and the worst
`/api/asr/runtime` round-trip during the busy save was **226 ms** - still over
100 ms, which is exactly why the ceiling had to move and not just the warm-up.

### The gate skipped its own 2000-test suite (2026-10-11)

Gate run 3 came back `FAILED: vitest` **and** `NOT MEASURED: pytest_backend` —
"skipped: a pytest appeared after the probe tests started; one suite at a time".
No other lane had started anything. The pytest that "appeared" was the gate's
**own** child: `live_pytest_processes()` excludes a process only when its command
line mentions `verify_gate.py` or `verify.ps1`, and the probe-tests step is spawned
as `python -m pytest -q <tests>` — no such string. CIM can still list a just-exited
child for a moment, so whether the suite ran at all was a race.

That is a false NOT_MEASURED in the one place that must never have one: under
`-Strict` it turns a complete run into exit 3 and hides 2215 real tests.

Fixed by identifying our own child instead of guessing from its command line:
`Check.run` now uses `Popen` and keeps `self.pid`, and the re-check calls
`live_pytest_processes(exclude_pids={that pid})`. Two tests pin it, and the first
is a control that would fail if the guard saw nothing at all: a real sleeping
process whose command line mentions pytest must be **seen** by the query and then
**removed** by the exclusion. Measured: `scripts/observability/tests` is **201
passed**.

### The vitest suite was starving itself on a shared 20-core box (2026-10-11)

Gate run 3 failed vitest again, on a fourth file (`DiskSection.test.tsx`, "Unable
to find ... option ... I: (344 GB free, NVMe)" — a mocked, deterministic value, so
the query simply ran before the mocked fetch landed). Three full runs launched by
hand then measured the machine rather than the tests:

| run | result | environment time | transform time |
|---|---|---|---|
| A | 67/67, 1090/1090 pass | 1492.9 s | 187.8 s |
| B | 67/67, 1090/1090 pass | 844.2 s | 240.2 s |
| C | 2 files / 4 tests fail | 728.7 s | 378.9 s |

Run C also logged `[vitest-pool]: Timeout terminating forks worker` — the pool gave
up on its own worker. The box has **20 logical processors and is shared with a game**
(League of Legends + LeagueClient + Chatterino + Discord were all live). vitest's
default is one worker per core, so a full run oversubscribes a machine already at
the governor's limit and then blames the tests. Two changes, both measured:

- `vite.config.ts` now sets `test.maxWorkers: 4` (vitest 4; `test.poolOptions` was
  removed and does not typecheck). Environment time fell from 1492.9 s to **257.2 s**
  and transform time from 378.9 s to **36.2 s** — the starvation signature is gone.
  The capped run is slower in wall clock (204.8 s vs 78.7 s in the gate) and green:
  **67 files, 1090 tests, 0 failed**.
- `src/test-setup.ts` now calls `configure({ asyncUtilTimeout: 5000 })`. Testing
  Library's own `waitFor`/`findBy*` budget is 1 s and is **independent of
  `testTimeout`** — raising `testTimeout` alone left that cliff standing, which is
  why the earlier round still failed.
- `DiskSection.test.tsx` — the failing test's five option lookups now retry
  (`findAllByRole`/`findByRole`, 5 s) instead of asserting on the first tick, because
  the labels it awaits are static and the options arrive later from `/api/disks`.

What is given up, stated plainly: the suite takes ~2.6x longer. That is the price of
not oversubscribing a box the owner is gaming on, and the gate has a 13-minute pytest
step anyway.
### The preview check graded a cold YouTube extraction against a warm budget (2026-10-11)

Gate run 4 came back with two probe reds and both were the check's own defect:
`preview` FAIL at **26,719 ms** and `concurrent-platforms` FAIL at **4,734 ms**
(budget 3000 ms for both). `health` in the same probe step answered in 312 ms
instead of its usual 15 ms, so the box was loaded — a foreign pytest from another
project had started 40 s earlier — but 26.7 s is not "a loaded box", and the
fingerprint says so: the same video, the same `session_id`, then answered in
**43 ms, 6 ms and 16 ms**.

That is the whole defect. `probe.py` picks the **newest** archived youtube video
(`ORDER BY rowid DESC`) and the FIRST `/api/preview/session` for a video is real
extraction work — the probe's own docstring already recorded **22,875 ms** for one.
The check graded that first call against the 3000 ms budget, so its verdict was
decided by whether the archive had grown since the last preview. Run 3 passed with
16 ms on a video that happened to be warm; run 4 went red on a cold one. Measured
on demand, on the live app, with a video that had never been previewed
(`ovmyuJhjsRo`): first call **1561 ms**, second call **12 ms**, same session — 130x.

A check whose passing path is "the session already exists" is not measuring the app,
and a gate that goes red when the owner archives a video is a lying instrument in
the opposite direction. The fix grades the thing the check claims to measure:

- `take_preview` warms the session first and grades the **repeat** call. The cold
  call is not discarded: its latency is recorded as `cold_ms` on the sample and in
  `tmp/liveness.jsonl`.
- `take_concurrent_platforms` warms one session per platform (sequential, ungraded,
  `warmup_ms` per arm) and only then opens the three AT ONCE, which is what its
  docstring always claimed to grade.
- A cold session that cannot be created at all is still not a pass: the warm-up gets
  `PREVIEW_COLD_TIMEOUT_S = 180 s` (the 30 s request timeout would have killed the
  26.7 s session that started all this), and a timeout or non-200 on it is reported
  with `path: cold` and "cold session could not be created". A preview that never
  opens is the original symptom.

It is not a weakening, and the tests are the control: a slow **warm** call still
fails (9,000 ms -> FAIL), a cold call that never opens is not a pass, and the
concurrent round still grades the slowest of three arms. Measured after the change,
`python scripts/observability/probe.py --once`: health 15 ms PASS, preview 16 ms
PASS, concurrent-platforms 1141 ms PASS, with
`{"path": "warm", "ms": 16, "cold_ms": 0}` and per-arm
`warmup_ms` 0 / 625 / 1328 ms recorded beside the graded 16 / 109 / 1141 ms.
### The liveness test was failing on its own shutdown, not on the loop (2026-10-11)

Gate run 5: `pytest_backend` FAILED after 856 s on exactly one test,
`test_settings_no_loop_block.py::test_liveness_endpoints_stay_live_during_busy_settings_save`,
with `42 probes, worst /api/asr/runtime round-trip 1277ms (budget 1000ms) while a
WAL-busy save spun - the event loop was blocked`. Two facts in that one sentence
contradict each other: the same run's samples show the loop-bound endpoints
peaking at **27.9 ms** (the printed list starts `0.0279, 0.0171, 0.0160, ...`), and
a blocked loop delays EVERY request, not one. `/api/asr/runtime` runs
`runtime_status()` in a worker thread, so its round-trip is the disk's and the
pool's, never the loop's.

What changed, and what the change then found:

1. **The loop's own responsiveness is now the primary assertion.** A coroutine
   beat re-arms itself on the loop every 20 ms and records how LATE it wakes
   (budget 250 ms). Red arm, measured: a 1.2 s inline `time.sleep` in the POST
   handler -> `the event loop stalled 1184ms ... the loop was blocked`. Green,
   measured: worst delay 30-56 ms across 439 beats while a WAL-busy save spun.
2. **The executor-bound endpoint is graded against itself**: worst IDLE
   round-trip x4, floor 250 ms, from at least 5 warm-up samples (a baseline of one
   lucky fast call was the old lottery - 226 ms one hour, 1277 ms the next).
3. **`post_elapsed` was measuring the shutdown.** It was read after
   `poller.join(timeout=15)`, and that join consumed its full timeout in 2 of 4
   runs, so a 10 s spin read as **26.3 s**. The instrumented failure proves what
   the number was: beats from +0.0 s to +11.0 s of a "26.1 s" spin, worst delay
   30 ms, every endpoint <= 21 ms - the loop was never blocked, the extra 15 s was
   the join. `post_elapsed` is now read the instant the POST returns. Before that
   fix 2 of 4 runs failed; after it, 4 of 4 green at ~16 s.
4. **A coverage guard** asserts the beat watched at least half the window, so a
   beat that died early cannot report "no stall" and pass.

The honest residue: the poller thread can outlive `stop.set()` by more than its 15 s
join, and its exit latency is now recorded in the failure message rather than
asserted - a request still queued at teardown is deliberately not counted as a
sample, and pinning an unexplained 15 s thread exit as a failure would be a red the
instrument cannot attribute.
### The liveness test froze the loop itself in its teardown (2026-10-11)

Gate run 5's only red was `pytest_backend` (exit 1 in 856 s, one test of 2,216).
The instrument that found the reason was the loop beat added in the section above,
and the reason was the test, not the app:

- **`poller.join(timeout=15)` was called ON the event loop.** The poller thread can
  be sitting in `fut.result(timeout=60)` waiting for a coroutine that only the loop
  can run, so joining it from the loop deadlocks the loop for the whole timeout.
  Measured standalone: 2 of 4 runs, loop stalled **14,983 ms** - a real stall, caused
  by the test's own shutdown, which the beat reported exactly as such.
- **`post_elapsed` was read after that join**, so a 10 s spin read as **26.3 s**, and
  the "the save really spun" assertion passed for the wrong reason.
- Fixed: `await asyncio.to_thread(poller.join, 15)` keeps the loop free (the pending
  request runs, the thread exits in milliseconds) and `post_elapsed` is read the
  instant the POST returns. After both: 6 of 6 standalone runs green at ~15 s, and
  gate run 6's `pytest_backend` exited 0 in 771.7 s.
- The beat now takes one final wake after `stop` instead of being cancelled in the
  finally, because cancellation is delivered before an overdue `asyncio.sleep`
  resumes and could drop the delay of a block that ends with the save. **Not
  reproduced**: a mutation sleeping 1.2 s after the handler's last await also went
  red under the old cancel shape, because the loop-bound endpoints' 100 ms budget
  catches any block >= 100 ms whatever the beat does. So this closes a hole by
  construction rather than fixing an observed false green - and both mutation shapes
  (block before the offload, block after it) are red against the current code.
- Known-unknown left visible: the poller thread's exit latency is recorded in the
  failure message rather than asserted, because pinning an unexplained 15 s thread
  exit as a failure would be a red the instrument cannot attribute.

Gate run 6, for the record: probe health 0 ms / preview 15 ms /
concurrent-platforms 890 ms, tsc exit 0 in 10.5 s, vitest exit 0 in 56.0 s,
probe tests exit 0 in 27.8 s, backend pytest exit 0 in 771.7 s, three laws PASS in
both arms - `GATE VERDICT: PASS exit 0`, the first clean strict gate of this round.

### Amendment: the poller exit is now asserted, not just recorded (2026-10-11)

The residue noted above is closed. `assert not poller.is_alive()` now runs before the
endpoint detail, so a poller thread still waiting on its last request can no longer
report "no slow sample" when what it means is "no sample". It could not be asserted
before the join moved off the loop, because the join itself was what kept the thread
alive. Evidence, all measured against the current file:

- 6 of 6 standalone runs green with the assertion in place (~15-16 s each), so the
  poller really does exit inside its 15 s join once the loop is free.
- Red arm: patching the poller loop to `while True:` so it never sees `stop` makes the
  join expire with the thread alive -> `AssertionError: the poller thread was still
  alive 15.0s after stop - its last request never completed, so the window is
  unmeasured` (restored byte-exact). The assertion can fail, so its passing is
  evidence.
- Both block mutations re-run against the same file: A (1.2 s before the offload) red
  with `the event loop stalled 1185ms ... the loop was blocked`; B (1.2 s after the
  handler's last await) red.

Gate run 7, after the last edit: probe health 188 ms / preview 46 ms /
concurrent-platforms 891 ms, tsc exit 0 in 11.7 s, vitest exit 0 in 56.4 s, probe
tests exit 0 in 22.6 s, backend pytest exit 0 in 743.6 s (`2216 passed, 3 skipped,
51 deselected in 739.94s`), three laws PASS in both arms - `GATE VERDICT: PASS
exit 0`.

Also re-verified for the owner's trial, read-only: `http://localhost:5173/` returns the
app shell (root + `/src/main`) and its proxy answers `GET /api/health` 200; the live API
serves 125 routes, `/api/archive/videos` returns the live archive (13,154 videos,
9.5 MB - the endpoint takes `platform`/`channel`, NOT a `limit`, and the client scopes by
platform, so no caller is passing a parameter the backend ignores),
`/api/archive/rate-budget`, `/api/settings/features` and `/api/archive/search` all 200.
The live `errors.jsonl` tail holds one 404 I caused myself (`GET /api/videos`, a guessed
route) and one real operational failure: `transcribe job chat-youtube-Hq_KCehYh94 failed:
yt-dlp yt_dlp_extract refused: YouTube r...` - a YouTube bot-gate refusal on the owner's
own queue, which the `VODRIP_YT_GATE_FREEZE_SEC` / remote-solver machinery already models;
not touched here.
## 2026-10-11: the "loads forever" round - two production bugs, one reviewer round

The owner reported that YouTube and Twitch videos "load forever". Two independent
defects sat behind it, and one instrument blind spot had been hiding the second.

### A. The rate-budget governor charged a token for the calls it REFUSED

`acquire()` did `st.auto_tokens -= 1.0; allowed = st.auto_tokens >= 0` - a refusal
decremented the pool *before* deciding, so every rejected request deepened the very
deficit it was rejected for. `recent_decisions` recorded
`{"source":"auto","allowed":false,"wait_s":4.286,"ceiling_rpm":20.0,"tokens":-5921.011,`
`"reason":"auto_exhausted"}` and the value fell about 1 per refusal (67/66/49 refusals
for three video ids in one window). The API display clamped with
`round(max(0.0, st.auto_tokens), 3)`, so the surface showed a healthy pool while the real
one was about 5,925 tokens under. Refill is 14/min, so no amount of waiting could clear
it: a self-lock that each requeue made worse. Fix: draw the token only when admitted
(`allowed = st.auto_tokens >= 1.0`, then `if allowed: st.auto_tokens -= 1.0`), same shape
for `user_tokens`; the admitted path is unchanged - exactly one token per admitted call.

Measured after the fix, same endpoint: four consecutive denials read
`0.688 / 0.691 / 0.691 / 0.881` - rising, never negative, so a refusal no longer moves the
pool - and the pool itself read
`{"tokens":10.695,"capacity":14.0,"refill_rpm":14.0,"exhausted":false}`. A 30 s sample of
the decision ring produced **0 new entries**, and `budget still exhausted` appears **0
times** in the last 400 lines of the dev log. The minute histogram of
`%APPDATA%\VOD.RIP\logs\errors.jsonl` is the timeline: 2 refusals/min steady until
22:27 UTC, **0 after** it (only dead-video and unrelated records remained).

### B. A Twitch VOD with no playable source was a 500 after 13.5-18.7 s

Probed in-process for `2883275549`: `TOKEN sig=False value_len=0` and
`META FAIL RuntimeError VOD 2883275549 has no seekPreviewsURL` - Twitch reports no
playable source for that id, so retrying can never succeed. `_get_vod_meta_sync` raised a
bare `RuntimeError`, the router catch-all turned it into a 500, and the client regex
`_PREVIEW_CREATE_FATAL_RE = /members-only|membership|unavailable|private|removed/i` did
not match "has no seekPreviewsURL" - so the click paid 3 POSTs (1200/2400 ms backoff) of
spinner for a video that could never play. Fix: raise `TwitchVodUnavailable` (the type
`_resolve_cloudfront_variants` already used) and map it to HTTP 404 in
`preview_create_session`. Measured: 500 after 13.5-18.7 s before, **404 in 770.2 ms**
after, detail "Twitch VOD 2883275549 is unavailable - Twitch reports no playable source
for it (removed, expired, or sub-only)", which matches the client regex on `unavailable`.
YouTube control on the same endpoint: **200 in 1658.4 ms**.

### The reviewer round, and why the finding was split (gpt, 2026-10-11)

The mandatory review returned BUG: "`_get_vod_meta_sync` treats a missing
`seekPreviewsURL` as proof that a VOD has no playable source; that does not establish the
VOD is unavailable ... raising `TwitchVodUnavailable` and mapping it to 404 can
incorrectly classify a playable VOD as permanently unavailable." Verified against the
live source, the finding is **half right, and the right half was worth a code change**:

- **Falsified:** `_get_vod_meta_sync` is only reachable *after* the token route fails.
  `get_vod_playback_sync` fetches `PlaybackAccessToken` first (line 532), and returns at
  line 567 when Twitch issues a usable signature and Usher answers - so a VOD playable
  over Usher never reaches the metadata check at all. The dead id was measured
  independently of metadata: Twitch refused the playback token (`sig=False
  value_len=0`). A missing `seekPreviewsURL` is therefore never the only evidence.
- **Upheld, and fixed:** if the token WAS issued and Usher merely failed (HTTP/URL
  error, line 568), then a metadata response without a CDN path proves nothing about
  playability, and a hard 404 would be premature. `_get_vod_meta_sync` now takes
  `token_refused: bool = True`; the caller passes
  `token_refused=not bool(sig and token)`, and only the refusal path raises the terminal
  `TwitchVodUnavailable`. The issued-token path stays a non-terminal `RuntimeError`, so
  the yt-dlp fallback is preserved exactly where the reviewer said it belongs.
- The second half of the finding ("the 404 mapping is not independently verifiable from
  the supplied lines") was a defect in my prompt, not in the patch: I passed
  `preview.py:50-100`, but the mapping lives at `preview.py:579-600`. Verified there.

### Red arms

Every new test was falsified by mutating its own subject, byte-exactly, restoring by hash:

| mutation | result |
|---|---|
| `services/rate_budget.py`: charge the token before deciding again | 2 failed |
| `services/twitch_gql_service.py`: bare `RuntimeError` for the metadata gap | 2 failed |
| `routers/preview.py`: drop the `TwitchVodUnavailable` branch | 1 failed |
| M1: collapse the new `if not token_refused:` branch | 1 failed - `TwitchVodUnavailable` raised where a non-terminal error was required |
| M2: call site hard-codes `token_refused=True` | 1 failed - `AssertionError: {'vod_id': '999', 'token_refused': True}` |

M2 was run twice. The first attempt injected a `# comment` inside the call parentheses,
which is a `SyntaxError` and therefore a *collection error*, not a red: the test never
ran, so its passing told us nothing. **A mutation that fails to compile is not a red arm.**
Re-run with valid syntax it failed on the assertion it exists to guard (`1 failed, 6
passed`).

The restore also exposed a defect in my own mutation driver: `read_text()` plus
`write_text(newline="")` had converted all three files from CRLF to LF. After repairing
the endings, `git diff --numstat` shows only the intended hunks - `routers/preview.py
22/6`, `services/rate_budget.py 15/4`, `services/twitch_gql_service.py 13/1`-ish, and the
metadata refinement keeps the file at pure CRLF (1,288 CRLF, 0 lone LF). Focused suite:
`7 passed` for the Twitch fast-fail file, `36 passed` for the older pair.

Live re-verification after the refinement: Twitch dead VOD **404 in 648.4 ms**, YouTube
control **200 in 7482.6 ms** (cold extract under load), decision ring **0 denials**.

### The blind spot this round exposed

The probe WARMS the session before grading it (`PREVIEW_COLD_TIMEOUT_S = 180.0`): the cold
path measured 22,875 / 26,719 ms while the graded warm path measured 43 / 6 / 16 ms. A
strict-green gate therefore cannot see a slow cold preview - exactly the state the owner
clicked into. Recorded, not fixed: the preview check needs a cold arm, or its green says
nothing about the first click.

### Residue (reported, deliberately not fixed)

- **ASR scratch sits on the wrong drive.** `tempfile.gettempdir()` is `G:\Temp` on this
  box (`TMP`/`TEMP`), measured at 7.6 GB free, while the model-cache drive `H:` has
  46.9 GB. 68 transcribe jobs failed with ffmpeg "No space left on device" and missing
  `shard_*.f32` files there. Relocating the 8 `tempfile.mkdtemp` call sites plus the
  orphan reaper in `services/archive_transcribe.py` is a change to the busiest lane while
  it drains a 346-job queue - the blast radius exceeds the measured benefit now. The fix
  is named for a quiet window: scratch under the model-cache drive.
- **Parakeet is Redux, proven by bytes.** The installed encoder holds 194 `MatMulNBits`
  and 0 `QuantizeLinear`; the int8 build holds 446 `QuantizeLinear`, 0 `MatMulNBits`. The
  kept int8 directory is invisible to the current code because the resolver probes
  `*.onnx` inside a directory named `sherpa-onnx-nemo-parakeet-redux`. Photon is the
  Moondream inference runtime, not a sherpa-onnx backend: `VODRIP_PHOTON_ASR` defaults to
  `0`, the venv is absent, and sherpa-onnx does not execute Photon programs.
- **Dead videos are still requeued.** After the budget fix the remaining error traffic is
  `[youtube] <id>: This video is not available` (1-3/min, one id 14 times in 15 min): a
  terminal extract failure is retried instead of being parked.

Gate run 9, after the final edit (the new verification rerun): probe health 0.0 ms / preview
0.0 ms / concurrent-platforms 813 ms, tsc exit 0 in 10.0 s, vitest exit 0 in 36.9 s, probe tests
exit 0 in 20.3 s, backend pytest exit 0 in 676.2 s (2225 passed, 3 skipped, 51 deselected, 5 warnings in 673.16s (0:11:13)),
three laws PASS in both arms (clean + RED_CONFIRMED) - `GATE VERDICT: PASS exit 0`.

### Gate run 8 went red first, and one of its three failures was a real harness defect

Gate 8 was the first run after the play fixes: `3 failed, 2221 passed, 3 skipped, 51 deselected
in 644.82s`. All three pass in isolation on the same tree (`7 passed in 47.98s`), so the red was
not the change set. Two of them were a defect worth fixing, and the mechanism was proven rather
than guessed:

`tests/test_impact_selfcheck.py` kept its LIVE cache in
`tempfile.mkdtemp(prefix="impact-selfcheck-")` - the system temp root - while
`conftest._SCRATCH_PREFIXES` lists exactly that prefix and EVERY pytest session teardown calls
`_wipe_vodrip_scratch(min_age_s=0.0, root=<temp root>)`. This suite spawns nested pytest sessions,
so another session teardown deleted the live cache between this module import and its first seed
write: `FileNotFoundError: G:\Temp\impact-selfcheck-*\cache.json`. Reproduced with the real reaper
on a private root (zero blast radius):
`ScratchReap(removed=('impact-selfcheck-probe',), kept_young=(), kept_future=(), stuck=())` on a
one-second-old directory. Fix: the cache lives in the repo-local scratch tree
(`backend/tests/_impact_scratch`, which no reaper scans) with an `atexit` cleanup, and the new
invariant test `test_live_impact_cache_is_outside_the_reaped_temp_root` fails if it moves back -
red arm: `AssertionError: the live impact cache G:\Temp\impact-selfcheck-7mnepfdr\cache.json is
inside the reaped temp root G:\Temp`, `1 failed`.

The third failure is load, not a defect, and was deliberately NOT papered over:
`/api/asr/runtime` is an `_EXECUTOR_ENDPOINTS` route held to `max(idle x multiple, 250 ms)` - a
corroborating worker-pool budget whose own failure text says "not a blocked loop" - and it measured
638 ms while the 2,221 tests of the gate saturated the box. The loop contract itself held
(`worst_stall`, `not poller.is_alive()`, `poller_errors` empty). The budget was left strict on
purpose: widening it to accommodate a saturated box would also mask a genuine executor stall. A
strict gate on a shared, governor-throttled box is load-sensitive in this one arm; that is a
property of the instrument, and it is recorded rather than hidden.

### 2026-10-11: the srdogg first-click latency round (requirement: under 3 s)

Requirement, verbatim: "i want to both twitch and youtube from srdogg channel first videos start
the preview in less then 3 seconds". Measured live against the API on :7897; srdogg = YouTube
channel SrDoglol / Twitch channel srdogg, enumerated read-only from
`H:\VOD.RIP-data\archive.db` (`videos.channel`, case-insensitive) - the YouTube name is SrDoglol,
which is why a first query for "srdogg" returned 0 YouTube rows.

| case | before | after |
|---|---|---|
| YouTube, warmed video, click at the warm height (360) | 11 ms | 11 ms |
| YouTube, warmed video, click at 720 | 6,342 ms | 12 ms |
| YouTube, warmed video, click at 1080 | not measured | 58 ms |
| Twitch VOD 2535610650, h=720 then h=1080 | 4,667 ms | 128 ms |
| Twitch VOD 2537555263, cold, then another height | 4,854 ms | 1,179 ms cold, 178 ms after |
| YouTube cold (never warmed), first rail videos | 6.8-7.9 s | 6.8-7.9 s |
| Twitch cold (never warmed) | 1.3-4.1 s | 1.2-1.3 s |

Two defects, one shape: a cache keyed by HEIGHT for a value that is height-agnostic.

**A. The warm snapshot was keyed `(vid, height)`, so the client 360 warm missed every 720 click.**
The client warms at `PREVIEW_FAST_START_HEIGHT = 360` while the click sends its own preference, so
the mismatch was the NORMAL case: the warm was paid for and then thrown away, and the first click
ran a full cold resolve. `_get_session_snapshot_closest` now serves a snapshot stored at any live
height - the snapshot carries the whole variant ladder and `prefer_height` only picks the starting
tier - with ties preferring the lower stored height so the first frame never upscales. Measured on
the same warmed video: 6,342 ms to 12 ms.

**B. The Twitch resolve was cached as `twvod:{vid}:{height}`** although
`get_vod_playback_sync` returns the master playlist plus the full variant list, so the same entry
serves every height. `_get_resolved_stream_cached_any_height` reuses it across heights. Measured on
VOD 2535610650: 4,667 ms to 128 ms (and 4,854 ms to 178 ms on 2537555263).

Both helpers are covered by red-armed tests in `tests/test_preview_cold_resolve.py`: disabling the
closest-height lookup fails with "the warm at 360 was not offered to a 720 click", and dropping the
resolved-cache prefix filter fails the cross-video leak assertion. Focused suite: 60 passed
(`test_preview_cold_resolve`, `test_preview_twitch_fastfail`, `test_preview_residual_fixes`,
`test_prefetch_cache`, `test_rate_budget`).

**What is still NOT under 3 s**: a YouTube click for a video that has never been warmed. Stage
instrumentation (added, measured, then removed) shows the fixed warm waits are innocent -
`warmjoin dt=0ms`, `snap1 dt=0ms hit=False`, `catchup dt=0ms` - and the whole cost is the resolve
chain itself: `resolve dt=3437ms` on a quiet box, 6.8-7.9 s under load. The app own warm (hover and
the channel-open batch, which the client already fires) is what brings the first click to tens of
milliseconds, and fix A is what makes that warm usable at every quality instead of only at 360.

**Residue found while measuring, deliberately NOT fixed here:**

- The recent SrDoglol YouTube rows are Shorts (13-60 s) and the preview path answers 404 "This
  video is unavailable." for them in 1.2-3.3 s, while yt-dlp on the same box resolves one of them in
  3.3 s (13 s, "#lolshorts"). `select_youtube_warm_urls` excludes Shorts from the RAIL on purpose,
  so this may be intended for the rail - but the 404 comes from the preview resolve, not the rail,
  and the message is generic.
- `services/preview/warm.py` defines `_put_resolved_stream_cache` TWICE (lines 362 and 507, byte
  identical); the later definition wins. Pre-existing, harmless today, a trap tomorrow.
- `/api/previews/status` never listed a video that `/api/preview/warm` had just warmed (polled for
  30 s), so "is it warm yet" is not observable through that endpoint.

### 2026-10-11 (later): parallel multi-platform burst round

Requirement, verbatim: "continue testing multiple platforms at once and in parallel from multiple
channels. it all must be super fast. <5 seconds to play, from first click on the preview button.
and check better about clips and vods or videos, cause you might not be seeing at all."

All numbers below are live against the API on :7897, read-only ids from
`H:\VOD.RIP-data\archive.db`. The first burst was fired with a `threading.Barrier` so the
requests genuinely start together.

**Defect A - the dev watchdog force-killed a LIVE API under a parallel burst.** With 9 simultaneous
cold preview creates, six `POST /api/preview/session` sockets died with `ConnectionResetError` at
26,306 ms and the process came back at 22:47:26 with no traceback. The dev log had the reason in it:
`[dev] API on :7897 unresponsive for ~45s - treating as hung, restarting`. `dev-all.mjs` probes
`/api/settings` every 15 s with a 2,500 ms timeout and kills after 3 strikes; `/api/settings` is APP
WORK, so a preview burst starved it and the watchdog killed a healthy process mid-request.
`scripts/observability/probe.py` already documents this exact class ("the Vite proxy logged
ECONNRESET on /api/settings ... while the API process was alive and merely starved"), and `deps.py`
created the 4-worker LIVENESS_EXECUTOR precisely so "a burst of app work can never queue the health
probe behind app work and fake a dead app". The watchdog was still probing app work. It now probes
`/api/health`, pinned by `scripts/_health_monitor_test.mjs`: a new phase serves `/api/health` while
`/api/settings` hangs, and the monitor must NOT trip. Red arm: restoring the `/api/settings` probe
fails with `the watchdog must probe /api/health, saw: /api/settings, /api/settings, ...`. After the
fix the same 9-way burst completed with 9/9 HTTP 200 and no restart.

**Defect B - two anonymous-probe workers serialized the lock-free YouTube path.** `_ANON_PROBE_EXECUTOR`
had `max_workers=2`. A 3+ video burst queued the third anonymous InnerTube probe past
`_ANON_PROBE_HEAD_START_SEC` = 2.0 s, the click fell through to the yt-dlp chain, and
`_YTDLP_EXTRACT_LOCK` (2-3 s per extract, global) serialized the rest - a comment in `ytdlp_hls.py`
predicts exactly this: "3 warm threads would serialize for 6-9s, blocking concurrent user clicks".
Measured, 3 concurrent COLD YouTube clicks: 6,685 / 10,751 / 10,985 ms (executor=2) vs
1,393 / 1,463 / 1,408 ms (executor=6). 2/3 over the 5 s budget became 3/3 under it.

**Measured matrix after both fixes** (9 concurrent, mixed platforms, cold unless noted):

| arm | cold first click | cached repeat |
|---|---|---|
| YouTube video (3 channels) | 1,393 / 1,463 / 1,408 ms | 53 / 50 / 51 ms |
| Twitch VOD (3 channels) | 1,523 / 1,869 / 1,876 ms (one run) | 669 / 741 / 747 ms |
| Kick VOD (3 channels) | 1,862 / 1,869 / 1,874 ms (one run) | 1,461 / 1,556 / 1,618 ms |

Note the variance: a later 7-way run on the same shapes measured Twitch 4,479-5,839 ms and Kick
4,724 ms while the archive scheduler had "100 video(s) queued" of Twitch chat backfill and a
transcribe pass active, and a sequential shape sweep saw YouTube 7,184-8,319 ms. Cold preview
latency on this box is load-dependent; the warm/cached path is what makes the first click fast and
it is the path the client already drives (hover and channel-open batch).

**Shape coverage (the "clips and vods or videos" question).** Probing one of each against the live
API:

| shape | url | result |
|---|---|---|
| YouTube short | `watch?v=<archive short row>` | 200, 7,184 ms, kind=hls |
| YouTube stream | `watch?v=<archive stream row>` | 200, 1,380 ms, progressive |
| YouTube video | `watch?v=<archive video row>` | 200, 8,319 ms, progressive |
| Twitch VOD | `twitch.tv/videos/<numeric>` | 200, 6,305 ms, hls |
| Twitch live page | `twitch.tv/<channel>` | 200, 5,719 ms, hls |
| New: Kick VOD | `kick.com/<slug>/videos/<uuid>` | 200, 1,272-1,874 ms, hls |
| New: Twitch CLIP | `clips.twitch.tv/<slug>` | **500 "Preview failed - try again." in 8,034 ms** |
| New: Kick CLIP | `kick.com/<slug>/clips/clip_...` | 400 "Kick channel not found" in 334 ms |

Twitch clips were genuinely unsupported and failed the worst possible way (generic retryable 500
after 8 s). `is_clip_url` already existed in `services/ytdlp_download.py` and already knew
`clips.twitch.tv`; the preview router simply never consulted it. The router now rejects Twitch clips
fast and explicitly: **400 in 55 ms** (first call after a reload, includes a cold module import) and
**400 in 7 ms** on the next, naming `twitch.tv/videos/<id>` as the alternative. Kick clips keep their
existing canonicalization path. Pinned by
`test_router_rejects_a_twitch_clip_fast_and_explicitly`. Kick clip URLs need a real clip id to test
end to end; the synthetic one was rejected in 334 ms, which is at least fast and honest.

**Coverage limits of this round, stated rather than implied:**

- The archive holds NO clip rows at all (`twitch` video_id shapes are numeric VOD 2,763,
  `twitch-live-*` 1,506 and 8 `mention-*`), so clip support could only be probed with URL shapes,
  not with real archived clips. Kick clips still need a real id.
- Channel coverage was one video per channel per platform (7-9 channels per burst), not every
  channel; the picker dedupes by channel so a wide sweep still needs a longer run.
- The `gpt` review of this round returned NO confirmed defects and explicitly listed what it could
  not see (the helper bodies and the Twitch cache implementation were outside the supplied line
  ranges). That is a scope limitation, not a clean bill of health.

### 2026-10-11 (final): clips answered, and the kick-resilience flake

**Kick clips work end to end - measured with a REAL clip id** fetched read-only from
`https://kick.com/api/v2/channels/nicklink/clips` (20 clips available):
`clip_01M4EVYSRK4QY36KQ22Y1H6JQ5`, 30 s. POST /api/preview/session with
`https://kick.com/nicklink/clips/<id>`:

| attempt | result |
|---|---|
| 1 (cold) | **200 in 1,336 ms**, kind=hls, dur=32.0 s, resolve_ms=1,328 |
| 2 (cached) | **200 in 312 ms**, resolve_ms=296 |

The API log line proves the path taken: `preview session=... platform=Kick kind=hls
heights=[] synthetic_master=False entry=https://clips.ki...`. `kick_api_service` has
`_clip_from_api_item` + `list_channel_clips_api`, so Kick clips are a supported shape; my
earlier synthetic clip id was simply a clip that does not exist (400 "Kick channel not
found" in 334 ms - fast and honest, but not a coverage result). Twitch clips remain
explicitly unsupported (400 in 7 ms naming the VOD URL) - that is a decision, not a gap in
testing.

Implementation note, because it cost three attempts: the same probe run through PowerShell
`Invoke-RestMethod` inside a joined one-liner died with exit 1 and wrote no log, twice, which
looked like a hung clip preview. It was the harness, not the app - a 150 s `urllib` timeout in
a plain Python script returned in 1.3 s. A probe that fails in the harness is not evidence
about the app.

**The gate-12 flake, fixed at the root.** `test_kick_resilience.py` had four tests all using
the video id `vid-r`. The archive job PK is derived from the video id (`job_id =
f"kick-{v.id}"`) and `_ensure_job` swallows the duplicate-key `IntegrityError` ("already queued
by a previous run"), so all four shared ONE `archive_jobs` row and each `latest_job("kick",
"vid-r")` assertion read whatever status the row happened to hold. Gate 12 failed exactly
there: `assert 'queued' == 'done'`. Each test now owns its own row (`vid-retry-ok`,
`vid-requeue`, `vid-gated`, `vid-terminal`) and `_kick_video` documents why a caller must never
share an id. Five consecutive runs of the file: 13 passed each time.

---

## Round: the user-level preview hang, and the frame layout fill (2026-10-12)

Owner report, verbatim in substance: "when *I* click the preview button it loads
forever and then errors", "measure like a user, through everything from the preview
click, not just the backend", and "dragging a panel into a frame layout must make it the
size of the whole layout, and stop erroring when interacting with layouts and panels".

### Cause 1 - a bare name in the HLS resource route (all previews, not some)

`routers/preview.py` compared `upstream.startswith(WINDOW_HLS_MARKER)` and never imported
that name. Every `/api/preview/hls/<sid>/resource` request therefore raised
`NameError: name WINDOW_HLS_MARKER is not defined`, which the route reported as 502. The
player attached, then every segment 502ed: infinite loading, then an error - the report,
exactly. Present at `b702625` (pre-existing, not introduced by the perf round).

- measured before: `errors.jsonl` held 8 consecutive `http_502 ... name
  WINDOW_HLS_MARKER is not defined` for one live session (`b570f7fb`, 07:03 UTC).
- measured after: session -> master `200` (524 B) -> variant `200` -> first
  `/resource` `200`. The route that raised the NameError now serves.

### Cause 2 - three more of the same shape, all silent

Fixed with the same reasoning, each verified by the scanner below and by the suite:

| site | what it did |
|---|---|
| `warm.py` preflight | the `resolve_stream_info` import sat INSIDE the `if` branch that returns first, so the call below raised NameError, `except` logged "preflight mux resolve skipped" and the preflight warm **never ran** |
| `warm.py` full-mux warm | used `resolve_stream_info`, `_resolve_youtube_preview_audio` and `MuxJob` with no import in scope: NameError was caught, and the handler **marked the video warm-dead for 6 h** |
| `__main_launcher.py` `_probe` | `http_requests` was imported in a *sibling* function, so the launcher watchdog probe always answered False |

### Law: no backend module may load a name it never binds in scope

`backend/tests/test_no_undefined_module_names.py` walks the scope stack (module ->
function -> nested function/class) and reports loads that resolve nowhere. Annotation
semantics are honoured (`from __future__ import annotations`; local annotations are never
evaluated).

- clean arm: **0 undefined-name loads across 140 modules**.
- red arm: the pre-fix router (`git show b702625:backend/routers/preview.py`) reports
  `line 934: undefined name WINDOW_HLS_MARKER` - the law fails on the bug it exists for.
- self-check arm: a synthetic module returning `window_hls_marker + 1` is flagged, and a
  module using a closure read plus a deferred annotation is not. 3 passed.

### Measurement: like a user, from the click

`e2e/playwright.user.config.ts` + `e2e/tests/preview-user-latency.spec.ts` drive the real
UI in the **installed Chrome** (Playwright 1.63 wants `chromium_headless_shell-1243`, which
this box does not have cached - that is an environment gap, not an app result).

- server-side readiness, measured directly: session create `200 in 16 ms`, then
  `playlist_ready/segment_buffer_ready/mux_ready` all true at **4,157 ms**; without any
  master-playlist fetch at all, **4,484 ms** - so there is no deadlock and no lazy kickoff.
- the client gate is real and it is where the report comes from: `App.tsx:1990-2007`
  awaits `waitForPreviewMuxReady` before opening the player for a cold YouTube HLS session,
  and when the poll budget runs out it **throws** ("surface the retry UI").
- worst single stall measured in the click timeline: `POST /api/preview/warm/batch`
  `1188 ms -> 14367 ms` (**13.2 s**) while every other `/api/preview/*` call in the same
  window answered in 10-200 ms. That, not the session create, is the remaining slow step
  under a multi-popup burst and the next thing to fix.

### Law: a panel dropped into a frame layout fills that layout

Both snap `fit()` implementations returned something strictly smaller than the cell: the
live player kept `Math.min(inner.w, sizeRef.current.w)` (its own width), and the explore
panel capped its video at 92% of the cell. `frameSnapPin` writes `width = fit(...)`, so a
snapped panel was always visibly smaller than the layout it was dropped into.

- clean arm: `e2e/tests/frame-popup-snap.spec.ts` now asserts the snapped popup fills the
  cell inner box (cell minus `FRAME_SNAP_INNER_PAD * 2`); **4 passed (12.9 s)**.
- red arm: restoring the old 92% cap makes the same run **fail** - the assertion is not a
  law that cannot fail.
- no-error arm: the same test now collects `pageerror` + console errors across the drag and
  snap and asserts the list is empty; it is. The reported "some kind of error interacting
  with layouts and panels" is not reproduced by this path.

- review loop (AGENTS.md): `gpt -f` on line ranges of the changed hunks - the whole
  files were 49,866 chars, past the 30,000-char safe paste limit, so the full-file run
  never got an answer and is not evidence. Two findings came back with the ranges:
  (1) **the narrow-cell width policy was implicit and could overshoot the cell**:
  `Math.max(EXPLORE_PANEL_MIN_W, inner.w - chatW)` on a cell narrower than the chat
  column asks for more than the whole cell and leaves `panelWidthRef` disagreeing with
  the React state derived from it (`inner.w - chatTotal`, which goes negative). The
  policy is now the pure `exploreSnapVideoWidth()` in `src/frameSnap.ts`, used by BOTH the
  ref and the state, and pinned by `src/frameSnap.test.ts` (5 passed: no-chat, with-chat,
  chat wider than the cell, zero/negative inner width, no-overflow vs `frameSnapInnerBox`).
  (2) filling the box could distort the video - **false here**: the element is
  `<video ... style={{ width: 100%, height: 100%, objectFit: contain }} />`
  (`LivePlayerPopup.tsx:2562`), so a filled cell letterboxes the picture. The first grep
  for this searched `object-fit` and missed the camelCase inline style; the claim is now
  measured, not asserted.

## The remaining UI hang, measured (2026-10-12 05:36, after the 502 fix was live)

One preview click in the real UI, every `/api/preview/*` call timestamped. The mux
poll is **not** the culprit and the unused budget is not the problem:

```
   0ms CLICK preview
 877ms REQ GET  preview/panel/youtube/TmI2QvpocDE      -> 37724ms (37.7 s)
 879ms REQ POST preview/session                       -> NO ANSWER for 61 s
1192ms REQ GET  previews/status                        -> 42120ms
37624ms RES 200 preview/warm/batch                    (28-36 s each, repeatedly)
61273ms REQ POST preview/session   (client RETRIES the create it never got)
86365ms RES 200 preview/session
87085ms REQ GET  preview/hls/8a6d8c1c1e436b2f/stream.mp4
151278ms RES 500 preview/hls/8a6d8c1c1e436b2f/stream.mp4
```

The API log names the cause:

```
rate_budget: pacing yt-dlp yt_dlp_extract for 4.3s (auto pool dry, ceiling 20.0 rpm)
YtGovernorExhausted: yt-dlp yt_dlp_extract refused: YouTube rate limit budget still
  exhausted after waiting 4s (platform=youtube source=auto ceiling=20.0 rpm)
```

A preview session create needs one YouTube extract. That extract runs through
`_guarded_youtube_dl` -> `_governor_admit_ytdlp(origin, "yt_dlp_extract")`
(`archive_ytdlp.py:686`) with **no `interactive` flag**, so it is metered against the
same 20 rpm AUTO pool that the archive/transcribe lane drains (the log shows
`chat-youtube-*` backfill jobs being refused by the same budget). `interactive=True`
means source `user`, and the docstring states the policy plainly: AUTO waits at most
`_YTDLP_GOVERNOR_MAX_WAIT_S` (30 s) and is then refused; **a USER caller never waits**
(Twitch already implements exactly this split for user actions - `archive_twitch.py`:
"an interactive kick (interactive=True) fail[s] fast".

So a user click is currently queued behind hours of background work: it either waits
~30 s per attempt and then errors, or the client retries and waits again. The 61 s of
silence before the retry is the user-facing symptom, and `This video is unavailable`
(http_404 on POST /api/preview/session) is what a refused extract tells the user.

**Not fixed here - it is a policy decision for the owner, and it is one line once
decided.** The choices:

1. route the preview session create (and its first mux) through the interactive lane,
   `interactive=True`, so a click is admitted immediately like a Twitch kick is;
2. keep the auto pool but bound the click differently - fail fast (0 wait), then
   retry with backoff inside the request - so the UI shows a state instead of
   hanging 61 s;
3. give preview its OWN bucket so background backfill cannot starve it at all
   (larger blast radius, it is a real YouTube-egress policy change).

This is the same class of decision as `VODRIP_YT_EXECUTE_REMOTE_CHALLENGE_SOLVER`:
a dial that trades YouTube good standing for responsiveness. It should not be
flipped silently by an agent.

## Round 2026-10-12 12:25 — the preview works; the browser never asks for it (user-POV trace, frontend → backend)

**Correction first, because it matters more than the finding:** an earlier claim in this
thread said a stale resource id 404 (`/resource?id=…`) had been reproduced as the user's
failure. That was a **probe bug, not an app bug** — the media-playlist URIs in the master
are already absolute paths (`/api/preview/hls/<sid>/…`) and my probe prefixed the host
prefix a second time, so the server answered FastAPI's *no-route* body
`{"detail":"Not Found"}` instead of the app's `Unknown preview resource`. Re-measured
with the correct URL, the chain serves everything. The 404 claim is withdrawn.

### Measured, same video the user clicked (`dQw4w9WgXcQ`), API on :7897, no browser

```
POST /api/preview/session            200  15 ms
GET  .../master.m3u8                200  m3u8 1001 B
GET  .../resource?id=371483f6c2bea291 200  m3u8 3084 B  (38 segments)
GET  .../resource?id=ba8b47541b70c7  206  5,361,808 B video/mp2t   ← the first byte of video
GET  .../resource?id=57654b616387ee  206  1,940,617 B video/mp2t
GET  .../resource?id=b316d687ffba24  206  1,384,056 B video/mp2t
```

The window-HLS mux reaches `mux_ready` in 18–30 s and every variant's first segment
streams. **The backend serves the user's video.** (Probe: `tmp/chain5.py`, log
`tmp/chain5.log`; segment-path reachability also verified on Twitch: `preview_status_timings`
cycle = ready.)

### Measured, the same click in a real browser (Chrome, :5173, preview-user-latency.spec.ts)

```
1185ms  REQ POST preview/warm/batch
1272ms  RES 200 preview/warm/batch      (12.7 s of yt-dlp)
1689ms  REQ POST preview/warm/batch      ─ repeated TEN times, ~18.7 s each, all 200
21525ms RES 200 previews/status
         console: Failed to load resource: the server responded with a status of 500
         (no POST /api/preview/session in 240 s — the test timed out before the player attached)
```

**The click never reaches preview-session creation.** The user's own log has the same
signature — a single `POST /api/preview/warm/batch` at 11:56:51 and no session POST.

### The user's `FastAPI on :7897 is not running` is a false alarm (log proof)

```
11:56:58.195  GET /api/settings/features   200
11:57:02.983  extract ok video=7cfvItEnWGY
```

Requests were served on both sides of the user's 11:56:59 `ECONNRESET`, with no restart,
no traceback and no shutdown in `tmp/vodrip-devall-api.log`. Only POSTs reset
(`/api/settings`, `/api/presence`) — the signature of a busy event loop dropping idle
**keep-alive** sockets, with vite's canned message blaming a dead server. The server was
alive; the connection was recycled.

### What this leaves, stated as hypothesis not measurement

1. The click's request path is saturated by the **rail's warm flood** (sequential
   `warm/batch`, ~19 s each) before it ever creates a session — the 61 s of unanswered
   first `POST /api/preview/session` measured at 05:36 fits the same shape.
2. The **AUTO vs interactive governor lane** is the owner's open question (recorded
   earlier): measured `rate_budget: pacing yt-dlp yt_dlp_extract for 4.3s (auto pool
   dry)` then `YtGovernorExhausted` → `http_404 POST /api/preview/session: This video
   is unavailable`. A user's click rides the AUTO bucket behind archive backfills.
3. The 500 and the exact hls.js fatal remain **unmeasured** — the spec timed out before
   the player attached. That is the next measurement, not a conclusion.

An unmeasured check is never a pass: the backend half is green (served bytes above), the
frontend half is not even started, and green on one half cannot speak for the other.

## Round 2026-10-12 12:45 — the user's own log closes the timeline (and kills one of my theories)

From `tmp/vodrip-devall-api.log`, session `93b4da96` (the user's dQw4w9WgXcQ):

```
11:58:35  session_created server_ms=18703  kind=hls  dash_window=True
11:58:53  window hls mux attempt 2: truncated Range 0-1048576: got ... expected 1048577
11:58:58  window hls fallback to hls session=93b4da96
```

and the same sequence reproduced at 12:19 and again at 12:43 in my own runs. So the
user's click is NOT slow (their PREVIEW_TIMING lines say click->open 24 ms and 39 ms);
the cost is **18.7 s to create the session + ~15 s of three failed mux attempts +
the fallback** before anything is watchable.

### The QUIC hypothesis is DISPROVEN (red not reproduced -> the law is falsifiable only if it can fail)

I forced `http_version=v3` suspicion: retrying the same span over negotiated HTTP/2
also truncates, and the log names the host -- **`manifest.googlevideo.com`**, not the
`rr*---sn-*` media host that serves 5.3 MB seconds later through the resource proxy.
A short body on the MANIFEST host is not a transport choice; the mux is issuing plain
HTTP Range requests against a host that speaks the SABR/protobuf exchange (yt-dlp's own
log warns "YouTube may have enabled the SABR-only streaming experiment"). The commit
`65c3889` is followed by one that keeps the diagnostic but claims no fix.

### What the fallback actually proves

`window hls fallback to hls` ends with the video SERVING: master 200, media playlist
200 with 38 segments, first segment 206 + 5,361,808 B of video/mp2t (measured after the
fallback, three times). So the preview does eventually play; it pays ~34 s of failed
window-mux work on the way, and the client's `waitForPreviewMuxReady` gate can throw
on its deadline during exactly that window. The candidate fix follows from the
measurement, not from the hypothesis: don't run the window mux at all when the media
URLs are already directly servable, or give the gate the fallback's ready state
sooner. Unmeasured until then.


## 2026-10-12 14:40 — "arruma tudo restante": the 3-second path, and a review
that found a hole I could not see

Owner instruction: apply everything suggested and every friction seen. Three commits:
`c13c4ee`, `f4885fd`, `90c4040`.

### Measured, the owner's own click path

| path | server_ms |
|---|---|
| warm/batch -> snapshot ready, extract cached | 0.5 ms (13:03:35,729 -> .229) |
| warm/batch -> snapshot ready, cold extract | 1.5 s (owner log 12:58:37 -> 39) |
| CLICK while the warm is queued — BEFORE | 18,703 ms (owner log 11:58:35) |
| CLICK while the warm is queued — AFTER c13c4ee | 5,610 ms |
| CLICK, progressive path, no mux at all | 5,485 ms |

The 18.7 s was never the governor: the preview extract already runs on the
interactive lane (`ytdlp_hls.py:1498`, `kind="interactive"`; the `guarded_youtube_dl
lock at :4134 is the clip-audio path, which is why it is irrelevant here). It was a
snapshot MISS: the warm for the clicked URL was still queued behind the process-wide
`guarded_youtube_dl` lock, so `create_session` found no snapshot after 1.5 s + 3 s of
waiting and paid a full cold resolve. `Event.wait` returns the instant the warm sets
its event, so raising the ceilings to 3.5 s + 5 s costs nothing on a fast warm and
buys the join on a queued one - that is the whole fix, and the number proves it.

### The mux loop was paying for a transport fix a re-extract cannot buy

`truncated Range 0-1048576: got 1915 expected 1048577` on `manifest.googlevideo.com`,
three attempts, each with freshly re-extracted URLs, all identically truncated -
while the SAME URLs served 206 + 5.3 MB through the fallback seconds later. A
truncated body is a transport, not a stale URL, so the loop now breaks after the
first one. RED ARM VERIFIED: with the branch removed, the attempt count goes 1 -> 3.

### The review loop earned its keep (the part worth remembering)

`gpt -f` over the two commits returned three findings. The [CRITICAL] one was
wrong and is now DISPROVEN BY TEST rather than by argument: `GooglevideoTruncatedBody`
subclasses `StaleGooglevideoUrl` (`ytdlp_hls.py:64`), so the post-loop isinstance guard
already classifies it and the fallback still runs - the test asserts one attempt AND
the fallback, and its red arm is the fail-fast being removed.

The [HIGH] one was right, and only because it said UNKNOWN. "The deferred chat job
returns a dict; if the caller ignores it the job is never requeued" - the caller was
unknown to the reviewer, so I went and read it: the neighbouring youtube-gate branch
requeues through `archive_db.update_job(status="queued")` FIRST and only then returns.
Mine only returned. Red arm: `assert 'queued' == 'running'` - the job sat in
running forever. A review that reports its own blind spot is the useful kind.

### Not closed

The window mux is still skipped-or-failed and the fallback still serves the video; the
5.5 s cold click is 2.5 s over the owner's 3 s target and the remaining cost is the
real extract+warm, which only a warm cache removes. The 0.5 ms snapshot hit is the
3-second path and it needs the client warm to fire before the row is clicked.

## Round: the preview-subtitles 502 was a budget aimed at the wrong phase

Measured 2026-10-12 14:51 against `GET /api/preview/subtitles/twitch/<id>` on the
running API (`tmp/vodrip-devall-api.log`), four requests in a row:

| # | endpoint | outcome |
|---|---|---|
| 1 | `/api/preview/subtitles/twitch/2574054422` | 502 Bad Gateway |
| 2 | `/api/preview/subtitles/twitch/2601888557` | 502 Bad Gateway |
| 3 | `/api/preview/subtitles/twitch/2792110402` | 502 Bad Gateway |
| 4 | (owner log) `/api/preview/subtitles/twitch/2574054422` | 502 Bad Gateway |

The child log is the proof of the mechanism:

```
14:51:29 sidecar ready (pid=40552 provider=cuda load=13.8s)  <- ONE child, fully warm
14:51:35 preview ASR sidecar cancel not acked; escalating
14:51:35 failed for twitch/2574054422: no caption row within 4.0s
14:51:56 failed for twitch/2792110402: sidecar closed its output (died?)
```

**Two candidate causes, and the first one was wrong.** `_ensure` handed out a
child whose warm was still running (Popen assigns `_proc` before the CUDA load
finishes, `FIRST_OUTPUT_S` is 4.0 s, the load measured 12.3-13.8 s), which is a
real race and was fixed first (`64e2ebd`, warm gate + one spawn under a lock).
It did not clear the 502: the 14:51 log shows a single warmed child. A fix is
not evidence that the cause is gone — the same endpoint still answered 502
afterwards, which is exactly how the wrong diagnosis was caught.

**The real cause: `FIRST_OUTPUT_S` was charging the audio fetch.** The ceiling
means "no caption row has appeared", which is a verdict about a hung *decode*.
The child spends its first seconds *fetching* the VOD head, where no caption row
can exist yet. A fetch longer than 4 s therefore looked identical to a hung
model: the supervisor cancelled a working request, the child missed the cancel
inside its blocking fetch ("cancel not acked"), was killed, and the next request
respawned it and paid another ~14 s CUDA load before dying the same way. Four
502s and ~40 s of reloading from one budget pointed at the wrong phase.

**The law that replaced it.** A named phase is a promise about where the time is
going: the child emits `{"event":"stage","stage":"fetch","budget_s":<total>}`
and the supervisor replaces the decode ceiling with that phase budget, while
`total_s` still bounds the whole request. Silence inside a named phase is no
longer indistinguishable from a hung model — but a child that names a phase
forever still dies at the total ceiling, which is the arm that stops this from
becoming an escape hatch.

**[CLOSED, falsified twice]** A red arm that neutralises the *real* stage branch
turns both new tests red: the named phase is killed at the decode ceiling, and
the total-ceiling test fails too because the stall now fires first and the total
ceiling is never reached. An earlier red arm was a no-op (it inserted a `pass`
*above* the live branch, so all three tests stayed green) — an arm that cannot
go red is not evidence, and this one briefly looked like evidence because it
was aimed at the wrong line.

**Secondary, same file.** The startup banner `cobalt-canon: ok` is the child
speaking before its first protocol line, not a protocol break. It is now debug
until the first valid JSON, and a warning only after one.

**Still not closed.**

- No measurement yet that a live Twitch preview-subtitles request now answers
  `200` with rows. The gate is 71 passed on the supervisor/worker contract; the
  endpoint has only been observed failing.
- `VODRIP_PREVIEW_ASR_FIRST_OUTPUT_S=4.0` remains the default, so a fetch that
  names no phase (an older child) is still killed at 4 s.
- The fetch itself has no progress signal: a fetch that stalls for 14 s is now
  correctly *allowed*, but a user watching it sees nothing rather than "fetching".


### Follow-up measurement, 15:10 (same endpoint, sidecar already warm)

| id | outcome | wall |
|---|---|---|
| `2574054422` | 502 | 16,901 ms |
| `2601888557` | 502 | 422 ms |
| `2792110402` | 502 | 13,659 ms |

The first-output kill is **gone**: the first request ran its whole budget and
answered at 16.9 s instead of dying at 4.0 s, and the log line still reads
"within 4.0s" because that string reports the ceiling value, not the elapsed
time — a log that reads like a 4 s failure when the call took 16.9 s is its own
trap and is worth remembering before quoting it.

The remaining 502 is now the fetch itself. `15:10:49` => sidecar cancelled a
still-working request, `15:10:50` => child dead, `preview ASR sidecar request
failed (attempt 1/2): [Errno 22] Invalid argument` => transport broke under the
supervisor, child respawned (`pid=42648 provider=cuda load=7.5s`), and the same
id failed again. Two facts follow and neither is fixed:

1. The child cannot be interrupted mid-fetch ("cancel not acked" every cycle),
   so the supervisor has no clean way to reclaim a fetch that outlives its
   budget — it escalates to a kill, which breaks the pipe for the *other*
   in-flight riders.
2. `at._fetch_remote_audio_slice` is the only stage with no progress signal and
   no partial answer, so a fetch that yields nothing and a fetch that never
   starts look identical from outside both.

The next law to write is not another ceiling: it is that the audio fetch must
answer *something* inside its budget — even `{"outcome":"no-audio"}` — because
the current design lets the only stage that cannot report silence be the one
that holds the longest.

## 2026-10-12 16:20 — The modal ladder, and why a literal is worse than a wrong constant

A second pass of the "hard to click / wrong hierarchy" audit. Every app-level
overlay was supposed to sit on `MODAL_Z` (`EXPLORE_POPUP_Z + MAX_EXPLORE_POPUPS
+ 2 = 10006`, `layoutUtils.ts:1099`). Two did not:

| file | what it used | the visible defect |
|---|---|---|
| `CookieInstallOffer.tsx` | `z-[21000]` | outranks the whole ladder by 10k for no reason |
| `FirstRunWizard.tsx` | `z-[9999]` | **`z-[9999]` IS `EXPLORE_POPUP_Z`** — the base every floating player stacks on, so a preview popup opened afterwards painted over a wizard the user still has to answer |
| `DownloadConfirmDialog.tsx`, `ExtensionWaitOverlay.tsx` | `zIndex: MODAL_Z` | correct |

**A magic literal is worse than a wrong constant.** A wrong constant fails in a
direction you can predict from its neighbours. `21000` fails in NO direction:
it is above the ladder, it outranks every modal that WILL be written against
the ladder, and no reader can tell that from the code, because the number is
arbitrary rather than derived. The defect is not the value, it is that the
value is a claim nothing can check. `ModalZLaw.test.tsx` now makes it checkable:
`MODAL_Z > EXPLORE_POPUP_Z`, and each of the four modal files must carry
`zIndex: MODAL_Z` and match neither `/z-\[\d+\]/` nor `/zIndex:\s*\d{3,}/`.

*Red arm, real:* restoring `z-[21000]` and `z-[9999]` turned exactly 3 of 6
assertions red (both per-file arms plus the aggregate offender scan) and left
the ladder-order arm green. Green arm: 6/6. **A law that cannot fail is not
evidence** — if the aggregate scan had not gone red, the arm would have been a
no-op and the green would have been worthless.

### Audited and found correct — recorded so the next pass does not redo it

| claim | measurement |
|---|---|
| the live player's click-catcher does not block fullscreen | catcher is `absolute inset-0 z-0` (`LivePlayerPopup.tsx:2538`), the transport bar is `zIndex: 10` (`:2662`) — the transport is above it |
| the captions overlay does not sit on the transport | `bottom-16 z-[5]` (`:2909`) vs transport `bottom: 0, zIndex: 10` — 64px of clearance |
| transcript rows are drag-safe | `PreviewChatPanel` rows are `<button>`, and its root carries `data-preview-chat-panel`, which `onPopupDrag`'s ignore list (`ChannelExplorePopup.tsx:1177`) already `closest()`-matches |
| no click-eating overlays | 102 source files scanned for `absolute inset-0` with no `pointer-events-none`/`aria-hidden` in the next 4 lines: 12 hits, all video-area click catchers or intent-blocking error states |
| no oversized shrink-0 rows left in the popup dock | every remaining `shrink-0` row in `ChannelExplorePopup.tsx` is either `flex-wrap`-parented or a single icon |

## 2026-10-12 — A read timeout is not a budget, and a docstring is not a check

The owner's complaint: *"extremamente lento ... sinto q ia carregar pra sempre."*
The measured click, on a video that has been on YouTube since 2005
(`jNQXAC9IVRw`): **`POST /api/preview/session` → 404 after 27,499 ms**, body
`{"detail":"This video is unavailable."}`. The backend's own reason was
`preview_race_exhausted`. The video was never unavailable.

### What was actually true

`_reextract_youtube_for_preview` (`session.py:2808`) — the `force_fresh=True`
path a cold click takes — calls the **slow** collector first and the **fast** one
second:

| step | measured |
|---|---|
| `_collect_merged_innertube_info(vid, session, 12.0, "user")` | **142,048 ms**, 8 formats |
| `innertube_extract_info(...)` — only reached if the first is rejected | **2,467 ms**, 1 format |
| `_youtube_info_is_dash_only_progressive` on both | `False` — **neither was rejected** |

So the 142 s was not a rejection. It was the first call. And the function's own
docstring claimed the opposite:

> *"Each probe is therefore clamped to the player read timeout — ... one hung
> client must not cost 9x the table's worth of wall time."*

**Measured: 8.3x the table's nominal worst case** (8 × `_READ_TIMEOUT_PLAYER_SEC`
2.0 + `_TV_TIMEOUT_SEC` 1.1 = 17.1 s). The docstring described a guarantee the
code did not have, and a reader who trusted it would look anywhere but here.

### The law

`read_timeout` bounds **one socket read**, not one request. A client that drips
bytes overruns it without limit, and `_collect_merged_innertube_info` walks
`_CLIENT_ORDER` **sequentially**, so the overrun is paid once per client. The
caller's `12.0` was never a budget, because the parameter is neither named nor
used as one.

The contract now enforced — stated exactly, because it is *not* "kill an
in-flight request": nothing here can interrupt a blocked socket read.

1. no probe **starts** once the budget is spent;
2. no probe is **handed** more time than the budget has left.

| | before | after |
|---|---|---|
| `_collect_merged_innertube_info(read_timeout=12.0)` | 142,048 ms | **10,252 ms**, still 8 formats |
| `POST /api/preview/session` — the real click | **404** in 27,499 ms | **200** in 6,285 ms |

Breaking out early loses nothing: the walk accumulates `all_formats` across
clients, so the deadline returns what the clients that answered already gave.
The bot-gate path (`youtube_innertube.py:1250`) already `break`s and returns a
partial merge, so partial results are not a new semantics — this adds a second
reason to stop, not a new kind of answer.

`backend/tests/test_innertube_wall_budget.py` pins both clauses deterministically
with a fake clock: no `sleep`, no tolerance window.

*Red arm, real:* against the unfixed file, **4 of 4 failed** (the real-clock arm
measured 20.75 s on a 2.0 s budget). Green arm: 4 passed. 239 youtube/innertube
tests passed with the fix, 1 skipped.

### The review that made the test honest

The first version asserted only `elapsed < 6.0` for a 2.0 s budget and
`len(calls) < len(_CLIENT_ORDER)`. `gpt -f` rejected it correctly: 4 s of
overrun is tolerated, and a table-length comparison does not prove the deadline
caused the stop — a client with no profile is skipped for a different reason. The
deterministic rewrite tests the contract clause by clause, and the third test
requires the **last probe to have overshot the deadline**, so a walk that stopped
early for any other reason fails it. **A test that tolerates the defect it names
is a decoration.**

### Still open — recorded, not fixed here

- The user-facing message is still a lie in the remaining timeout cases: an
  extract timeout surfaces as `"This video is unavailable."`, and the frontend's
  `_PREVIEW_CREATE_FATAL_RE` matches `unavailable`, so it treats a **timeout** as
  permanent and never retries.
- The metric that reported green while the user waited is `click→open`
  (`PREVIEW_TIMING`), which measures the popup opening, not the video. The same
  log carries `click→open=-1ms` sentinels. The law the owner actually asked for
  is **click → first decodable byte**.
- 6,285 ms is working, not ≤3 s. What remains is the sequential walk itself; the
  ordering fix — try the 2.5 s path before the 142 s one — is not in this change.



## 2026-10-12 — A guard nobody calls is not a guard (and a metric can lie while it runs)

Four defects fixed in one pass. They share one shape: **the correct signal
existed, and the call site used the unguarded one.**

### 1. A stale playability record branded a live video dead

`POST /api/preview/session` on `jNQXAC9IVRw` — a video on YouTube since 2005 —
returned **404 `"This video is unavailable."` in 27,499 ms**. The backend log's
last word was `extract fail ... reason=preview_race_exhausted`, which maps to
**503** and an honest "try again" message. So the 404 came from somewhere else.

It came from `_youtube_extract_preview_with_retries` (`ytdlp_hls.py:963`)
calling `innertube_last_playability` — a reader with **no age limit and no kind
validation** — and turning its answer into a user-facing verdict. Two facts make
that fatal:

- `_record_playability` **never downgrades a stored fatal**;
- its own docstring records that "a bot-gated WEB probe reports 'Video
  unavailable' while ANDROID plays the same video".

So **one transient bot-gated probe branded that video for the life of the
process.** Its sibling `innertube_playability_verdict` exists for exactly this
and says so: *"A caller that turns this into a user-facing status code must not
be handed a guess."* It was used by `youtube_diag.structured_playability` and by
**neither** extraction call site.

Fix: both sites now use the validated reader. Note the trap — the two readers
return tuples in **different orders**: `(status, reason, kind)` vs
`(kind, status, reason)`.

The client matched `unavailable` as permanent (`_PREVIEW_CREATE_FATAL_RE`), so
the wrong word here was not cosmetic: it turned a retryable timeout into a dead
end the user could not escape by clicking again.

### 2. The frontend made the same mistake with prose

The same regex matched the message a **503** gets when its body is not JSON:
`useApiClient.ts:114` falls back to `res.statusText`, the literal string
**"Service Unavailable"**. `ApiError` already carried `.status` — its own
docstring states the intent, *"so callers can tell a terminal 404/410 from a
transient blip"* — and the classifier ignored it. Now status decides
(403/404/410 permanent; 408/425/429/5xx transient) and prose is only a fallback
for throws with no status.

**`zIOnnd61w40` really is gone** and must stay permanent; it does, by status.

### 3. The metric measured the popup, not the click

`full_path` reported `click→playable` as `open→playable` whenever no gesture
had been captured, and **the main surface never captured one** (`openPreview`
runs from an effect, not from the click). A click that hung 26 s logged
`preview_open click→open=29ms` and nothing else, because a failed preview
emitted **no terminal event at all**.

Now: an unmeasurable segment reports `unknown` instead of being silently
substituted; `selectVod` captures the gesture for the main surface; and
`markFailed` gives every failed click a terminal line with its own duration
(guarded so a post-playable stall is not called a failed click).

### 4. A dropped `.finally()` made every failed preview an unhandled rejection

`_dedupInflight` chained `promise.finally(cleanup)` and **discarded the derived
promise**, which rejects exactly like the original. Found because the new
frontend tests emitted exactly one unhandled rejection per test — 8 tests, 8
errors — and the trace pointed back into the source. `then(ok, err)` with
non-throwing handlers always fulfils.

### The law

**A guard that exists but has no caller is not a guard. A reader with no age
limit is not evidence. A metric that cannot fail is not a measurement.**

Red arms (all measured, not asserted):

| mutation | result |
|---|---|
| restore the unsafe reader | `test_a_stale_fatal_record_does_not_abort_the_ladder` + the AST source law both fail |
| `const measured = true` | the no-gesture test fails |
| `if (true) return` in `markFailed` | the terminal-event test fails |
| drop the `.finally()` fix | 8 unhandled rejections, one per test |

### Still open — measured, not fixed here

The ≤3 s law is **not met**. Racing the two InnerTube paths (which replaced both
sequential orders, each measured worse) gives, on cold clicks:

| video | before | after |
|---|---|---|
| UU6Rrs6y6Ec | 2,186 ms | **953 ms** ✓ |
| 26he4HN565U | 7,077 ms | 4,296 ms |
| bUQCh9abGwo | 7,101 ms | 5,485 ms |
| jNQXAC9IVRw | 6,285 ms | 5,688 ms |

Ordering was measured, not guessed: merged-first cost every click 6.3–7.1 s;
fast-first cost the misses **24 s** (5.5 s miss paid before a 12 s walk). The
race is never worse than either. Three of four still miss 3 s, and the honest
reason is that both InnerTube legs lose on those videos and the yt-dlp fallback
answers.

## The warm was lying, and the lie was in the classifier (2026-10-12)

**A preview click was slow because the warm had already "failed" — and the warm
was wrong.** The API log carried, for videos the click then resolved in under a
second:

```
cold preview resolve failed for UU6Rrs6y6Ec after 25.3s: This video is unavailable
```

Three videos were branded **definitively fatal for the life of the process** by
`_classify_playability`, because it read an ambiguous status — InnerTube's bare
`ERROR` — as proof of removal whenever the reason text merely contained
"unavailable". `_record_playability` then refuses to downgrade a stored fatal,
so one bad probe was permanent: the warm aborted, no snapshot was ever built, and
every click paid a cold resolve.

**The guard that already covered this was one branch earlier.**
`_DEFINITIVE_PLAYABILITY_MARKERS` already lists every genuine removal ("removed
by the uploader", "video removed", "private", "members only", "deleted", "no
longer available", age and regional blocks). "This video is unavailable" is not
on that list because it is not a removal — it is what YouTube says when it is
rate-limiting you. Deleting the extra branch restores `retry` for
`ERROR`/`UNPLAYABLE`/`CONTENT_CHECK_REQUIRED`.

This is the **fourth** instance this session of the same defect class: *the
correct guard exists and the call site uses the unguarded variant* — after
`innertube_playability_verdict` vs `innertube_last_playability`,
`ApiError.status` vs prose matching, and `res.statusText` leaking prose into a
permanence decision. Three of the four were in the same function family, deciding
permanence from text.

### Red arm — the law can fail, and did

| arm | before the fix | after |
|---|---|---|
| warm produces a snapshot | **0 of 4** | **4 of 4** |
| `_classify_playability("ERROR", "This video is unavailable")` | `"fatal"` | `"retry"` |
| module self-check at `youtube_innertube.py:1849` | pinned `"fatal"` | pins `"retry"` |

The self-check is the interesting one: the module asserted its own wrong answer,
so the process **crashed on import** the moment the branch was deleted. A guard
that pins a bug is not a guard.

### What the fix bought, measured on a fresh server (live HTTP)

| regime | click |
|---|---|
| **snapshot ready (warm completed)** | **15 / 16 / 16 / 31 ms — 4 of 4 inside 3 s** |
| click resolving with no warm running | 2,172–2,797 ms — inside 3 s |
| **click landing while a warm is in flight** | 2,422 · 6,828 · 6,891 · 13,985 ms — **1 of 4** |

### The budgets, and why the obvious fix was wrong

The click's wait for an in-flight warm was `3.5 s + 5.0 s = 8.5 s`, paid
*before* a resolve that costs 2.5 s — so the click could wait longer than the
whole resolve it was waiting to avoid. Three budgets were tried and all three
were measured, not argued:

| wait budget | worst click |
|---|---|
| 0.5 s | 25,813 ms |
| 3.05 s | 25,813 ms |
| 5.8 s (join) | **39,563 ms** |

Waiting longer made it **worse**, because the click then fell through to a
resolve that competed with the warm it had just waited for. Two concurrent
resolves for the same video contend upstream and degrade **both**: 5,625–39,563 ms
together against 2,172–2,797 ms alone. The wait is now 0.5 s and the snapshot
read is what catches a warm that finished — for free.

Capping the resolve budgets (`_PREVIEW_EXTRACT_RACE_SEC` 5.5→2.8,
`_PREVIEW_EXTRACT_FALLBACK_SEC` 24→3, `_PREVIEW_EXTRACT_WAIT_SEC` 24→1.4) was
also tried and **reverted**: the race budget cut off a fast leg that genuinely
needs 2.2–3.9 s, and videos that had been served from a snapshot began
*failing*. A budget that manufactures failures is not a speed-up. `ytdlp_hls.py`
is unchanged from HEAD.

### Still open, stated plainly

**1 of 4.** A click that lands in the same instant a warm is fired still misses
3 s. The warm and the click are the same computation; nothing tried here makes
them share it without one waiting on the other. The honest next step is a single
in-flight resolve that both join, not a shorter timeout.

## A key is not a predicate (2026-10-12, later the same day)

**The line that was supposed to explain every slow click could not be wrong
about anything, because its answer was always the same.** The preview miss log
printed:

```
preview snapshot MISS vid=... h=... pending_warm=%s so the click pays a cold resolve
```

with `bool(_youtube_warm_inflight_key(url))`. That function returns
`extract_video_id(raw) or raw` — **a key**, which is non-empty for any
non-empty URL. So `pending_warm` was `True` on every click, including clicks
with no warm at all.

The line existed precisely to separate "a warm is queued" from "nothing is
running" — two states with different fixes. It could not separate them. This is
the fifth instance of the session's dominant defect class, and the purest one:
not a guard that is never called, but **a guard that always answers the same
thing**, which is indistinguishable from a working guard until you need it.

`youtube_warm_is_pending()` now answers the question the log was asking, and
`test_warm_pending_predicate.py` pins the distinction — including the red arm,
which asserts that the old expression is `True` with nothing registered, so the
test fails if anyone ever re-derives the predicate from the key.

### The budgets, all four of them, measured

The click's wait for an in-flight warm:

| budget | worst click | outcome |
|---|---|---|
| 0.5 s | 25,813 ms | best of the four |
| 3.05 s | 25,813 ms | no better |
| 4.5 s | 4,563 ms | **every** warm-in-flight click failed |
| 5.8 s (join) | 39,563 ms | worst |

The 4.5 s row is the instructive one. It looks like the best number and is
actually the worst result: the click stopped failing *slowly* and started failing
*completely*, because waiting longer walked it into the YouTube bot-gate window
instead of out of it. **A faster failure is not a faster preview.**

### The blocker that is not a design problem

Every failure in these runs carried `YouTube preview is temporarily restricted`
and the API log held 160 `bot-gate cooldown until +1800s (innertube
LOGIN_REQUIRED bot gate)` lines. This box is being rate-limited by YouTube. No
backend budget, cache, or race fixes that: a resolve that YouTube refuses is
refused at any speed. The design work above is what makes the *served* path fast;
the bot gate is what makes the *unserved* path fail, and the two must not be
confused when reading these numbers.
