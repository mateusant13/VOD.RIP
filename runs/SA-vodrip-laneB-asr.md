# SA — VOD.RIP Lane B — preview ASR worker (Twitch/Kick real-time captions)

Seat: WORKER. Slice: local ASR for Twitch + Kick preview captions.
YouTube is **out of scope by owner instruction** (prompt id=192509): YouTube
carries its own captions and another lane owns `backend/routers/subtitles.py`.
I did not touch that file, `backend/routers/preview*.py`, `src/`,
`.steady-watcher.json`, `control/`, or `runs/TICKET-*.md`.

No commit, no `git add`, no `git checkout`, no `git reset` was run. The parent
owns every commit. Paths + changes are in section 6.

---

## 1. WHAT I FOUND IN THE UNTRACKED FILES

Both files were mid-flight and **both were wrong in ways that would have made
the owner's test fail**. Nothing here was correct as inherited.

### `backend/services/preview_asr_sidecar.py` (the supervisor)

| # | Finding | Where | Verdict |
|---|---|---|---|
| 1 | `self._lock = threading.Lock()` held around the ENTIRE request/response cycle — `with self._lock:` wrapping the spawn/retry/roundtrip. **This is the serialisation gate.** | old `preview_asr_sidecar.py:43` + `:115` | **The bug that kills the owner's test.** Three previews queue; three 4 s decodes become 12 s. The 5-second promise dies on a design choice, not on the GPU. |
| 2 | The docstring **advertised the gate as a feature**: "Concurrency: requests are serialised with a lock. Two previews decoding at once would contend for the same CUDA context". | old `:10-14` | Wrong. The CUDA concern is answered by *containment* (separate process), not by a lock. |
| 3 | No first-output ceiling. Only `_REQUEST_TIMEOUT_S = 240.0`, one wall clock, checked once at the end. | old `:35`, `:113` | Exactly the failure the briefing names: 240 s is not fail-fast, and it cannot tell 4.9 s of work from 4.9 s of a hung model. 240 s is also **48× the owner's 5 s budget**. |
| 4 | `_request()` waited with `t.join(timeout)`; on timeout the reader thread was **left alive**, still blocked on `proc.stdout.readline()`. | old `:99-107` | Live defect. That orphan steals the *next* reply line and puts it in a dead box — the pipe is permanently desynced for every later caller. |
| 5 | No cancellation at all. `transcribe_head()` had no cancel token. | old `:160-185` | A caller that gives up could not stop the work; the slot stayed busy. |
| 6 | `stderr=subprocess.DEVNULL` | old `:56` | Threw away the native crash trace this whole file exists to survive. |
| 7 | No `id` on the wire; replies were matched by arrival order — which is *why* the lock existed. | old `:94-111` | Ordering-coupled by construction. |
| 8 | `head_sec` passed straight through to the child, unbounded. | old `:160-177` | A caller asking for 4 hours gets 4 hours. |

What was right and I kept: the out-of-process rationale (measured
`Windows fatal exception: access violation` killing the API on 2026-10-06),
the `permanent`-vs-transient distinction via `_is_remote_permanent_error`, the
respawn-once-then-raise policy, and `sidecar_status()`.

### `backend/services/preview_asr_worker.py` (the child)

| # | Finding | Where | Verdict |
|---|---|---|---|
| 1 | `for raw in sys.stdin:` — one request at a time, single thread, strictly serial. **A second serialisation gate, in the other process.** | old `:137-168` | Even with the supervisor lock removed, three previews would still queue. Both gates had to go. |
| 2 | No cancellation op existed, and the loop could not even *read* one while `_asr` ran. | old `:137` | Genuine wedge: nothing could stop an in-flight decode. |
| 3 | No first-output ceiling anywhere; no silence/stall concept. | whole file | The core of the briefing was absent. |
| 4 | `timeout_s=90.0` hardcoded into the fetch. | old `:95` | 18× the budget, not configurable. |
| 5 | `head_sec` unclamped. | old `:154` | Same defect as supervisor #8. |
| 6 | `parakeet_segments_from_array(...)` called **once for the whole head**; rows appeared only at the very end. | old `:102` | Nothing could be emitted early, so no first-output signal existed even in principle. |
| 7 | `s["words"]` fetched by the helper then **dropped** on the floor. | old `:106-108` | Word timestamps are exactly what a caption strip wants. |
| 8 | `sys.stdout.reconfigure(...)` executed at **import** time. | old `:51-52` | Importing this module from a test would reconfigure pytest's captured stdout. |
| 9 | `time_monotonic()` defined at `:172`, called at `:151`. | old | Worked only by accident of module load order; fragile. |
| 10 | Single monolithic reply: no event stream. | old | The caller cannot time a first caption row. |

