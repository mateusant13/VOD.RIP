#!/usr/bin/env bash
# scripts/wt-migrate.sh — move an EXISTING registered worktree into a state directory.
#
# VOD.RIP-ONLY (2026-10-05). Not part of the upstream port.
#
# WHAT IT IS FOR
#   This repo had 42 registered worktrees in a FLAT, UNSTRUCTED set under I:/TEMP
#   (wt-ar, wt-bind, wt-obsv, ...). The sanctioned topology is
#   I:/vod-rip-wt/{wip,unsure,done,archive}/<lane>. This tool performs that reorganisation
#   using `git worktree move` — a MOVE, never a delete — and records every move in
#   docs/wt-migration-ledger.tsv so it can be undone.
#
# !!! THIS TOOL NEVER DELETES ANYTHING. !!!
#   No `git worktree remove`. No `git branch -D`. No `rm -rf`. The only mutating
#   command it can run is `git worktree move`, and every refusal below is a NO-OP.
#
# THE JUNCTION GUARD IS THE POINT OF THIS TOOL
#   10 of the 42 worktrees carry a `node_modules` JUNCTION pointing at the shared
#   main tree on C:. `git worktree remove` has already destroyed a dependency tree
#   through one of these. So a junctioned tree is not moved either — wtv_refuse_junction
#   stops it BEFORE any git command runs, and the lane is reported instead. Junction
#   detection happens first, every time; it is never a post-hoc check.
#
# USAGE
#   wt-migrate.sh --project <id> --lane <lane> --state <state> [--dry-run] [--deep]
#   wt-migrate.sh --project <id> --list-outside          # what is still a stray
#   wt-migrate.sh --project <id> --audit-junctions       # report junctioned lanes
#
#   --lane is the worktree's DIRECTORY NAME as it stands today (e.g. `wt-ar`), not a
#   branch name. It keeps its name after the move, so the ledger maps 1:1.
#   --deep widens the junction scan to unbounded (slower; use when unsure).
set -u
set -o pipefail

SELF="${BASH_SOURCE[0]}"
SCRIPT_DIR="$(cd "$(dirname "$SELF")" && pwd)"
# shellcheck source=lib/worktree-roots.sh
. "$SCRIPT_DIR/lib/worktree-roots.sh"
# shellcheck source=lib/wt-vodrip.sh
. "$SCRIPT_DIR/lib/wt-vodrip.sh"

PROJECT="vod-rip"
LANE=""
STATE=""
DRY_RUN=0
DEEP=0
LIST_OUTSIDE=0
AUDIT_JUNCTIONS=0

usage() { awk 'NR==1{next} /^#/||/^[[:space:]]*$/{sub(/^# ?/,"");print;next} {exit}' "$SELF"; }
refuse() { printf 'wt-migrate: REFUSED: %s\n' "$*" >&2; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    --project) PROJECT="${2:-}"; shift 2 ;;
    --lane)    LANE="${2:-}"; shift 2 ;;
    --state)   STATE="${2:-}"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    --deep)    DEEP=1; WTV_JUNCTION_DEPTH=-1; shift ;;
    --list-outside) LIST_OUTSIDE=1; shift ;;
    --audit-junctions) AUDIT_JUNCTIONS=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) refuse "unknown argument '$1' (the destination is NOT a caller argument — it is resolved from the manifest)" ;;
  esac
done

[ "$DEEP" = 1 ] && WTV_JUNCTION_DEPTH=-1
export WTV_JUNCTION_DEPTH

# ---------------------------------------------------------------- resolve
row="$(wtroot_row "$PROJECT")" || refuse "unknown project '$PROJECT' (not in manifest $(wtroot_manifest))"
REPO="$(printf '%s' "$row" | cut -f1)"
ROOT="$(printf '%s' "$row" | cut -f2)"
[ -d "$REPO" ] || refuse "project '$PROJECT' repo root does not exist: $REPO"
git -C "$REPO" rev-parse --git-dir >/dev/null 2>&1 \
  || refuse "project '$PROJECT' repo root is not a git repository: $REPO"
wtroot_validate_root "$ROOT" \
  || refuse "manifest declares a malformed worktree_root for '$PROJECT': '$ROOT'"

# ---------------------------------------------------------------- reports
if [ "$LIST_OUTSIDE" = 1 ]; then
  printf 'registered worktrees OUTSIDE %s (strays, primary excluded):\n' "$ROOT"
  wtroot_worktrees "$REPO" | while IFS= read -r w; do
    wtroot_inside "$w" "$ROOT" && continue
    [ "$(wtroot_norm "$w")" = "$(wtroot_norm "$(wtroot_primary "$REPO")")" ] && continue
    printf '  %s\n' "$w"
  done
  exit 0
