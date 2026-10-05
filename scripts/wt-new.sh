#!/usr/bin/env bash
# scripts/wt-new.sh — the SANCTIONED worktree-creation path for VOD.RIP.
#
# PROVENANCE — PORTED 2026-10-05 from the house's sanctioned tool:
#   I:\!manager\scripts\wt-new.sh   (authoritative upstream, NOT modified)
#   I:\!manager\scripts\lib\worktree-roots.sh  (ported to scripts/lib/, same commit)
#   I:\!manager\worktree-roots.tsv (NOT modified; superseded by the repo-scoped
#                                    scripts/worktree-roots.tsv, see that file's header)
#
# !!! EVERY REFUSAL BELOW IS LOAD-BEARING. DO NOT REMOVE ONE TO MAKE A LANE PASS. !!!
#   Each exists because the wrong thing was MEASURED to succeed. `git` has no concept
#   of a correct worktree location, so this wrapper is the only CHOKEPOINT; a deleted
#   guard is a permanent hole, not a cleanup. The full port diff is in
#   `wtnew_port_note` (scripts/lib/worktree-roots.sh) — read it before changing this.
#
# THE QUESTION THIS ANSWERS
#   Owner, 2026-09-25: "o que impede a gente de fazer worktree no lugar errado e
#   prosseguir?" MEASURED: nothing. `git -C G:/superharness worktree add
#   I:/Temp/wrong-place-wt -b tmp/x` returned rc=0 and registered the worktree.
#   Every other actor — shell, PowerShell, cargo, the janitor, a TUI — calls git
#   directly. So the missing piece is a CHOKEPOINT, not a warning: this wrapper.
#
#   VOD.RIP-SPECIFIC: this repo previously had scripts/wt-new.ps1, which took
#   `-Root I:\TEMP` as a CALLER ARGUMENT (the exact hole) and which created a
#   `node_modules` JUNCTION per lane. Those junctions are why 10 of the 42 registered
#   worktrees can never be `git worktree move`d, and `git worktree remove` destroyed a
#   dependency tree through one. This tool creates no junction and refuses instead.
#
# USAGE
#   wt-new.sh --project <id> --lane <name> [--state wip|unsure|done|archive]
#                                        [--base <ref>] [--branch <name>]
#                                        [--ensure-roots] [--dry-run]
#   wt-new.sh --roots-only --project <id>      # create the state DIRS only
#   wt-new.sh --selftest                       # fixture arm + VOD.RIP manifest arm
#   wt-new.sh --help
#
#   --state defaults to wip. --base defaults to the repo's current HEAD (detached
#   only if --branch is omitted AND the base is a raw sha; see BRANCH below).
#   --ensure-roots creates <root>/{wip,unsure,done,archive} if missing (DIRECTORIES
#   only, never a worktree).
#   --roots-only creates those four directories and NOTHING else — the ROLLOUT step.
#
# REFUSALS (each prints a reason and exits non-zero — the whole point of the tool)
#   * unknown project, empty lane, lane with a path separator, bad state
#   * the resolved destination is not under the project's declared worktree root
#   * the destination's `<name>-wt` convention root would sit on a DIFFERENT drive
#     from the project's declared root (cross-drive placement is legal in git, but a
#     future `git worktree move` is same-drive-only, so `archive/` must share the
#     drive — see the manifest's DRIVE RULE)
#   * an existing GIT worktree would be landed on (a registered row, or a dir that
#     is itself a worktree of ANY repo) — never nest a worktree inside another
#   * the lane dir already exists and is non-empty
#   * a `*-wt` root exists on another drive for this project AND holds REGISTERED
#     worktrees (an empty leftover dir is not a competing home — upstream fix,
#     2026-10-02; VOD.RIP has a live case: G:/vod-rip-wt exists, 0 registered)
#   * a branch that already exists, when a new one was requested
#
# BRANCH — and the trap that makes it non-obvious
#   `git worktree add <dir> -b <branch>` takes `-b` from the (project, lane), so two
#   runs for the same lane collide on the ref. Fine: that IS the same lane.
#   The trap is the BASE. In a linked worktree HEAD is the lane branch, and on the
#   main checkout it may be detached (MEASURED on G:/superharness: detached HEAD).
#   Passing a bare HEAD/detached value as the base therefore fails with
#   "not a valid object name: 'HEAD'". So the base is resolved to a concrete COMMIT
#   (`rev-parse --verify <base>^{commit}`) before git sees it, and a detached HEAD is
#   reported rather than guessed at.
#
# CREATION IS DONE HERE, THROUGH GIT. This script runs `git worktree add` itself, so
# the caller has no path argument to get wrong. There is no fallback, no `|| true`,
# and no path echo without a verified registry row afterwards.
#
# SIBLING TOOLS (same directory, same manifest, same refusals)
#   wt-migrate.sh  — move an EXISTING registered worktree into a state directory,
#                    with a junction refusal and a rollback ledger row.
#   wt-restore.sh  — move a worktree BACK out of a state directory, using that ledger.
set -u
set -o pipefail

