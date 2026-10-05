#!/usr/bin/env bash
# scripts/wt-restore.sh — THE ROLLBACK. Move a worktree back out of a state directory.
#
# VOD.RIP-ONLY (2026-10-05). Not part of the upstream port.
#
# WHAT THIS IS
#   The worktree-topology migration MOVED 32 registered worktrees from a flat
#   I:/TEMP pile into I:/vod-rip-wt/{wip,unsure,done}/. Every move was recorded in
#   docs/wt-migration-ledger.tsv by wt-migrate.sh. This script reads that ledger and
#   runs the SAME `git worktree move` in reverse, putting each lane back exactly
#   where it came from.
#
#   Written for an owner who does not know git internals. Three commands cover
#   everything:
#       scripts/wt-restore.sh --list              # see what is reversible
#       scripts/wt-restore.sh --lane wt-ar        # put ONE lane back
#       scripts/wt-restore.sh --all               # put ALL of them back
#
# !!! THIS TOOL NEVER DELETES ANYTHING. !!!
#   No `git worktree remove`. No `git branch -D`. No `rm -rf`. The only mutating
#   command it can run is `git worktree move` (in reverse). The ledger is never
#   rewritten or truncated by a restore — it is the historical record, and a
#   restore that is run twice is a no-op, not a second move.
#
# IDEMPOTENCE (why --all is safe to press twice)
#   A ledger row is restored only if its lane is currently INSIDE the root. Once
#   moved back, it is outside, so the row is reported "already restored" and skipped.
#   Restoring therefore converges rather than oscillating.
#
# USAGE
#   wt-restore.sh --list
#   wt-restore.sh --lane <lane> [--to <path>] [--dry-run]
#   wt-restore.sh --all [--dry-run]
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
TO=""
DRY_RUN=0
ALL=0
LIST=0

usage() { awk 'NR==1{next} /^#/||/^[[:space:]]*$/{sub(/^# ?/,"");print;next} {exit}' "$SELF"; }
refuse() { printf 'wt-restore: REFUSED: %s\n' "$*" >&2; exit 1; }
note()  { printf 'wt-restore: %s\n' "$*"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --project) PROJECT="${2:-}"; shift 2 ;;
    --lane)    LANE="${2:-}"; shift 2 ;;
    --to)      TO="${2:-}"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    --all)     ALL=1; shift ;;
    --list)    LIST=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) refuse "unknown argument '$1' (an explicit --to destination is the only path you may supply)" ;;
  esac
done

row="$(wtroot_row "$PROJECT")" || refuse "unknown project '$PROJECT' (not in manifest $(wtroot_manifest))"
REPO="$(printf '%s' "$row" | cut -f1)"
ROOT="$(printf '%s' "$row" | cut -f2)"
[ -d "$REPO" ] || refuse "project '$PROJECT' repo root does not exist: $REPO"

LEDGER="$(wtv_ledger_path)" || refuse "cannot resolve the ledger path"

if [ "$LIST" = 1 ]; then
  if [ ! -f "$LEDGER" ]; then note "no ledger at $LEDGER — nothing has been migrated."; exit 0; fi
  printf 'rollback ledger: %s\n' "$LEDGER"
  printf '%-24s %-10s %-46s %s\n' LANE STATE CURRENT-PATH RESTORABLE-TO
  awk -F'\t' '/^#/ {next} NF>=6 {
      printf "%-24s %-10s %-46s %s\n", $1, $3, $4, $2
    }' "$LEDGER"
  note "reversible rows: $(awk -F'\t' '/^#/ {next} NF>=6' "$LEDGER" | wc -l | tr -d ' ')"
  exit 0
fi

[ "$ALL" = 1 ] || [ -n "$LANE" ] || refuse "nothing to do — pass --lane <lane>, --all, or --list"
[ -f "$LEDGER" ] || refuse "no ledger at $LEDGER — nothing was migrated, so there is nothing to restore"