---

## 2. WHAT I CHANGED

### `backend/services/preview_asr_worker.py` — full rewrite (child process)

**Bounds, with reasoning, stated in the module:**
- `HEAD_DEFAULT_S = 120.0` — two minutes of captions for a panel that follows
  the playhead from 0; cost is linear in this number.
- `HEAD_MAX_S = 180.0` — hard ceiling. A 4-hour ask is **clamped, not
  obeyed**; the reply carries `head_clamped: true`.
- `CHUNK_DEFAULT_S = 30.0` — decode in slices so rows can land early,
  cancellation is checkable between slices, and a first-output ceiling means
  something at all.
- `FIRST_OUTPUT_DEFAULT_S = 4.0` — under the owner's 5 s so captions that can
  arrive do so while the user watches the start.
- `TOTAL_DEFAULT_S = 20.0` — covers warm GPU (~2-6 s) and CPU (~15-25 s).
- `MAX_CONCURRENT_ASR` = 3 (`VODRIP_PREVIEW_ASR_CONCURRENCY`) — a **ceiling,
  not a lock**.

**Protocol:** every reply carries the `id` it answers. Correlation by `id` is
what makes concurrency safe; ordering is never assumed.

**New ops:** `cancel` (acks with `delivered: true|false`), `asr` now streams
`{"event": "rows", ...}` per landed chunk before its final reply.

**Outcome vocabulary** (the discriminator):
`ok` · `slow` (rows + budget spent, `partial: true`) · `stalled` (ceiling
fired with no row — **silence named as silence**) · `silent` (finished, zero
rows, audio genuinely mute below `_SILENCE_PEAK`) · `cancelled` ·
`permanent` · `unavailable`.

`silent` vs `stalled` is what makes `stalled` mean something: without it,
"stalled" would just mean "no rows".

**Cancellation:** `threading.Event` per request, checked at `pre_fetch`,
`post_fetch`, and at every chunk boundary; `cancelled` stage reported.

**Concurrency:** each request runs on its own thread from a
`BoundedSemaphore(3)`. Per-request `mkdtemp` — no shared file is ever appended
to, so the `O_APPEND` law in AGENTS.md does not bind **by design**.

### `backend/services/preview_asr_sidecar.py` — full rewrite (supervisor)

- **The global lock is gone.** Replaced by: one `_write_lock` held only across
  a single `stdin.write` of one line (a pipe write is not atomic across
  threads), one `_state_lock` guarding the `_proc` swap only, and a single
  **reader thread demultiplexing by `id`** into per-id queues. That is a
  demultiplexer, not a serialiser: it holds nothing while a decode runs.
- **Two ceilings, both enforced and both reported:**
  `first_output_s` → cancel + `SidecarStalled`; `total_s` → cancel +
  `TimeoutError`.
- **Bounded return by form:** every wait is `queue.get(timeout=…)` with its own
  deadline. No `join(timeout)` anywhere in `_roundtrip` — pinned by a test
  that greps the function body.
- **Cancellation is real and escalating:** register the ack queue *before*
  writing (so an ack cannot arrive into a void), send `cancel`, wait
  `_HARD_STOP_GRACE_S` (2.0 s) for the ack, then `terminate()` → `kill()`.
- **New exception types so no caller can collapse them by accident:**
  `SidecarStalled`, `SidecarCancelled`, `SidecarPermanent`,
  `SidecarUnavailable`.