SELF="${BASH_SOURCE[0]}"
SCRIPT_DIR="$(cd "$(dirname "$SELF")" && pwd)"
# shellcheck source=lib/worktree-roots.sh
. "$SCRIPT_DIR/lib/worktree-roots.sh"

PROJECT=""
LANE=""
STATE="wip"
BASE=""
BRANCH=""
ENSURE_ROOTS=0
ROOTS_ONLY=0
DRY_RUN=0
MODE=create

# PORT DIFF vs upstream: upstream hardcoded `sed -n '2,52p'`, a line range that
# silently rots the moment the header grows. This prints the contiguous comment block
# after line 1 instead. Cosmetic only — no refusal touched.
usage() { awk 'NR==1{next} /^#/||/^[[:space:]]*$/{sub(/^# ?/,"");print;next} {exit}' "$SELF"; }
refuse() { printf 'wt-new: REFUSED: %s\n' "$*" >&2; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    --project) PROJECT="${2:-}"; shift 2 ;;
    --lane)    LANE="${2:-}"; shift 2 ;;
    --state)   STATE="${2:-}"; shift 2 ;;
    --base)    BASE="${2:-}"; shift 2 ;;
    --branch)  BRANCH="${2:-}"; shift 2 ;;
    --ensure-roots) ENSURE_ROOTS=1; shift ;;
    # --roots-only creates <root>/{wip,unsure,done,archive} and NOTHING else (no
    # worktree, no branch). This is the portfolio ROLLOUT step: the root layout is
    # the wrapper's to own, so it is not an ad-hoc mkdir loop in a caller.
    --roots-only) ROOTS_ONLY=1; ENSURE_ROOTS=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    --selftest) MODE=selftest; shift ;;
    -h|--help) usage; exit 0 ;;
    *) refuse "unknown argument '$1' (the destination is NOT an argument — that is the point)" ;;
  esac
done

