#!/usr/bin/env bash
# autoresearch-loop.sh — one measured iteration of the karpathy/autoresearch
# pattern for the VOD.RIP ASR path.
#
# PATTERN (https://github.com/karpathy/autoresearch, branch master, MIT):
#   program   = the VOD.RIP checkout, restricted to MUTABLE below. The agent
#               edits this and nothing else.
#   evaluator = G:/vodrip-bench/asr-wer/wer_eval.py — OUTSIDE the repo, so the
#               program cannot edit the thing that scores it. Its sha256 is
#               pinned below; a changed evaluator is a refusal, not a warning.
#   budget    = FIXTURE_SHA + the pinned CUDA lane. Comparable across runs.
#   decision  = ONE scalar, WER (lower is better). Keep on strict improvement,
#               otherwise revert. Git does the revert — no bespoke undo.
#   ledger    = TSV, one row per iteration, appended outside the repo.
#
# GUARDS (all refusal paths, checked BEFORE any mutation):
#   1. uncommitted work OUTSIDE the mutable set -> refuse. Inside the mutable
#      set, dirt IS the experiment under evaluation, so it is allowed (and is
#      what `decide` may revert on a regression).
#   2. never `git reset --hard`, never `git checkout .`, never `git clean`.
#      The revert is `git restore --source="$BASE" -- <MUTABLE paths only>`,
#                               which cannot touch anything outside the scope
#                               the agent was allowed to edit.
#   3. never on main/master  -> refuse. Work happens on the current feature
#                               branch or a dedicated worktree branch.
#   4. evaluator sha pinned  -> refuse on drift (the metric must be fixed).
#   5. fixture sha pinned    -> enforced inside the evaluator; a changed audio
#                               fixture makes every older ledger row void.
#
# USAGE
#   scripts/autoresearch-loop.sh measure     # run the evaluator, print WER
#   scripts/autoresearch-loop.sh decide      # keep-or-reset vs the best row
#   scripts/autoresearch-loop.sh status      # ledger + current best
#   scripts/autoresearch-loop.sh accept      # record the CURRENT state as the
#                                            # baseline (first run only)
set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"
EVAL_DIR="${AR_EVAL_DIR:-G:/vodrip-bench/asr-wer}"
EVAL_PY="$EVAL_DIR/wer_eval.py"
LEDGER="$EVAL_DIR/ledger.tsv"
# sha256 of the evaluator as reviewed. Re-pin deliberately (and note it in the
# report) — never silently.
EXPECTED_EVAL_SHA="${AR_EVAL_SHA:-1d0723c258840fe0eeaef7dc8343fb6931d6823477d0f431c977c06fe22773eb}"

# The program's write scope. The revert in `decide` is bounded by this list, so
# a mistake here widens what the loop can destroy — keep it to the ASR text
# path, never the evaluator, never CI, never build config.
MUTABLE=(
  "backend/services/archive_transcribe.py"
  "backend/services/transcript_fix.py"
)

# grep -E alternation of the same list, derived so the guard and the revert
# can never drift apart (one source of truth for the write scope).
MUTABLE_PAT="$(printf '%s|' "${MUTABLE[@]}")"
MUTABLE_PAT="${MUTABLE_PAT%|}"

die() { printf '%s\n' "REFUSED: $*" >&2; exit 3; }
say() { printf '%s\n' "$*"; }

sha_of() {
  if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | cut -d' ' -f1
  else shasum -a 256 "$1" | cut -d' ' -f1; fi
}