- `stderr` is a **PIPE drained into the app log**, not `DEVNULL`.
- Orphan events (late rows from an abandoned request) are **dropped**, never
  routed to another caller — handing one VOD's words to another is worse than
  being slow.
- A deadline verdict is **not retried**; only transport failures retry, once.
- `sidecar_status()` now reports `inflight`, `first_output_s`, `total_s`.

### `backend/tests/test_preview_asr_worker.py` — new, 23 tests

Fakes `archive_transcribe` **and** `soundfile`, so it runs on a box with no
CUDA and no sherpa-onnx. Deliberate: a test that skips because the model would
not load is a test that never goes red.

### Proposed changes to files I do NOT own

None required for this slice to be correct. One **integration** change the
parent must make when wiring the router (it owns that file):

```python
# backend/routers/preview*.py  (NOT mine)
try:
    out = transcribe_head(platform, video_id, channel, head_sec=120.0)
except SidecarStalled as e:
    -> answer "captions stalled", do NOT retry immediately, do NOT cache as absent
except TimeoutError:
    -> answer "captions slow", eligible for retry
except SidecarPermanent:
    -> "no subtitles", CACHE (settled fact)
except SidecarUnavailable:
    -> infrastructure problem, retryable later
```

---

## 3. TEST EVIDENCE

### The exact command and the real exit code

```
cd "C:\Users\Administrador\Desktop\Nova pasta (3)\TESTE\VOD.RIP\backend"
python -m pytest tests/test_preview_asr_worker.py -q > ..\tmp\laneB-pytest3.txt 2>&1
$LASTEXITCODE   ->  0
```

Real summary line, verbatim from `tmp\laneB-pytest3.txt`:

```
23 passed, 1 warning in 5.02s
```

Re-run with `-v` (exit code `0`), all 23 named tests `PASSED`:

```
test_stalled_model_is_not_reported_as_ok                    PASSED
test_silence_is_not_collapsed_into_slow                     PASSED
test_quiet_audio_is_silent_not_stalled                      PASSED
test_slow_is_reported_with_partial_rows                     PASSED
test_cancel_event_stops_decode_work                         PASSED
test_cancel_before_start_never_touches_the_backend          PASSED
test_cancel_acks_the_supervisor                             PASSED
test_cancel_of_unknown_id_is_not_delivered                  PASSED
test_three_concurrent_asr_requests_overlap                  PASSED
test_each_reply_carries_its_own_id                          PASSED
test_streaming_rows_event_precedes_the_final_reply          PASSED
test_whole_vod_request_is_clamped                           PASSED
test_head_bound_is_configurable_and_ceiling_hard            PASSED
test_head_bound_default_is_documented_in_the_module         PASSED
test_temp_dir_is_per_request_and_removed                    PASSED
test_permanent_error_is_distinguished_from_transient        PASSED
test_supervisor_fires_first_output_ceiling_on_wedged_child  PASSED
test_supervisor_total_ceiling_is_distinct_from_the_stall_ceiling PASSED
test_supervisor_records_first_row_time_on_success           PASSED
test_three_supervisor_requests_do_not_serialise             PASSED
test_supervisor_orphans_do_not_cross_wired                  PASSED
test_transport_failure_is_retried_once_then_raised          PASSED
test_deadline_never_raises_a_bare_timeout_from_join         PASSED
```

### The suite was RED first — not written to be green

The first full run returned **`LASTEXITCODE=1`, `5 failed, 11 passed in 63.09s`**.
Four were bugs in my own test code; **one was a real design finding**, and it is
the most important line in this report:

> `test_stalled_model_is_not_reported_as_ok` failed with
> `stall ceiling must fire on its own clock, took 30.0s`.

My in-worker ceiling was checked **only at chunk boundaries**, so a chunk
already blocked inside native decode sailed straight past it — precisely the
"a cap that only checks at the end" failure the briefing describes. I did not
paper over it. The fix was to make the distinction explicit and testable:
the worker's ceiling is **cooperative** (nothing in Python can interrupt a
native call), and the **supervisor's** ceiling is authoritative because a pipe
is observable no matter what the child thread is doing. That supervisor path
is now covered by `test_supervisor_fires_first_output_ceiling_on_wedged_child`.