# ---------------------------------------------------------------- resolve + validate
resolve() { # sets REPO ROOT DEST CEILING or refuses
  [ -n "$PROJECT" ] || { usage >&2; refuse "missing --project"; }
  [ "$ROOTS_ONLY" = 1 ] || [ -n "$LANE" ] || refuse "missing --lane"

  local row
  row="$(wtroot_row "$PROJECT")" || refuse "unknown project '$PROJECT' (not in manifest $(wtroot_manifest))"
  REPO="$(printf '%s' "$row" | cut -f1)"
  ROOT="$(printf '%s' "$row" | cut -f2)"
  CEILING="$(printf '%s' "$row" | cut -f3)"

  [ -d "$REPO" ] || refuse "project '$PROJECT' repo root does not exist: $REPO"
  git -C "$REPO" rev-parse --git-dir >/dev/null 2>&1 \
    || refuse "project '$PROJECT' repo root is not a git repository: $REPO"

  # A malformed root declaration makes every containment test below meaningless.
  wtroot_validate_root "$ROOT" \
    || refuse "manifest declares a malformed worktree_root for '$PROJECT': '$ROOT'"

  [ "$ROOTS_ONLY" = 1 ] && { DEST=""; return 0; }

  DEST="$(wtroot_resolve_destination "$PROJECT" "$STATE" "$LANE")" || exit 1   # message already printed

  # RULE B, applied to the thing we are about to create: it MUST be inside the root.
  wtroot_inside "$DEST" "$ROOT" \
    || refuse "destination '$DEST' is outside the declared worktree root '$ROOT' for '$PROJECT'"

  # Cross-drive: the convention root of the destination must share the declared
  # root's drive, or a later same-drive-only archive/move cannot work.
  local dd rd
  dd="$(wtroot_norm "$DEST")"; rd="$(wtroot_norm "$ROOT")"
  [ "${dd%%/*}" = "${rd%%/*}" ] \
    || refuse "cross-drive placement: destination drive '${dd%%/*}' != declared root drive '${rd%%/*}'"

  # Ambiguity wall: if a `<project>-wt` root already exists on ANOTHER drive, the
  # project has two candidate homes and that must be resolved, not silently picked.
  #
  # CORRECTED 2026-10-02 (runs/2026-10-02-worktree-topology-repair.md): the test
  # used to be a bare `[ -d "$other/$name" ]`. That is the WRONG predicate, and it
  # was not hypothetical: `i:/superharness-wt` refuses every superharness lane with
  # ZERO registered worktrees inside it. An empty leftover directory is not a
  # competing home; it is a directory. A competing home is a path that actually
  # holds registered worktrees for this project.
  #
  # A second escape, narrower and deliberate: a root the owner has explicitly
  # RETIRED in the manifest's `superseded_roots` field is not a competitor even if
  # it still holds worktrees — that is the manager case (12 registered worktrees
  # under I:/manager-wt, 11 of them dirty/locked/leased, and none of them ours to
  # discard). Declaring it is an explicit, nameable, revertible act recorded in the
  # manifest; it is not something the wall infers.
  #
  # What the wall still guarantees, and what it never gave up: the destination MUST
  # be inside the declared root (checked above), MUST share its drive, and MUST NOT
  # already be a worktree (preflight_dest). Two lanes cannot land on the same tree
  # because of this wall.
  #
  # VOD.RIP LIVE CASE: G:/vod-rip-wt exists with four empty state dirs and ZERO
  # registered worktrees (MEASURED 2026-10-05), so the wall correctly does NOT fire
  # against this repo's I:/vod-rip-wt. The selftest's project arm asserts this, so a
  # regression to the bare `[ -d ]` predicate would be caught here, not in production.
  local name="${rd##*/}" drive other
  drive="${rd%%/*}"
  for other in g: h: i:; do
    [ "$other" = "$drive" ] && continue
    [ -d "$other/$name" ] || continue
    # escape 1: explicitly retired by the owner in worktree-roots.tsv
    if wtroot_superseded "$PROJECT" "$other/$name"; then
      continue
    fi
    # escape 2: no registered worktree there -> not a competing home
    if ! wtroot_has_registered "$other/$name"; then
      continue
    fi
    refuse "ambiguous placement: '$PROJECT' has a root on '$drive' AND on '$other' ($other/$name exists and holds registered worktrees) — resolve which one is canonical before creating"
  done
}

preflight_dest() {
  local existing
  if [ -e "$DEST" ]; then
    # A registered worktree row, or any dir that is a worktree of ANY repo, is a
    # hard stop: creating here would land a worktree on top of another one.
    if git -C "$DEST" rev-parse --git-dir >/dev/null 2>&1; then
      refuse "destination '$DEST' already exists and IS a git worktree — refusing to nest or overwrite"
    fi
    if [ -d "$DEST" ] && [ -n "$(ls -A "$DEST" 2>/dev/null)" ]; then
      refuse "destination '$DEST' already exists and is non-empty"
    fi
  fi

  # The lane must not already be registered under a DIFFERENT path for this repo.
  existing="$(wtroot_worktrees "$REPO" 2>/dev/null | while IFS= read -r w; do
    wtroot_inside "$w" "$ROOT" || continue
    [ "$(wtroot_norm "$w")" = "$(wtroot_norm "$DEST")" ] && printf '%s\n' "$w"
  done)"
  [ -z "$existing" ] || refuse "destination '$DEST' is already a registered worktree of '$PROJECT'"
}

ensure_roots() {
  local s created=0
  for s in $(wtroot_states); do
    if [ ! -d "$ROOT/$s" ]; then
      [ "$DRY_RUN" = 1 ] && { echo "dry-run: would mkdir -p $ROOT/$s"; continue; }
      mkdir -p "$ROOT/$s" || refuse "cannot create state dir $ROOT/$s"
      created=$((created + 1))
    fi
  done
  return 0
}