# ---------------------------------------------------------------- one restore
# restore_one <lane> <original_path> <state> <new_path> <mode>
#   mode = auto  -> use <original_path>
#   mode = to    -> use the --to destination given by the caller
restore_one() {
  local lane="$1" original="$2" state="$3" current="$4" mode="$5" dest
  local reg="" w b wl
  local PRIMARY_LC; PRIMARY_LC="$(wtroot_primary "$REPO")"; PRIMARY_LC="${PRIMARY_LC,,}"
  local LANE_LC="${lane,,}"

  # Locate the lane as currently registered.
  # Pure-shell comparison, no wtroot_norm in the loop: see the PERFORMANCE note in
  # wt-migrate.sh — a `tr` fork per worktree per row made this appear to hang.
  while IFS= read -r w; do
    [ -n "$w" ] || continue
    b="${w##*/}"; b="${b,,}"
    [ "$b" = "$LANE_LC" ] || continue
    wl="${w,,}"
    [ "$wl" = "$PRIMARY_LC" ] && continue
    reg="$w"; break
  done <<EOF
$(wtroot_worktrees "$REPO")
EOF

  if [ -z "$reg" ]; then
    note "SKIP  $lane: not a registered worktree of $PROJECT any more (nothing to move)"
    return 0
  fi
  if [ "$mode" = to ]; then
    dest="$TO"
  else
    dest="$original"
  fi

  # Already out of the root -> the rollback is done. Idempotent, not an error.
  if ! wtroot_inside "$reg" "$ROOT"; then
    note "OK    $lane: already outside $ROOT (at $reg) — nothing to restore"
    return 0
  fi

  if [ -z "$dest" ]; then
    refuse "$lane: no destination. The ledger records original_path='$original'; pass --to <path> if that is wrong."
  fi
  if [ "$(wtroot_norm "$reg")" = "$(wtroot_norm "$dest")" ]; then
    note "OK    $lane: already at $reg — nothing to restore"
    return 0
  fi
  if [ -e "$dest" ]; then
    refuse "$lane: restore destination already exists: $dest — move it aside first; refusing to land on it"
  fi

  # *** JUNCTION GUARD — BEFORE ANY git COMMAND. ***
  # The migration refused these, so a restore should never meet one; if a junction
  # appeared since, refuse rather than risk the tree that destroyed a dependency.
  wtv_refuse_junction "$reg" "restore" || return 3

  note "lane=$lane state=$state"
  note "  from $reg"
  note "  to   $dest   ($( [ "$mode" = to ] && printf 'explicit --to' || printf 'ledger original_path' ))"
  if [ "$DRY_RUN" = 1 ]; then
    note "  dry-run: would run: git -C $REPO worktree move $reg $dest"
    return 0
  fi
  wtv_move "$REPO" "$reg" "$dest" \
    || refuse "$lane: restore move failed; the tree was left where it was"
  note "  RESTORED $lane -> $dest"
  return 0
}

# ---------------------------------------------------------------- whole-migration undo
if [ "$ALL" = 1 ]; then
  total=0; done_n=0; skipped=0
  # REVERSE order: undo the most recent move first, so an interrupted undo leaves the
  # topology closest to its pre-migration shape.
  while IFS=$'\t' read -r lane original state newpath sha utc; do
    case "$lane" in \#*|"") continue ;; esac
    total=$((total + 1))
    if restore_one "$lane" "$original" "$state" "$newpath" auto; then
      done_n=$((done_n + 1))
    else
      skipped=$((skipped + 1))
    fi
  done < <(awk -F'\t' '/^#/ {next} NF>=6' "$LEDGER" | tac)
  note "-----"
  note "ledger rows: $total   restored/skipped-ok: $done_n   failed: $skipped"
  [ "$skipped" -eq 0 ] || refuse "$skipped lane(s) could not be restored. The failing lanes were reported above and were LEFT WHERE THEY ARE. Re-run after fixing the cause — this is safe to re-run."
  note "The topology is back to its pre-migration shape. NO worktree, branch or file was deleted at any point."
  exit 0
fi

# ---------------------------------------------------------------- one lane
lrow="$(wtv_ledger_find "$LANE")" \
  || refuse "no ledger row for lane '$LANE'. Use 'scripts/wt-restore.sh --list' to see the reversible lanes, or pass --to <path> for a lane with no ledger row."

lane="$(printf '%s' "$lrow" | cut -f1)"
original="$(printf '%s' "$lrow" | cut -f2)"
state="$(printf '%s' "$lrow" | cut -f3)"
newpath="$(printf '%s' "$lrow" | cut -f4)"

if [ -n "$TO" ]; then
  restore_one "$lane" "$original" "$state" "$newpath" to
else
  restore_one "$lane" "$original" "$state" "$newpath" auto
fi
note "The ledger still records this move — it is history, not a to-do. Re-running this command is a no-op."
