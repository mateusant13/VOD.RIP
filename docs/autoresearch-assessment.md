# Autoresearch assessment — VOD.RIP

**Verdict: an honest scalar metric EXISTS and is implemented. It is ASR word
error rate (WER) on a frozen TTS fixture. One real iteration was measured and
recorded.**

Date: 2026-09-25. Pattern source: `karpathy/autoresearch` `master`
(<https://raw.githubusercontent.com/karpathy/autoresearch/master/README.md>, MIT).

---

## 1. The load-bearing parts of the pattern, and where each one lives here

`karpathy/autoresearch` is only honest if five pieces are separated. Mapping
each onto this repo:

| Pattern piece | This repo | Where it physically lives |
|---|---|---|
| one mutable file the agent may edit | the ASR text path — `backend/services/archive_transcribe.py`, `backend/services/transcript_fix.py` | inside the repo, listed in `MUTABLE` in `scripts/autoresearch-loop.sh` |
| an evaluator **outside** the agent's write scope | `wer_eval.py` (scoring + fixture) | `G:\vodrip-bench\asr-wer\` — **not** in the repo |
| a fixed comparable budget | fixture sha `9ba2191f…` + pinned CUDA lane | `fixture.sha256`, `PINNED_LANE` |
| keep-or-reset by ONE scalar, reset via a git operation | `WER_PCT`, `git restore --source=$BASE -- <MUTABLE>` | `cmd_decide` |
| a results ledger | `ledger.tsv` | `G:\vodrip-bench\asr-wer\` (outside the repo) |

The separation is the entire point. An evaluator that lives in the repo is an
evaluator the program can edit, and a metric the program can edit is not a
metric.

## 2. Candidate metrics — what was CHECKED, not assumed

The brief listed four candidates. Results of actually inspecting each:

**ASR WER on a fixed audio fixture — ADOPTED.** The repo already had the two
halves needed. `backend/tests/test_archive_transcribe_e2e_real.py:64` proves
Windows `System.Speech` can synthesize speech offline with **known text**, and
`services/archive_transcribe.py:1-49` documents parakeet
(`sherpa-onnx nemo_transducer TDT v3 int8`) as the only ASR engine. Known
spoken text + a real engine = an exact ground truth that never comes from the
model being scored.

**Transcription speed (x-realtime) — REJECTED as the decision scalar.**
`bench_one.py:296-313` already prints `WALL_SEC` and `XRT`, so it is computable.
It is not usable as *the* metric: it is trivially gamed by doing less work
(skipping chunks, shrinking the VAD threshold, cutting model size), and it is
host-noisy. It is recorded in the evaluator output as a non-decisional
observability column only.

**Download throughput — REJECTED.** Not comparable across runs (CDN variance,
time of day) and not something an agent can improve honestly.

**Existing gate / fixture pass count — REJECTED.** `test-results/.last-run.json`
and the pytest suite give a count, but the agent edits that same code *and*
those tests. It is a self-graded metric.

## 3. The metric command, and its real output

```
$ cd "G:/vodrip-bench/asr-wer" && python wer_eval.py --measure
```

```
WER_PCT: 6.06
EDITS: 2
REF_WORDS: 33
HYP_WORDS: 32
SEGMENTS: 2
AUDIO_SEC: 14.11
WALL_SEC: 15.06
XRT: 0.94
LANE: cuda
ENGINE: parakeet
FIXTURE_SHA: 9ba2191f722315784cd5e305649fad68618e42e0c4b69e0cc0610b9b97f723f3
REF_TEXT: welcome to the vod dot rip archive system this is a test second speech
          segment spoken after a short pause the quick brown fox jumps over the
          lazy dog near the river bank
HYP_TEXT: Welcome to the VOD.rip Archive system. This is a test. Second speech
          segment spoken after a short pause. The quick brown fox jumps over
          the lazy dog near the Riven bank.
```

Lower is better. The two errors are `river`→`Riven` and `VOD.rip`→`VOD dot rip`
(an artifact of the reference writing the name as spoken letters).

### Why it satisfies the three requirements

**(a) Computable by a command that already exists.** It drives the real
`run_worker(once=True, …)` queue — the same entry point
`bench_one.py:277` uses — against a scratch DB. No new inference path, no mock.
Pre-flight, the queue plumbing was validated without loading the model:
`leaked jobs: 0`, `jobs: [('wer-plumb01', 'queued')]`, `engine: parakeet`,
`device: ('cuda', 'int8')`.

**(b) Comparable across runs.** Pinned on both axes that move the number: the
audio fixture (sha-verified on every run, drift is fatal) and the compute lane
(`provider=cuda`; the evaluator exits 2 on any other lane, and the loop turns
that into a refusal so a non-comparable run can never enter the ledger).
Measured determinism — three independent `--measure` invocations:

```
WER_PCT: 6.06  EDITS: 2  LANE: cuda  FIXTURE_SHA: 9ba2191f…   (run 1)
WER_PCT: 6.06  EDITS: 2  LANE: cuda  FIXTURE_SHA: 9ba2191f…   (run 2)
WER_PCT: 6.06  EDITS: 2  LANE: cuda  FIXTURE_SHA: 9ba2191f…   (run 3)
```

**(c) Not gameable by editing the code.** The scoring function
(`normalize_tokens`, `levenshtein`) and the reference text live in the
evaluator, outside the write scope. The reference is the literal string fed to
the TTS engine — it is not the ASR output, so the model cannot grade its own
homework. Normalization is deliberately **not** imported from
`services/transcript_fix.py`, which is inside `MUTABLE`: importing it would
hand the program its own scoring function. The evaluator's sha256 is pinned in
the loop script and checked on every run.

## 4. The loop

`scripts/autoresearch-loop.sh` — `measure` | `accept` | `decide` | `status`.

### Guards (every one exercised, see §6)

1. **Never on main.** Refuses on `main`/`master`.
2. **Never discards work it did not create.** The revert is
   `git restore --source="$BASE" -- "${MUTABLE[@]}"` — bounded to the two
   mutable files. There is no `git reset --hard`, no `git clean`, no
   `git checkout .` anywhere in the script.
3. **Dirty work outside the mutable set is a refusal.** The guard is scoped to
   `MUTABLE`, and that scoping is load-bearing, not cosmetic: a blanket
   dirty-tree refusal makes the loop unable to do its job, because the agent's
   uncommitted edit *is* the experiment under evaluation. First draft had the
   blanket check and deadlocked — see §6.
4. **Evaluator sha pinned**, unconditionally.
5. **Fixture sha pinned** inside the evaluator.

### One recorded iteration

`G:\vodrip-bench\asr-wer\ledger.tsv`:

```
iter	time	status	sha	wer_pct	commit	branch
1	2026-09-25T13:53:51Z	baseline	9ba2191f722315784cd5e305649fad68618e42e0c4b69e0cc0610b9b97f723f3	6.06	2bafa0d	autoresearch/asr-wer
2	2026-09-25T13:54:42Z	reset	9ba2191f722315784cd5e305649fad68618e42e0c4b69e0cc0610b9b97f723f3	6.06	6f5ea10	autoresearch/asr-wer
3	2026-09-25T13:55:36Z	keep	9ba2191f722315784cd5e305649fad68618e42e0c4b69e0cc0610b9b97f723f3	6.06	7a409cf	autoresearch/asr-wer
```

Iteration 1 is the real baseline. Rows 2 and 3 are the branch-coverage runs
described in §6 — row 2 is a genuine tie→reset, row 3's `keep` was produced by
raising the recorded best to 7.00 so that the unchanged 6.06 run would take
the keep path. **No row claims a WER gain the pipeline did not actually
produce**; the 7.00 was edited back to 6.06 afterwards and `status` reports
`best WER: 6.06%`.

## 5. What this metric does NOT tell you

Stated plainly, because a benchmark that oversells itself is worse than none:

- **33 reference words.** Enough to catch a regression that breaks
  transcription outright; far too few to resolve a 1–2 % improvement. A real
  optimization run needs a corpus 10–100× larger, ideally multiple voices and
  noise conditions. The current fixture is a *smoke test with a number*, not a
  tuning benchmark.
- **Clean synthetic TTS only.** No real VOD audio, no music, no overlapping
  speech, no accents. The `G:\vodrip-bench\audio-a.wav` fixture has no ground
  truth, so it cannot be scored.
- **One language, one engine.** en-US via parakeet. No coverage of the pt-BR
  path that most users actually hit.
- **A hardcoded transcript still scores 0 %.** A program that ignores its input
  and returns the reference would win. The lane/fixture pins do not stop that.
  Closing it needs a held-out fixture the program never sees, rotated per
  iteration.
- **The keep/reset decision is mechanical, not semantic.** A change that
  improves WER while breaking resume, progress reporting, or the disk-hygiene
  contract is kept. Run the repo's ASR test suite alongside `decide`.

## 6. Verification actually performed

| Check | Result |
|---|---|
| `bash -n` syntax | OK |
| Guard: on `main` | `REFUSED: on 'main' …` rc=3 |
| Guard: evaluator tampered | `REFUSED: evaluator sha drift` rc=3 |
| Guard: dirty outside mutable set | `REFUSED: uncommitted work OUTSIDE the mutable set` rc=3 |
| `accept` → baseline | recorded 6.06 % |
| `decide` tie → **RESET** | `reverted … to 2bafa0d` |
| Revert bound | committed `notes-untracked.txt` **survived** the reset |
| `decide` improvement → **KEEP** | `KEEP: WER 6.06% < best 7.00%` |
| KEEP with empty diff | `no file change in the mutable set` (no `set -e` abort) |
| Metric determinism | 6.06 % on 3 independent runs |
| Scoring math | 5 self-test cases, `--selfcheck` |

### Three real defects found and fixed during this work

1. **Fixture spoke the wrong language.** The default `System.Speech` voice on
   this box is *Microsoft Maria (pt-BR)*. The first build read English
   reference sentences with Portuguese phonetics and parakeet returned
   `"O Elcante é o devoto da terra e Par Kai'Sa, dizes a testa."` for
   `"Welcome to the VOD dot RIP archive system"` — WER 90 %+ that measured the
   TTS voice, not the pipeline. Fixed with an explicit `SelectVoice` +
   fail-loud check. That first run was **discarded, not recorded**; it is the
   concrete argument for pinning the fixture.
2. **The sha pin was inert.** The guard read
   `if [ -n "${AR_EVAL_SHA:-}" ]`, so on a normal run it never executed — a
   tampered evaluator passed cleanly. Now unconditional. Caught by testing the
   guard rather than reading it.
3. **The dirty guard deadlocked the loop.** Blanket dirty-tree refusal rejects
   the agent's in-flight edit, which is exactly what must be measured. Rescoped
   to work outside `MUTABLE`.

## 7. Paths touched

**Repo** (`C:/Users/Administrador/Desktop/Nova pasta (3)/TESTE/VOD.RIP`):

- `scripts/autoresearch-loop.sh` — **new**, the loop.
- `docs/autoresearch-assessment.md` — **new**, this file.

Both are **uncommitted on `main`** in the main checkout, as required (the loop
never commits to main). The main checkout's pre-existing
`.steady-watcher.json` modification was left untouched.

**Outside the repo** (evaluator + ledger, deliberately not in the checkout):

- `G:\vodrip-bench\asr-wer\wer_eval.py` — new evaluator.
- `G:\vodrip-bench\asr-wer\fixture.wav`, `reference.txt`, `fixture.sha256`
- `G:\vodrip-bench\asr-wer\ledger.tsv`
- `G:\vodrip-bench\asr-wer\last_measure.json`, `whisper_manifest\`

**Test worktree**, created for verification and left in place on branch
`autoresearch/asr-wer` (3 commits, clean tree):
`I:\Temp\wt-ar` — remove with
`git worktree remove I:/Temp/wt-ar && git branch -D autoresearch/asr-wer`
from the main checkout.

## 8. If you want to make this a real optimization loop

In priority order:

1. **Grow the corpus.** 10+ sentences × several voices, plus `audio-a.wav`
   with a hand-checked reference. Until then, treat 6.06 % as a tripwire, not
   a score.
2. **Add a held-out fixture** the program never reads, rotated each iteration.
   This is what stops the hardcoded-transcript exploit in §5.
3. **Gate `decide` on the ASR test suite** as well as WER, so a WER win that
   breaks the resume or disk-hygiene contract is rejected.
4. **Multi-metric tiebreak.** Keep WER as the decision scalar, but refuse to
   keep an iteration whose x-realtime regresses by more than ~20 % — that
   catches "transcribe less" as an improvement.

---

## SELF-AUDIT

- **protocolos em falta** — none material. I treated "measure, do not guess"
  as load-bearing (each candidate metric was inspected, three were rejected on
  mechanism, not taste) and I read `karpathy/autoresearch` for the pattern
  rather than working from memory. The one I drifted on initially: I wrote the
  evaluator before deciding it must live outside the repo, then had to reason
  backwards about why. Deciding the write-scope split first would have been
  cleaner.
- **verificacao adicional** — a second independent WER run after the final
  header edit, to confirm the last edit did not perturb the pinned sha path.
  Cheap (16 s); I ran the guard smoke instead, which exercises the same sha
  check without the ASR cost. The ASR number itself was already confirmed
  deterministic across 3 runs.
- **checkboxes novas** — (1) after building a fixture from any TTS, assert the
  synthesizer's voice culture matches the reference language, and fail loudly
  otherwise — this would have caught defect #1 before the ASR run, not after.
  (2) For every guard in a harness, run it with the guard's own precondition
  deliberately violated (tamper the file, not just read the code) — that is what
  exposed the inert sha pin.
- **review por outro subagente** — `sim-com-escopo`: the evaluator's scoring
  function (`normalize_tokens` / `levenshtein` / the `strict improvement`
  threshold in `cmd_decide`) and the WER requirements-(a)/(b)/(c) argument.
  Those are the parts where I could be systematically wrong in a way my own
  runs cannot catch, since the same reasoning produced both the metric and its
  justification. The bash guards I would not re-review; each was executed and
  its refusal observed.
- gate-doubt:
  - verde-de-verdade: the guards were real, not vacuous — each refusal was
    observed with its own output and a non-zero rc, and each was provoked by
    actually violating the precondition (toggling the evaluator file, editing
    `transcript_fix.py`, running on `main`). The one that was NOT real was the
    evaluator sha pin: it printed a plausible guard and was inert because of
    the `if [ -n "${AR_EVAL_SHA:-}" ]` wrapper. I only know it was inert
    because I tampered with the file and it let me through. The determinism
    claim is weaker than it looks: three runs at 6.06 % could be a genuinely
    deterministic pipeline or a cached/memoized result path; I did not prove
    the transcript is recomputed from the wav on every run.
  - falta-no-gate: nothing verifies that WER *improvement* corresponds to
    *better transcription* rather than a different-but-equally-wrong output.
    A future change that makes the pipeline emit the reference text verbatim
    scores 0 % and passes every gate here. A held-out fixture is the missing
    instrument; §5 and §8 name it.
  - `gate-melhor:` a held-out second fixture, sha-pinned and rotated per
    iteration, scored but never shown to the program. RED command:
    `AR_FIXTURE=G:/vodrip-bench/asr-wer/heldout.wav python wer_eval.py --measure`
    with a fixture the agent has never read — a hardcoded transcript scores 0 %
    on the visible fixture and non-zero on the held-out one, so the keep
    decision goes RED. The evaluator's `_verify_fixture()` already enforces a
    sha; pointing it at a second pinned file is the whole change.
- **confianca** — `alta` on "an honest metric exists and the loop is
  mechanically sound" (measured, deterministic, guards executed). `media` on
  "6.06 % is a meaningful quality number" — 33 words of clean synthetic TTS is
  a tripwire, not a benchmark, and the doc says so. Would rise to `alta` with
  a 10× corpus and a held-out fixture.
- **nao verificado** — that a real WER *improvement* keeps (the keep path was
  reached by raising the recorded best to 7.00, not by a genuine gain; a true
  improvement is untested). That the transcript is recomputed rather than
  memoized across runs. Multi-iteration behavior over more than 3 rows. The
  pt-BR / real-VOD-audio paths (no ground truth exists for `audio-a.wav`).
  Windows PowerShell was used for all shell work; behavior under cmd.exe or
  WSL is unverified. The worktree cleanup command in §7 was not executed.