# ---------------------------------------------------------------- create
create() {
  local branch resolved_base out
  if [ -n "$BRANCH" ]; then
    branch="$BRANCH"
    if git -C "$REPO" show-ref --verify --quiet -- "refs/heads/$branch"; then
      refuse "branch '$branch' already exists in $REPO — pick another name or drop --branch"
    fi
  else
    branch="lane/$PROJECT/$LANE"
    if git -C "$REPO" show-ref --verify --quiet -- "refs/heads/$branch"; then
      refuse "branch '$branch' already exists (this lane is already created) — use 'git worktree list' to find it"
    fi
  fi

  # Resolve the base to a concrete commit BEFORE git sees it (see BRANCH in header).
  local ref="${BASE:-HEAD}"
  if ! resolved_base="$(git -C "$REPO" rev-parse --verify --quiet "$ref^{commit}")"; then
    if [ -z "$BASE" ] && git -C "$REPO" symbolic-ref -q HEAD >/dev/null 2>&1; then
      resolved_base="$(git -C "$REPO" rev-parse --verify --quiet 'HEAD^{commit}')"
    fi
    [ -n "${resolved_base:-}" ] \
      || refuse "base '$ref' does not resolve to a commit in $REPO (a detached HEAD cannot be used as a base name — pass --base <branch|sha>)"
  fi

  echo "wt-new: project=$PROJECT lane=$LANE state=$STATE"
  echo "wt-new: repo=$REPO"
  echo "wt-new: dest=$DEST  (resolved from the manifest — not a caller argument)"
  echo "wt-new: branch=$branch  base=$ref -> $resolved_base"

  if [ "$DRY_RUN" = 1 ]; then echo "dry-run: would run: git -C $REPO worktree add -b $branch $DEST $resolved_base"; return 0; fi

  mkdir -p "$(dirname "$DEST")" || refuse "cannot create parent of $DEST"
  if ! out="$(git -C "$REPO" worktree add -b "$branch" "$DEST" "$resolved_base" 2>&1)"; then
    printf '%s\n' "$out" >&2
    refuse "git worktree add failed"
  fi
  printf '%s\n' "$out"

  # POST-CONDITION — verified, not assumed: the dir must now be a worktree of $REPO
  # and must be registered INSIDE the declared root.
  local reg
  reg="$(wtroot_worktrees "$REPO" | while IFS= read -r w; do
    [ "$(wtroot_norm "$w")" = "$(wtroot_norm "$DEST")" ] && printf '%s\n' "$w"
  done)"
  [ -n "$reg" ] || refuse "postcondition failed: $DEST is not a registered worktree of $REPO"
  wtroot_inside "$reg" "$ROOT" || refuse "postcondition failed: registered at $reg, outside $ROOT"

  echo "wt-new: CREATED $DEST (branch $branch) — registered inside $ROOT"
}