### Mutation proof — each assertion, and the edit that turns it red

Every test carries a `MUTATION:` line in its own docstring. I additionally
proved the five load-bearing ones **mechanically**, by applying each mutation
to the real source, running only the test meant to catch it, and confirming it
went red:

```
python tmp/laneB_mutation_proof.py    ->  $LASTEXITCODE = 0

RED  rc=1  test_three_supervisor_requests_do_not_serialise
RED  rc=1  test_supervisor_fires_first_output_ceiling_on_wedged_child
RED  rc=1  test_whole_vod_request_is_clamped
RED  rc=1  test_cancel_event_stops_decode_work
RED  rc=1  test_stalled_model_is_not_reported_as_ok

5/5 mutations turned the suite RED
restored preview_asr_sidecar.py : OK
restored preview_asr_worker.py : OK
```

The five mutations, verbatim:

| Assertion | Mutation | Result |
|---|---|---|
| 3 concurrent previews **overlap** | re-add `with self._work_lock:` around `_roundtrip` — i.e. restore the inherited gate | `peak outstanding == 1` ≠ 3 → RED |
| silence is named silence | `if False and first_row_s is None and now >= deadline_first:` | no stall branch → generic `TimeoutError`, child never cancelled → RED |
| head bound is real | `if h > HEAD_MAX_S: return HEAD_MAX_S, True` → `return h, False` | 4 h passed straight to the fetcher → RED |
| cancellation actually stops work | `_cancel_check("decode")` → `pass` | counter keeps climbing past cancel → RED |
| a hung model is never `ok` | `out.update(ok=False, outcome="stalled", …)` → `ok=True, outcome="ok"` | **the exact lie** → RED |

Full mutation list for the other 18 tests is in the `MUTATION:` docstring of
each test in `backend/tests/test_preview_asr_worker.py`.

### A real defect my own tooling introduced, and how it was caught

My first mutation script restored files with `write_text()`. On Windows that
translates `\n` → `\r\n`, so it "restored" two files into different bytes than
it found them. **The script's own sha256 check caught it** (`!!! CORRUPTED`)
before it could be reported as a pass. Fixed by writing **bytes** back and
normalising anchors in LF space, then re-running: both files now restore
**byte-for-byte identical** (verified by sha256 before and after).

Line-ending note for the parent: `preview_asr_sidecar.py` and
`preview_asr_worker.py` are **CRLF**, matching every other file in
`backend/services/` (`archive_transcribe.py` is also pure CRLF).

---

## 4. POPULATION AND WINDOW

Every number in this report, with its population.