fi

if [ "$AUDIT_JUNCTIONS" = 1 ]; then
  printf 'junction / reparse audit of every registered worktree of %s:\n' "$PROJECT"
  n=0
  wtroot_worktrees "$REPO" | while IFS= read -r w; do
    if wtv_junctions "$w" >/dev/null 2>&1; then
      printf '  JUNCTION  %s\n' "$w"
    fi
  done
  exit 0
fi

# ---------------------------------------------------------------- the move
[ -n "$LANE" ]  || refuse "missing --lane"
[ -n "$STATE" ] || refuse "missing --state (one of: $(wtroot_states | tr '\n' ' '))"

# The destination is resolved, never accepted.
DEST="$(wtroot_resolve_destination "$PROJECT" "$STATE" "$LANE")" || exit 1
wtroot_inside "$DEST" "$ROOT" \
  || refuse "destination '$DEST' is outside the declared worktree root '$ROOT'"

# Find the CURRENT location of this lane among the repo's registered worktrees.
#
# PERFORMANCE (measured 2026-10-05): the first version compared paths with
# `wtroot_norm`, which forks `tr A-Z a-z`. Across 43 registered worktrees that is
# ~170 process spawns per invocation, and on a governor-throttled box each spawn
# costs enough that the tool appeared to HANG (a dry-run ran >280s without a line
# of output). The comparison below is pure shell (`${var,,}`, case folding) and
# walks the same registry with ZERO forks per row. Do not "simplify" this back
# into wtroot_norm calls in a loop.
PRIMARY="$(wtroot_primary "$REPO")"
PRIMARY_LC="${PRIMARY,,}"
LANE_LC="${LANE,,}"
SRC=""
while IFS= read -r w; do
  [ -n "$w" ] || continue
  b="${w##*/}"; b="${b,,}"
  [ "$b" = "$LANE_LC" ] || continue
  wl="${w,,}"
  [ "$wl" = "$PRIMARY_LC" ] && continue   # never move the main checkout
  SRC="$w"
  break
done <<EOF
$(wtroot_worktrees "$REPO")
EOF
[ -n "$SRC" ] || refuse "no registered worktree named '$LANE' for project '$PROJECT' (it may already be migrated, or the name may be a branch name rather than a directory name)"

# Already where it belongs?
if wtroot_inside "$SRC" "$ROOT"; then
  refuse "'$SRC' is already inside the declared root '$ROOT' — nothing to migrate"
fi
if [ "$(wtroot_norm "$SRC")" = "$(wtroot_norm "$DEST")" ]; then
  refuse "'$SRC' is already at the destination"
fi
[ -e "$DEST" ] && refuse "destination '$DEST' already exists — refusing to land on it"

# *** JUNCTION GUARD — BEFORE ANY git COMMAND. ***
wtv_refuse_junction "$SRC" "migrate" || exit 3

SHA="$(git -C "$SRC" rev-parse HEAD 2>/dev/null)"
BR="$(git -C "$SRC" rev-parse --abbrev-ref HEAD 2>/dev/null)"

printf 'wt-migrate: project=%s lane=%s\n' "$PROJECT" "$LANE"
printf 'wt-migrate:   from   %s\n' "$SRC"
printf 'wt-migrate:   to     %s   (resolved from the manifest)\n' "$DEST"
printf 'wt-migrate:   branch %s  head %s\n' "$BR" "$SHA"
printf 'wt-migrate:   junction scan (depth %s): CLEAN\n' "$WTV_JUNCTION_DEPTH"

if [ "$DRY_RUN" = 1 ]; then
  printf 'wt-migrate: dry-run: would run: git -C %s worktree move %s %s\n' "$REPO" "$SRC" "$DEST"
  exit 0
fi

wtv_move "$REPO" "$SRC" "$DEST" || refuse "move did not complete — tree left where it was"

# Ledger row: the record that makes this reversible.
wtv_ledger_append "$LANE" "$SRC" "$STATE" "$DEST" "$SHA" "$(wtv_utc)" \
  || refuse "the move SUCCEEDED but the ledger row could not be written — record this manually: lane=$LANE original=$SRC"

printf 'wt-migrate: MOVED  %s -> %s\n' "$SRC" "$DEST"
printf 'wt-migrate: ROLLBACK: scripts/wt-restore.sh --lane %s   (puts it back at %s)\n' "$LANE" "$SRC"