# ---------------------------------------------------------------- selftest
# Two arms, one counter, one verdict.
#   ARM 1 (fixture)  — upstream's own arm, VERBATIM in behaviour. Builds a throwaway
#                      repo + manifest in a temp dir and touches NO real project.
#   ARM 2 (project)  — NEW 2026-10-05. Read-only assertions against THIS repo's real
#                      manifest and root. Creates nothing, moves nothing, deletes
#                      nothing: resolution primitives + `--dry-run` only. It is the
#                      arm that proves the VOD.RIP row is well-formed and that the
#                      upstream 2026-10-02 ambiguity-wall fix is still in force here.
selftest() {
  local pass=0 fail=0 rc out
  # NOT `local`: the EXIT trap runs after this function returns, where a `local` is
  # out of scope — under `set -u` the trap aborted, so cleanup never ran.
  SELFTEST_FX="$(mktemp -d "${TMPDIR:-/tmp}/wtnew-selftest.XXXXXXXX")" || { echo "selftest: no temp dir" >&2; return 2; }
  SELFTEST_FX="$(cd "$SELFTEST_FX" && pwd -W 2>/dev/null || printf '%s' "$SELFTEST_FX")"
  local fx="$SELFTEST_FX"
  trap 'rm -rf "${SELFTEST_FX:-}" 2>/dev/null || true' EXIT

  _chk() {
    if [ "$2" = "$3" ]; then printf '  PASS: %s\n' "$1"; pass=$((pass+1));
    else printf '  FAIL: %s (want [%s] got [%s])\n' "$1" "$2" "$3"; fail=$((fail+1)); fi
  }

  # ================================================================ ARM 1: fixture
  echo "ARM 1/2  fixture (upstream; no real project is touched)"

  # Fixture repo + a manifest pointing at it, so the selftest touches NO real project.
  mkdir -p "$fx/repo"
  git -C "$fx/repo" init -q 2>/dev/null
  git -C "$fx/repo" -c user.name=t -c user.email=t@t commit -q --allow-empty -m init 2>/dev/null
  printf '# fixture manifest\nfx\t%s\t%s/fx-wt\t0\tselftest\n' "$fx/repo" "$fx" > "$fx/manifest.tsv"
  export WORKTREE_ROOTS="$fx/manifest.tsv"

  # [1] the sanctioned path ACCEPTS a legal creation and puts it under the root
  out="$(bash "$SELF" --project fx --lane alpha 2>&1)"; rc=$?
  _chk "legal creation rc 0" "0" "$rc"
  [ -d "$fx/fx-wt/wip/alpha" ] && _chk "created at <root>/wip/<lane>" "yes" "yes" || _chk "created at <root>/wip/<lane>" "yes" "no"
  git -C "$fx/repo" worktree list --porcelain | grep -qi "fx-wt/wip/alpha" && _chk "registered inside root" "yes" "yes" || _chk "registered inside root" "yes" "no"

  # [2] a WRONG destination cannot even be expressed: no path argument exists
  out="$(bash "$SELF" --project fx --lane beta "$fx/wrong-place" 2>&1)"; rc=$?
  _chk "path argument refused (rc!=0)" "1" "$rc"
  printf '%s' "$out" | grep -q "NOT an argument" && _chk "refusal names the reason" "yes" "yes" || _chk "refusal names the reason" "yes" "no"

  # [3] unknown project refused
  out="$(bash "$SELF" --project nosuch --lane x 2>&1)"; rc=$?
  _chk "unknown project rc 1" "1" "$rc"

  # [4] lane with a separator refused (would escape the root by construction)
  out="$(bash "$SELF" --project fx --lane '../escape' 2>&1)"; rc=$?
  _chk "separator in lane rc 1" "1" "$rc"

  # [5] bad state refused
  out="$(bash "$SELF" --project fx --lane gamma --state sideways 2>&1)"; rc=$?
  _chk "bad state rc 1" "1" "$rc"

  # [6] re-creating the same lane refused (no silent second worktree)
  out="$(bash "$SELF" --project fx --lane alpha 2>&1)"; rc=$?
  _chk "duplicate lane rc 1" "1" "$rc"

  # [7] the resolved destination is always inside the declared root
  out="$(bash "$SELF" --project fx --lane zeta --dry-run 2>&1)"
  printf '%s' "$out" | grep -q "dest=$fx/fx-wt/wip/zeta" && _chk "dry-run dest inside root" "yes" "yes" || _chk "dry-run dest inside root" "yes" "no"

  # ================================================================ ARM 2: project
  echo
  echo "ARM 2/2  VOD.RIP manifest (read-only: creates nothing, moves nothing)"
  unset WORKTREE_ROOTS   # so the REAL repo-scoped manifest is the one under test

  local m repo root proj
  m="$(wtroot_manifest)"
  case "$(wtroot_norm "$m")" in
    *vod-rip*/scripts/worktree-roots.tsv) _chk "default manifest is the REPO-SCOPED one" "yes" "yes" ;;
    *) _chk "default manifest is the REPO-SCOPED one" "yes" "no" ;;
  esac
  [ -f "$m" ] && _chk "default manifest exists" "yes" "yes" || _chk "default manifest exists" "yes" "no"

  proj="$(awk -F'\t' '/^[[:space:]]*#/ {next} NF>=4 {print $1}' "$m" | head -n1)"
  [ -n "$proj" ] && _chk "manifest declares a project row" "yes" "yes" || _chk "manifest declares a project row" "yes" "no"

  repo="$(wtroot_field "$proj" 1)"; rc=$?
  [ "$rc" = 0 ] && _chk "wtroot_field repo_root resolves" "0" "0" || _chk "wtroot_field repo_root resolves" "0" "$rc"
  [ -d "$repo" ] && _chk "declared repo_root exists" "yes" "yes" || _chk "declared repo_root exists" "yes" "no"
  git -C "$repo" rev-parse --git-dir >/dev/null 2>&1 && _chk "declared repo_root is a git repo" "yes" "yes" || _chk "declared repo_root is a git repo" "yes" "no"

  root="$(wtroot_field "$proj" 2)"
  wtroot_validate_root "$root" && _chk "declared worktree_root is well-formed" "0" "0" || _chk "declared worktree_root is well-formed" "0" "1"
  case "$root" in *-wt) _chk "declared worktree_root uses the -wt convention" "yes" "yes" ;;
                  *) _chk "declared worktree_root uses the -wt convention" "yes" "no" ;; esac

  # The four states resolve, and every one lands inside the declared root.
  local nbad=0 s d
  for s in $(wtroot_states); do
    d="$(wtroot_resolve_destination "$proj" "$s" selftest-probe 2>/dev/null)" || { nbad=$((nbad+1)); continue; }
    wtroot_inside "$d" "$root" || nbad=$((nbad+1))
    [ "$(wtroot_norm "$d")" = "$(wtroot_norm "$root")/$s/selftest-probe" ] || nbad=$((nbad+1))
  done
  _chk "all 4 states resolve to <root>/<state>/<lane>, inside the root" "0" "$nbad"

  # THE DRIVE RULE, asserted on the real manifest: archive/ must share the root's
  # drive, because `git worktree move` into archive/ is same-drive-only.
  #
  # PORT NOTE (bug found by this arm on its first run, 2026-10-05): the drive is
  # extracted with `${p%%/*}`, NOT `cut -d/ -f1`. On this box a Windows-native
  # `cut.exe` shadows the MSYS one, it rejects `-d/` ("the delimiter must be a
  # single character"), and it prints NOTHING — so the first version of this check
  # compared "" with "" and reported PASS on an empty value. A pure-shell
  # parameter expansion has no such failure mode. The assertion is also written so
  # that "both empty" FAILS (actual is only OK when both are non-empty AND equal),
  # so this class of false green cannot recur here.
  local rd ad expect actual
  rd="$(wtroot_norm "$root")";      rd="${rd%%/*}"
  ad="$(wtroot_norm "$root/archive")"; ad="${ad%%/*}"
  expect="OK:$rd"          # the root's own drive is the expected archive/ drive
  actual="MISMATCH"
  [ -n "$rd" ] && [ -n "$ad" ] && [ "$rd" = "$ad" ] && actual="OK:$rd"
  _chk "archive/ shares the root drive" "$expect" "$actual"

  # The real thing still refuses, through the REAL manifest (not the fixture).
  out="$(bash "$SELF" --project "$proj" --lane selftest-probe --dry-run 2>&1)"; rc=$?
  _chk "real-manifest dry-run rc 0" "0" "$rc"
  printf '%s' "$out" | grep -q "dest=$root/wip/selftest-probe" && _chk "real-manifest dest is inside the root" "yes" "yes" || _chk "real-manifest dest is inside the root" "yes" "no"

  out="$(bash "$SELF" --project nosuch-project-xyz --lane x 2>&1)"; rc=$?
  _chk "real-manifest unknown project rc 1" "1" "$rc"

  out="$(bash "$SELF" --project "$proj" --lane 'a/b' 2>&1)"; rc=$?
  _chk "real-manifest separator lane rc 1" "1" "$rc"

  out="$(bash "$SELF" --project "$proj" --lane selftest-probe --state sideways 2>&1)"; rc=$?
  _chk "real-manifest bad state rc 1" "1" "$rc"

  # A real, registered lane must refuse re-creation at its REAL location.
  #
  # BUG FOUND BY RUNNING THIS, 2026-10-05 — the first version took only the lane's
  # BASENAME and re-ran the creation with the DEFAULT state (wip). Before the
  # migration every lane under the root happened to be in wip/, so the destination it
  # computed was the lane's own occupied directory, the tool correctly refused, and
  # the test passed. After the migration the first lane under the root is usually in
  # done/ or unsure/, so the same command computed an EMPTY wip/ destination — and
  # CREATED A REAL WORKTREE, on branch lane/vod-rip/wt-asrsup. A selftest arm that
  # documents itself as read-only was writing to the repository.
  #
  # Fixed by deriving the lane's ACTUAL state from its registered path, so the
  # destination the tool resolves IS the lane's own directory, which is occupied and
  # must be refused. The registry's size is now also compared before and after, so a
  # side effect of this kind fails loudly instead of passing quietly.
  # NOTE: the relative path is `${w#"$root"/}`, i.e. strip the ROOT PREFIX. The first
  # version used `${w##*/}` (strip up to the LAST slash), which yields only the lane
  # name and left <state> empty — so the guard below never matched and the check
  # silently SKIPPED instead of testing anything. A guard that quietly stops testing
  # is reported as a pass-count that still looks healthy, which is how this got
  # through a first "27 passed" run.
  local rows_before rows_after
  rows_before="$(wtroot_scan "$repo" "$root" 2>/dev/null | cut -f3)"
  local real_lane real_state
  real_lane="$(wtroot_worktrees "$repo" 2>/dev/null | while IFS= read -r w; do
    local rel st ln
    wtroot_inside "$w" "$root" || continue
    rel="${w#"$root"/}"
    st="${rel%%/*}"; ln="${rel#*/}"; ln="${ln%%/*}"
    case "$st" in wip|unsure|done|archive) ;; *) continue ;; esac
    [ -n "$ln" ] || continue
    printf '%s|%s\n' "$ln" "$st"
    break
  done | head -n1)"
  if [ -n "$real_lane" ]; then
    # ORDER MATTERS: real_state must be taken BEFORE real_lane is truncated, or the
    # `|` is already gone and the state comes out equal to the lane name (which then
    # silently computes a wip/ destination and defeats the whole point of the check).
    real_state="${real_lane#*|}"
    real_lane="${real_lane%%|*}"
    out="$(bash "$SELF" --project "$proj" --lane "$real_lane" --state "$real_state" 2>&1)"; rc=$?
    [ "$rc" != 0 ] && _chk "existing registered lane ($real_lane/$real_state) refuses re-creation" "yes" "yes" \
                   || _chk "existing registered lane ($real_lane/$real_state) refuses re-creation" "yes" "no"
    printf '%s' "$out" | grep -q "already exists and IS a git worktree" \
      && _chk "refusal names the occupied-destination reason" "yes" "yes" \
      || _chk "refusal names the occupied-destination reason" "yes" "no"
  else
    printf '  SKIP: no registered lane under the root yet (nothing to collide with)\n'
  fi
  rows_after="$(wtroot_scan "$repo" "$root" 2>/dev/null | cut -f3)"
  _chk "arm 2 changed no worktree rows (read-only)" "$rows_before" "$rows_after"

  # REGRESSION GUARD for the upstream 2026-10-02 fix: the ambiguity wall must NOT
  # fire merely because a sibling `<project>-wt` directory exists. VOD.RIP has a live
  # case — G:/vod-rip-wt (four empty state dirs, ZERO registered worktrees).
  local name drive other blocked=""
  name="$(wtroot_norm "$root")"; name="${name##*/}"
  drive="$(wtroot_norm "$root")"; drive="${drive%%/*}"   # NOT cut -d/ — see the note above
  for other in g: h: i:; do
    [ "$other" = "$drive" ] && continue
    [ -d "$other/$name" ] || continue
    wtroot_superseded "$proj" "$other/$name" && continue
    wtroot_has_registered "$other/$name" && blocked="$other/$name"
  done
  _chk "empty sibling -wt root does not trip the ambiguity wall" "" "$blocked"

  echo
  echo "wt-new selftest: $pass passed, $fail failed  (arm 1 fixture + arm 2 VOD.RIP manifest)"
  [ "$fail" -eq 0 ] || return 1
  return 0
}

[ "$MODE" = selftest ] && { selftest; exit $?; }

resolve
if [ "$ROOTS_ONLY" = 1 ]; then
  ensure_roots
  echo "wt-new: roots for '$PROJECT' at $ROOT — states: $(wtroot_states | tr '\n' ' ')"
  exit 0
fi
preflight_dest
[ "$ENSURE_ROOTS" = 1 ] && ensure_roots
create