| Number | Meaning | Population / source |
|---|---|---|
| **23 passed** | lane-B tests | exactly `backend/tests/test_preview_asr_worker.py`, run from `backend/`, one file, no deselections |
| **5 failed, 11 passed** | first run | same 23-test file (5 tests were added later), `63.09s` |
| **5/5 mutations red** | mutation proof | exactly the 5 mutations listed in section 3, one targeted test each |
| **3** | `MAX_CONCURRENT_ASR` | `VODRIP_ASR_CONCURRENCY` unset → default `3`, chosen to match the owner's 3-channel test |
| **443 / 535** | LF / CRLF counts | byte scan of my two files; `pureLF=0` ⇒ fully CRLF |
| **2** | files whose bytes were verified after mutation runs | sha256 before/after, both OK |
| **4.0 s** | `FIRST_OUTPUT_DEFAULT_S` | default, `<` the owner's 5 s play budget |
| **20.0 s** | `TOTAL_DEFAULT_S` | warm GPU ~2-6 s, CPU ~15-25 s (repo's own figures) |
| **120 s / 180 s** | head default / hard ceiling | module constants, pinned by test |
| **2.0 s** | `_HARD_STOP_GRACE_S` | cancel-ack wait before `terminate()`/`kill()` |

Inherited figures I did **not** re-measure, and am quoting as the previous
lane's claims: model load **~5.4 s**, and the **2026-10-06**
`Windows fatal exception: access violation` that killed the API.

**Window of evidence:** everything above was produced on 2026-10-06 between
roughly 11:17 and 11:50 local, from a single working tree, with the ASR
backend and `soundfile` both **faked**. No network audio, no CUDA, no Twitch
or Kick endpoint was touched.

---

## 5. WHAT WAS NOT VERIFIED

Being blunt: **the most important thing here is unproven, because it needs a
real network and a real GPU.**

1. **No real ASR was run.** Zero seconds of real audio. Every latency-shaped
   number (`4.0 s`, `20.0 s`, the chunk size) is **reasoned from the repo's
   existing measurements, not measured by me.** If a real warm decode of a
   120 s Twitch head takes 9 s, `FIRST_OUTPUT_DEFAULT_S=4.0` will declare
   healthy work stalled — a false alarm that will look like a bug.
2. **The owner's actual test was not run.** Three real channels
   (Twitch ×2 + Kick ×1), real HLS, real CUDA. My concurrency proof uses a
   fake backend and proves *the code overlaps*, not that *the GPU tolerates
   three overlapping decodes*. That second claim rests on reading
   `archive_transcribe._parakeet_model`, which documents one model per device
   with per-thread `create_stream()`. **Reading it is not measuring it.**
3. **The CUDA-contention question was NOT settled, only de-risked.** The
   previous lane serialised "because two decodes contend for the same CUDA
   context". I removed the lock on the grounds that a native fault in the
   child costs one preview instead of the app. If contention turns out to
   *crash* rather than slow the CUDA context, this reintroduces a crash — in
   a process, so survivable, but not solved. **This is the single biggest
   unknown in the lane.**
4. **Cancellation cannot interrupt native decode.** It fires at chunk
   boundaries, so worst-case overshoot is one chunk. For a 4-hour fetch the
   fetch itself is not cancellable — `_fetch_remote_audio_slice` is called
   without a cancel hook. I bounded it with `timeout_s=total_s`; that is a
   deadline, not a cancellation.
5. **The sidecar was never spawned end-to-end.** No live `python -m
   services.preview_asr_worker` handshake was tested; `_spawn()`/`warm` against
   a real child is unexercised.
6. **The 23 tests all use fakes.** They prove control flow, ceilings,
   cancellation wiring, correlation and bounds. They prove nothing about
   sherpa-onnx, CUDA, HLS, Twitch GQL, Kick's API, or yt-dlp behaviour.
7. **Router integration is untested and unwritten.** No endpoint calls
   `transcribe_head` yet; the mapping in section 2 is a proposal.
8. **I did not run the wider backend suite.** A regression in other tests is
   possible and unmeasured.
9. **No commit was made, by rule.** Nothing here is landed.

---

## 6. OWNERSHIP — files I wrote, for the parent to commit

Untracked, new:

| Path | State |
|---|---|
| `backend/services/preview_asr_sidecar.py` | rewritten (existed untracked) |
| `backend/services/preview_asr_worker.py` | rewritten (existed untracked) |
| `backend/tests/test_preview_asr_worker.py` | new |
| `runs/SA-vodrip-laneB-asr.md` | this report |

Scratch, gitignored, **not for commit**:
`tmp/laneB_mutation_proof.py`, `tmp/laneB-pytest*.txt`,
`tmp/laneB-mut*.txt`, `tmp/laneB-gitstatus.txt`.

I touched **nothing else**. `git status --porcelain` after my work shows the
same set as before: `M .steady-watcher.json`, `M backend/routers/subtitles.py`
(both other lanes'), `?? backend/services/preview_asr_sidecar.py`,
`?? backend/services/preview_asr_worker.py`, `?? control/`, `?? runs/`.
No secret was printed anywhere in this lane.

---

## 7. SELF-AUDIT

**1. Protocols missing.**
- The brief's 4 mandated assertions: all present.
- Missing: a test that the **router** maps `SidecarStalled` to a distinct
  user-visible state (not mine to write).
- Missing: an **integration** test that spawns a real child and runs the
  newline-JSON protocol. This is the largest gap — the protocol's
  wire-level behaviour is covered only through a scripted fake child.

**2. Extra verification beyond the brief.**
- **Mutation proof**: 5 mutations applied to real source, red confirmed, files
  restored sha-identical. The brief asked "what mutation makes it fail"; I ran
  it rather than asserting it.
- **A shа check on my own tooling** that caught a genuine line-ending
  corruption in my mutation script before it could be reported as a pass.
- A test greps `_roundtrip`'s body to forbid `join(` and require
  `q.get(timeout=` — a **mechanical** check on form, not intent.
- A test pins `head_clamped` all the way down to the **fetcher argument**,
  not just the reply field, because a clamp that is reported but not passed
  down is a lie.
- The suite was run `-v` as well as `-q` so every name is independently
  visible.

**3. New checkboxes (named, mechanical).**
1. `laneB:mutation-proof-red` — run `python tmp/laneB_mutation_proof.py`;
   **PASS iff** `$LASTEXITCODE == 0` **and** the last two lines read
   `restored … : OK`. Any `!!! CORRUPTED` is a hard fail.
2. `laneB:no-join-in-roundtrip` — `grep -n "join(" backend/services/preview_asr_sidecar.py`
   between `def _roundtrip` and `def _cancel`; **PASS iff** zero hits.
   (Now also asserted inside the suite.)
3. `laneB:no-global-work-lock` — `grep -n "_work_lock\|with self._lock:" backend/services/preview_asr_sidecar.py`;
   **PASS iff** zero hits.
4. `laneB:crlf-intact` — byte-scan the two service files; **PASS iff**
   `pureLF == 0`.
5. `laneB:three-channel-overlap` — `pytest …::test_three_supervisor_requests_do_not_serialise`;
   **PASS iff** exit 0 **and** the assertion `state["peak"] == 3` is present in
   the body (i.e. concurrency, not completion).

**4. Review by another subagent.**
Not done. I did not spawn a reviewer — the brief made me the worker and I had
no reviewer seat. The mutation proof is a partial substitute (it falsifies, it
does not review), and it cannot catch a wrong *design*, only a wrong
*implementation*. **This is unclosed.**

**5. Confidence, and what moves it.**
- **High** (0.9) on: the ceilings are distinct and correctly named; the global
  lock is gone and three requests provably overlap in code; the head bound is
  enforced and passed to the fetcher; cancellation is wired and provably stops
  work between chunks; no shared-file append anywhere.
- **Medium** (0.5) on: the 4.0 s / 20.0 s defaults being *right* — reasoned,
  not measured. **One real timed decode of a 120 s head moves this hard.**
- **Low** (0.2) on: three concurrent real CUDA decodes being safe on this box.
  Only the real 3-channel test moves this.
- **Unknown** on whether the router will use `SidecarStalled` as a distinct
  state or quietly fold it into "no subtitles" — which would make this lane
  cosmetic. **Owner review of the section 2 mapping moves this.**

**6. What was NOT verified.** Section 5, in full — no real ASR, no real
network, no real CUDA, no live child spawn, no router wiring, no wider-suite
regression run.

**7. Gate-doubt.**
- **`verde-de-verdade:`** — 23 green with real exit code 0, and **5/5
  mutations demonstrably red**, is true green for *control flow only*. It says
  nothing about a real decode: the ceiling constants that decide stall-vs-ok
  in production have never met the model they judge.
- **`falta-no-gate:`** — *three concurrent real CUDA decodes in one sidecar
  process.* Every check in this lane is faked at exactly that boundary. The
  previous lane added a global lock on the belief that this is unsafe; I
  removed it on the belief that containment makes it survivable. **A future
  change that raises `VODRIP_PREVIEW_ASR_CONCURRENCY`, swaps the provider to
  CPU, or moves the decode back in-process crosses this line with nothing
  testing it.** It is a belief, not a measurement.
- **`gate-melhor:`** — add a `VODRIP_PREVIEW_ASR_FAKE=1` mode where the
  sidecar child answers with synthetic rows, then a test that runs the owner's
  exact 3-channel shape end-to-end through a real spawned child and asserts
  **per-request `first_row_s`** (not just completion). That converts today's
  structural proof into a protocol-level one, and it is mechanical.