# --- guards -----------------------------------------------------------------
guard_preconditions() {
  [ -f "$EVAL_PY" ] || die "evaluator missing: $EVAL_PY (the metric must exist
       outside the repo; the loop does not create or guess one)"

  local branch
  branch="$(git rev-parse --abbrev-ref HEAD)"
  case "$branch" in
    main|master) die "on '$branch' — the loop never commits to main.
       Create a feature branch or a worktree branch first:
         git switch -c autoresearch/asr-wer" ;;
  esac

  # Scope of the guard is the MUTABLE set, not the whole tree — and the
  # distinction is load-bearing. A blanket `git status` refusal made the loop
  # unable to do its job: the agent's UNCOMMITTED edit inside MUTABLE is
  # precisely the thing being judged, so refusing on it is a deadlock.
  #
  # So: work inside MUTABLE is the experiment (allowed, and it is what `decide`
  # may revert). Anything dirty OUTSIDE MUTABLE is work this loop did not
  # create and must never touch -> refuse, and name the paths.
  local dirty_out
  dirty_out="$(git status --porcelain --untracked-files=all \
    | grep -vE "^.. (${MUTABLE_PAT// /|})$" || true)"
  if [ -n "$dirty_out" ]; then
    printf '%s\n' "$dirty_out" >&2
    die "uncommitted work OUTSIDE the mutable set — the loop will not measure
       around it and will never revert it. Commit or stash it, then re-run.
       (Dirty files inside ${MUTABLE[*]} are the experiment and are fine.)"
  fi

  # Unconditional on purpose: keeping the evaluator outside the repo is only
  # worth something if the loop actually checks it. An earlier revision wrapped
  # this in `if [ -n "${AR_EVAL_SHA:-}" ]`, which left the pin INERT on a normal
  # run — a tampered evaluator sailed straight through. It must fire every time.
  local got
  got="$(sha_of "$EVAL_PY")"
  [ "$got" = "$EXPECTED_EVAL_SHA" ] || die "evaluator sha drift:
       pinned   $EXPECTED_EVAL_SHA
       on disk  $got
       The metric must stay fixed across iterations. Re-pin deliberately with
       AR_EVAL_SHA=<newsha> and record why in the report."
}

# --- subcommands ------------------------------------------------------------
cmd_measure() {
  guard_preconditions
  say "--- measure (evaluator: $EVAL_PY)"
  say "commit $(git rev-parse --short HEAD)  branch $(git rev-parse --abbrev-ref HEAD)"
  # Exit 2 from the evaluator = lane deviation = NOT comparable. Surface it as
  # a refusal: a run on a different lane must never enter the ledger.
  local rc=0
  ( cd "$EVAL_DIR" && python wer_eval.py --measure ) || rc=$?
  [ "$rc" -eq 2 ] && die "evaluator reported a lane deviation — result is not
       comparable with the ledger; nothing recorded."
  [ "$rc" -eq 0 ] || die "evaluator failed (rc=$rc); nothing recorded."
}

# Best = lowest WER among rows whose status is keep|baseline.
best_wer() {
  [ -f "$LEDGER" ] || { echo ""; return 0; }
  awk -F'\t' 'NR>1 && ($3=="keep"||$3=="baseline") && $5!="" {print $5}' "$LEDGER" \
    | sort -g | head -1
}

cmd_accept() {
  guard_preconditions
  [ -f "$LEDGER" ] && die "ledger already exists ($LEDGER) — accept is for the
       first run only; later decisions use 'decide'."
  local wer; wer="$(cmd_measure | awk -F': ' '/^WER_PCT/{print $2}')"
  [ -n "$wer" ] || die "no WER_PCT in evaluator output"
  printf 'iter\ttime\tstatus\tsha\twer_pct\tcommit\tbranch\n' > "$LEDGER"
  printf '1\t%s\tbaseline\t%s\t%s\t%s\t%s\n' \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    "$(sha_of "$EVAL_DIR/fixture.wav")" "$wer" \
    "$(git rev-parse --short HEAD)" "$(git rev-parse --abbrev-ref HEAD)" >> "$LEDGER"
  say "baseline recorded: WER ${wer}%"
}

cmd_decide() {
  guard_preconditions
  [ -f "$LEDGER" ] || die "no ledger — run 'accept' on the first iteration."
  local wer best iter
  wer="$(cmd_measure | awk -F': ' '/^WER_PCT/{print $2}')"
  [ -n "$wer" ] || die "no WER_PCT in evaluator output"
  best="$(best_wer)"
  [ -n "$best" ] || die "no baseline row in the ledger."
  iter=$(( $(wc -l < "$LEDGER") ))

  # Strict improvement keeps. Ties and regressions reset: a tie is noise from a
  # nondeterministic lane, and keeping it would let a no-op iteration win.
  if awk -v a="$wer" -v b="$best" 'BEGIN{exit !(a < b - 0.0001)}'; then
    say "KEEP: WER ${wer}% < best ${best}%"
    git add -- "${MUTABLE[@]}"
    # A strict improvement with an empty diff (baseline was recorded on a noisy
    # run) must not abort under `set -e`: there is nothing to commit, and the
    # row still belongs in the ledger.
    if git diff --cached --quiet -- "${MUTABLE[@]}"; then
      say "  (no file change in the mutable set — improved run, empty diff)"
    else
      git commit -q -m "autoresearch(asr): WER ${wer}% (was ${best}%)" -- "${MUTABLE[@]}"
    fi
    printf '%s\t%s\tkeep\t%s\t%s\t%s\t%s\n' \
      "$iter" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
      "$(sha_of "$EVAL_DIR/fixture.wav")" "$wer" \
      "$(git rev-parse --short HEAD)" "$(git rev-parse --abbrev-ref HEAD)" >> "$LEDGER"
  else
    say "RESET: WER ${wer}% did not beat ${best}%"
    # Bounded revert. No --hard, no clean, no checkout of the whole tree: only
    # the paths the agent was permitted to touch go back to the baseline commit.
    local base; base="$(awk -F'\t' 'NR>1 && $3=="baseline"{print $6; exit}' "$LEDGER")"
    [ -n "$base" ] || die "no baseline commit recorded in the ledger."
    git restore --source="$base" -- "${MUTABLE[@]}"
    printf '%s\t%s\treset\t%s\t%s\t%s\t%s\n' \
      "$iter" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
      "$(sha_of "$EVAL_DIR/fixture.wav")" "$wer" \
      "$(git rev-parse --short HEAD)" "$(git rev-parse --abbrev-ref HEAD)" >> "$LEDGER"
    say "reverted ${MUTABLE[*]} to $base"
  fi
  say "--- ledger"; cat "$LEDGER"
}

cmd_status() {
  [ -f "$LEDGER" ] || die "no ledger at $LEDGER — nothing has been measured yet."
  say "--- ledger ($LEDGER)"; cat "$LEDGER"
  local b; b="$(best_wer)"
  say "--- best WER: ${b:-none}%"
}

case "${1:-}" in
  measure) cmd_measure ;;
  accept)  cmd_accept ;;
  decide)  cmd_decide ;;
  status)  cmd_status ;;
  *) sed -n '/^# USAGE/,/^set -euo/p' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac
