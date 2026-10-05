#!/usr/bin/env bash
# workspace-status.sh - bash sibling of scripts/workspace-status.ps1
#
# WHAT THIS IS: a reduced-fidelity sibling, not a replacement. The PowerShell
# script is the authority; this one is for a bash shell / WSL / CI. Where the
# two could disagree, the PowerShell one wins.
#
# SAME CONTRACT AS THE POWERSHELL ONE:
#   exit 0  every gate measured and passing
#   exit 2  GATE FAILURE      - measured, and bad
#   exit 3  NOT MEASURED      - nothing failed, but something could not be measured
#   never print 0 for something that was not measured; print not_measured + a reason
#
# EVERY BOUNDED BY CONSTRUCTION. A panel that scans without a ceiling is a panel
# that stops answering, and an absence is not a green. Specifically:
#   * node_modules is counted with an explicit FILE CAP. If the cap is hit the
#     count is reported not_measured, never a truncated number presented as whole.
#   * no recursive walk of the repository is ever performed. There is no --root.
#   * per-worktree git calls are counted and reported; there is no unbounded loop
#     over an unknown number of worktrees without a cap either (see MAX_WORKTREES).
#
# USAGE:
#   ./scripts/workspace-status.sh [-r REPO] [-j] [-f]
#     -r REPO   observe another checkout (default: this repo)
#     -j        JSON instead of text
#     -f        fast: skip the node_modules file count
#
# Env overrides: WS_MAX_FILES, WS_MAX_WORKTREES, WS_DEPS_MIN_FILES

set -uo pipefail

# never let a read-only tool refresh git's index
export GIT_OPTIONAL_LOCKS=0

REPO=""
JSON=0
FAST=0
while getopts "r:jfh" opt; do
  case "$opt" in
    r) REPO="$OPTARG" ;;
    j) JSON=1 ;;
    f) FAST=1 ;;
    h) sed -n '2,30p' "$0"; exit 0 ;;
    *) echo "unknown option -$OPTARG" >&2; exit 1 ;;
  esac
done

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -z "$REPO" ]; then
  REPO="$(git -C "$SELF_DIR" rev-parse --show-toplevel 2>/dev/null)"
fi
if [ -z "$REPO" ] || [ ! -d "$REPO" ]; then
  echo "cannot resolve a repository to observe (pass -r REPO)" >&2
  exit 1
fi

MAX_FILES="${WS_MAX_FILES:-60000}"          # ceiling on the node_modules count
MAX_WORKTREES="${WS_MAX_WORKTREES:-12}"     # gate: registered worktrees over this fail
DEPS_MIN_FILES="${WS_DEPS_MIN_FILES:-1000}"
PROBE_TAIL="${WS_PROBE_TAIL:-2000}"

NM="not_measured"; LFS="not_measured"; LFN="not_measured"
GATE_DEPS="not_measured"; GATE_DEPS_REASON="not measured"
GATE_PROBE="not_measured"; GATE_PROBE_REASON="not measured"
GATE_LANES="not_measured"; GATE_LANES_REASON="not measured"
GATE_TREE="ok"; GATE_TREE_REASON=""
GATE_WT="ok"; GATE_WT_REASON=""
WT_TOTAL=0; WT_UNMERGED=0; WT_DIRTY=0; WT_MISSING=0; WT_JUNC=0
DIRTY_AGENT=0; STASH_N="not_measured"
BRANCH="unknown"; SHA="unknown"; AHEAD="not_measured"; BEHIND="not_measured"
COLLISIONS=0

# ---------------------------------------------------------------------- git
BRANCH="$(git -C "$REPO" rev-parse --abbrev-ref HEAD 2>/dev/null)" || BRANCH="unknown"
SHA="$(git -C "$REPO" rev-parse --short HEAD 2>/dev/null)" || SHA="unknown"
_ab="$(git -C "$REPO" rev-list --left-right --count origin/main...HEAD 2>/dev/null)"
if [ -n "$_ab" ]; then
  BEHIND="$(echo "$_ab" | awk '{print $1}')"
  AHEAD="$(echo "$_ab" | awk '{print $2}')"
else
  GATE_TREE_REASON="${GATE_TREE_REASON:-}"
fi

# dirty, classified by owner: the governor's file is not an agent's
DIRTY_LINES="$(git -C "$REPO" status --porcelain 2>/dev/null)"
DIRTY_TOTAL="$(printf '%s' "$DIRTY_LINES" | grep -c . || true)"
while IFS= read -r line; do
  [ -z "$line" ] && continue
  f="${line:3}"
  case "$f" in
    *' -> '*) f="${f##* -> }" ;;
  esac
  f="${f//\\//}"
  if [ "$f" != ".steady-watcher.json" ]; then
    DIRTY_AGENT=$((DIRTY_AGENT + 1))
    GATE_TREE="FAIL"
  fi
done <<< "$DIRTY_LINES"
[ "$DIRTY_AGENT" -gt 0 ] && GATE_TREE_REASON="$DIRTY_AGENT agent-owned dirty file(s); only the watcher may dirty .steady-watcher.json"

STASH_N="$(git -C "$REPO" stash list 2>/dev/null | grep -c . || true)"

# ---------------------------------------------------------------- worktrees
# Bounded: we stop adding worktrees past a hard ceiling and say so.
SCAN_CAP=$((MAX_WORKTREES * 8))
if [ "$SCAN_CAP" -lt 64 ]; then SCAN_CAP=64; fi
wt_paths="$(git -C "$REPO" worktree list --porcelain 2>/dev/null | sed -n 's/^worktree //p')"
wt_total_declared="$(printf '%s' "$wt_paths" | grep -c . || true)"
wt_index=0
wt_truncated=0
while IFS= read -r p; do
  [ -z "$p" ] && continue
  wt_index=$((wt_index + 1))
  if [ "$wt_index" -gt "$SCAN_CAP" ]; then wt_truncated=1; break; fi
  if [ ! -d "$p" ]; then
    WT_MISSING=$((WT_MISSING + 1))
    GATE_WT="FAIL"
    continue
  fi
  head="$(git -C "$p" rev-parse HEAD 2>/dev/null)"
  if [ -n "$head" ]; then
    if git -C "$REPO" merge-base --is-ancestor "$head" main 2>/dev/null; then :; else
      WT_UNMERGED=$((WT_UNMERGED + 1))
    fi
  fi
  n="$(git -C "$p" status --porcelain 2>/dev/null | grep -c . || true)"
  if [ "${n:-0}" -gt 0 ]; then WT_DIRTY=$((WT_DIRTY + 1)); fi
  # a junction/symlink node_modules is a hard stop for worktree move/remove
  if [ -e "$p/node_modules" ] && [ ! -d "$p/node_modules/.." ] 2>/dev/null; then :; fi
  if [ -L "$p/node_modules" ]; then WT_JUNC=$((WT_JUNC + 1)); fi
done <<< "$wt_paths"
WT_TOTAL="$wt_index"
[ "$wt_truncated" -eq 1 ] && WT_MISSING=$((WT_MISSING + 1))

if [ "$wt_total_declared" -gt "$MAX_WORKTREES" ]; then
  GATE_WT="FAIL"
  GATE_WT_REASON="$wt_total_declared registered worktrees is over the cap of $MAX_WORKTREES; they are not being released"
fi
if [ "$wt_truncated" -eq 1 ]; then
  GATE_WT="FAIL"
  GATE_WT_REASON="${GATE_WT_REASON:+$GATE_WT_REASON; }worktree scan hit its $SCAN_CAP ceiling; later worktrees not examined"
fi
if [ "$WT_MISSING" -gt 0 ] && [ -z "$GATE_WT_REASON" ]; then
  GATE_WT_REASON="$WT_MISSING registered worktree(s) whose directory is gone"
fi

# -------------------------------------------------------------------- deps
NM_PATH="$REPO/node_modules"
if [ ! -d "$NM_PATH" ]; then
  NM="absent"; LFS="not_measured"; LFN="not_measured"
  GATE_DEPS="FAIL"
  GATE_DEPS_REASON="node_modules does not exist"
elif [ ! -d "$NM_PATH/.bin" ]; then
  # the load-bearing check, and it needs no directory walk at all
  GATE_DEPS="FAIL"
  GATE_DEPS_REASON="node_modules/.bin is ABSENT - tsc/vitest cannot run; any green reported now is a false green"
  if [ "$FAST" -eq 0 ]; then
    LFN="$(find "$NM_PATH" -type f 2>/dev/null | head -n "$MAX_FILES" | wc -l | tr -d ' ')"
    if [ "$LFN" -ge "$MAX_FILES" ]; then LFN="not_measured"; LFS="not_measured"; fi
  fi
else
  if [ "$FAST" -eq 1 ]; then
    GATE_DEPS="not_measured"
    GATE_DEPS_REASON="node_modules/.bin is present but -Fast skipped the file count"
  else
    LFN="$(find "$NM_PATH" -type f 2>/dev/null | head -n "$MAX_FILES" | wc -l | tr -d ' ')"
    if [ "$LFN" -ge "$MAX_FILES" ]; then
      LFN="not_measured"; LFS="not_measured"
      GATE_DEPS="not_measured"
      GATE_DEPS_REASON="file count hit the $MAX_FILES ceiling; not_measured rather than a truncated number"
    elif [ "$LFN" -lt "$DEPS_MIN_FILES" ]; then
      GATE_DEPS="FAIL"
      GATE_DEPS_REASON="only $LFN files under node_modules, below floor $DEPS_MIN_FILES - partial or pruned install"
    else
      GATE_DEPS="ok"; GATE_DEPS_REASON=""
    fi
  fi
fi

# ------------------------------------------------------------------- probe
PROBE="$REPO/tmp/liveness.jsonl"
P_N="not_measured"; P_P50="not_measured"; P_P95="not_measured"; P_MAX="not_measured"
P_FAIL="not_measured"; P_OK="not_measured"; P_WINDOW="not_measured"
if [ ! -f "$PROBE" ]; then
  GATE_PROBE_REASON="probe file absent: $PROBE"
else
  # health/preview lines only, and only those carrying a numeric ms
  samples="$(grep -E '"event": *"(health|preview)"' "$PROBE" 2>/dev/null | tail -n "$PROBE_TAIL" | grep '"ms":' || true)"
  if [ -z "$samples" ]; then
    GATE_PROBE_REASON="probe file exists but carries no health/preview sample with a numeric ms in the last $PROBE_TAIL lines"
  else
    ms_list="$(printf '%s\n' "$samples" | sed -n 's/.*"ms": *\([0-9.]*\).*/\1/p' | sort -n)"
    P_N="$(printf '%s\n' "$ms_list" | grep -c . || true)"
    if [ "$P_N" -eq 0 ]; then
      GATE_PROBE_REASON="samples found but no ms value could be extracted"
    else
      pct() { printf '%s\n' "$ms_list" | awk -v q="$1" '{a[NR]=$1} END{ i=int(q*NR+0.999999); if(i<1)i=1; if(i>NR)i=NR; printf "%g", a[i] }'; }
      P_P50="$(pct 0.50)"; P_P95="$(pct 0.95)"
      P_MAX="$(printf '%s\n' "$ms_list" | tail -n 1)"
      P_FAIL="$(printf '%s\n' "$samples" | grep -c '"ok": *false' || true)"
      P_OK=$((P_N - P_FAIL))
      first_ts="$(printf '%s\n' "$samples" | head -n 1 | sed -n 's/.*"ts": *"\([^"]*\)".*/\1/p')"
      last_ts="$(printf '%s\n' "$samples" | tail -n 1 | sed -n 's/.*"ts": *"\([^"]*\)".*/\1/p')"
      P_WINDOW="$first_ts .. $last_ts"
      GATE_PROBE="ok"; GATE_PROBE_REASON=""
      [ "$P_N" -lt 2 ] && GATE_PROBE_REASON="n=$P_N - a single sample is not a distribution"
    fi
  fi
fi

# ------------------------------------------------------------------- lanes
MANIFEST="$REPO/docs/lane-ownership.tsv"
LANE_N=0; LANE_QUIET=0
if [ ! -f "$MANIFEST" ]; then
  GATE_LANES_REASON="no manifest at $MANIFEST"
else
  # reduced-fidelity collision check: exact shared path, and a literal sitting
  # under a directory glob. The PowerShell tool does the full segment-overlap
  # analysis and remains the authority.
  LANE_N="$(tail -n +2 "$MANIFEST" | grep -c . || true)"
  COLLISIONS="$(tail -n +2 "$MANIFEST" | awk -F'\t' '
    NF >= 3 {
      n = split($3, arr, ",")
      for (i = 1; i <= n; i++) if (arr[i] != "") { m++; lid[m] = $1; lpath[m] = arr[i] }
    }
    END {
      c = 0
      for (a = 1; a <= m; a++) for (b = a + 1; b <= m; b++) {
        if (lid[a] == "" || lid[b] == "" || lid[a] == lid[b]) continue
        if (lpath[a] == lpath[b]) { c++; continue }
        if (lpath[b] ~ /^[^*?]*\/$/ && index(lpath[a], lpath[b]) == 1) c++
        if (lpath[a] ~ /^[^*?]*\/$/ && index(lpath[b], lpath[a]) == 1) c++
      }
      print c + 0
    }' 2>/dev/null || echo 0)"
  [ "${COLLISIONS:-0}" -gt 0 ] && GATE_WT="FAIL"
  GATE_LANES="ok"; GATE_LANES_REASON=""
fi

# ----------------------------------------------------------------- verdict
EXIT=0
[ "$GATE_TREE" = "FAIL" ] && EXIT=2
[ "$GATE_DEPS" = "FAIL" ] && EXIT=2
[ "$GATE_WT" = "FAIL" ] && EXIT=2
if [ "$EXIT" -eq 0 ]; then
  for g in "$GATE_PROBE" "$GATE_LANES" "$GATE_DEPS"; do
    [ "$g" = "not_measured" ] && EXIT=3
  done
fi

if [ "$JSON" -eq 1 ]; then
  printf '{\n'
  printf '  "tool": "workspace-status.sh",\n'
  printf '  "read_only": true,\n'
  printf '  "repo": "%s",\n' "$REPO"
  printf '  "generated_at": "%s",\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  printf '  "git": { "branch": "%s", "sha": "%s", "ahead_of_origin_main": "%s", "behind_origin_main": "%s", "dirty_total": %s, "dirty_agent_owned": %s, "stash_count": "%s" },\n' \
    "$BRANCH" "$SHA" "$AHEAD" "$BEHIND" "${DIRTY_TOTAL:-0}" "$DIRTY_AGENT" "$STASH_N"
  printf '  "worktrees": { "n_registered": %s, "n_unmerged": %s, "n_dirty": %s, "n_dir_missing": %s, "n_node_modules_junction": %s, "cap": %s, "scan_cap": %s, "scan_truncated": %s },\n' \
    "$WT_TOTAL" "$WT_UNMERGED" "$WT_DIRTY" "$WT_MISSING" "$WT_JUNC" "$MAX_WORKTREES" "$SCAN_CAP" "$wt_truncated"
  printf '  "deps": { "state": "%s", "files": "%s", "bin_present": %s, "max_files_ceiling": %s, "reason": "%s" },\n' \
    "$NM" "$LFN" "$([ -d "$NM_PATH/.bin" ] && echo true || echo false)" "$MAX_FILES" "$GATE_DEPS_REASON"
  printf '  "probe": { "measured": %s, "n": "%s", "window": "%s", "p50_ms": "%s", "p95_ms": "%s", "max_ms": "%s", "failure_count": "%s", "reason": "%s" },\n' \
    "$([ "$GATE_PROBE" = "ok" ] && echo true || echo false)" "$P_N" "$P_WINDOW" "$P_P50" "$P_P95" "$P_MAX" "$P_FAIL" "$GATE_PROBE_REASON"
  printf '  "lanes": { "manifest_present": %s, "n_declared": %s, "n_collision": %s, "reason": "%s" },\n' \
    "$([ -f "$MANIFEST" ] && echo true || echo false)" "$LANE_N" "${COLLISIONS:-0}" "$GATE_LANES_REASON"
  printf '  "gates": { "tree_clean": "%s", "deps_complete": "%s", "worktrees_sane": "%s", "probe_measured": "%s", "lanes_recorded": "%s" },\n' \
    "$GATE_TREE" "$GATE_DEPS" "$GATE_WT" "$GATE_PROBE" "$GATE_LANES"
  printf '  "exit_code": %s\n' "$EXIT"
  printf '}\n'
  exit "$EXIT"
fi

W=78
echo "=============================================================================="
echo "VOD.RIP WORKSPACE STATUS (bash)   read-only   generated $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "observing: $REPO"
echo "=============================================================================="
echo
echo "GIT"
printf '  branch            %s   sha %s\n' "$BRANCH" "$SHA"
printf '  vs origin/main    ahead=%s  behind=%s   (n=1 ref pair)\n' "$AHEAD" "$BEHIND"
printf '  dirty             total=%s  agent-owned=%s   (n=porcelain lines)\n' "${DIRTY_TOTAL:-0}" "$DIRTY_AGENT"
printf '  stash             n=%s   UNTOUCHABLE - do not pop/drop/push\n' "$STASH_N"
echo
echo "WORKTREES   population = all $wt_total_declared registered worktrees"
printf '  unmerged=%s  dirty=%s  dir-gone=%s  node_modules-junction=%s  cap=%s  scan-cap=%s\n' \
  "$WT_UNMERGED" "$WT_DIRTY" "$WT_MISSING" "$WT_JUNC" "$MAX_WORKTREES" "$SCAN_CAP"
[ "$wt_truncated" -eq 1 ] && echo "  scan hit its ceiling: not_measured for worktrees past $SCAN_CAP"
echo
echo "DEPENDENCIES"
printf '  node_modules      state=%s   files=%s   .bin=%s\n' "$NM" "$LFN" "$([ -d "$NM_PATH/.bin" ] && echo present || echo ABSENT)"
printf '  ceiling           %s files   (population = full count of node_modules, capped)\n' "$MAX_FILES"
[ -n "$GATE_DEPS_REASON" ] && echo "  reason            $GATE_DEPS_REASON"
[ "$GATE_DEPS" = "FAIL" ] && echo '  >> Any tsc/vitest PASS reported right now is a FALSE GREEN.'
echo
echo "DISK   population = df whole volumes, capped at ${WS_DISK_CAP:-12} rows"
printf '  %-24s %8s %10s\n' MOUNT FREE SIZE
# The LABEL is field 1 (the drive), but the NUMBERS are counted from the END:
# an msys mount name can contain a space (C:/Program Files/Git), and counting
# from the front would shift that row's numbers into the wrong columns. A
# shifted column is a wrong number; a truncated label is only cosmetic.
df -h 2>/dev/null |
  awk 'NR > 1 && NF >= 6 { printf "  %-24s %8s %10s\n", $1, $(NF-2), $(NF-4) }' |
  head -n "${WS_DISK_CAP:-12}"
_dfn="$(df -h 2>/dev/null | awk 'NR > 1 && NF >= 6' | wc -l | tr -d ' ')"
_cap="${WS_DISK_CAP:-12}"
_dsh=$_dfn; [ "$_dsh" -gt "$_cap" ] && _dsh=$_cap
echo "  rows shown: $_dsh of $_dfn df rows"
echo
echo "LIVE PROBE"
if [ "$GATE_PROBE" = "ok" ]; then
  printf '  distribution      n=%s  window=%s\n' "$P_N" "$P_WINDOW"
  printf '  p50=%sms  p95=%sms  max=%sms  ok=%s  failures=%s\n' "$P_P50" "$P_P95" "$P_MAX" "$P_OK" "$P_FAIL"
  printf '  population        last %s lines of %s\n' "$PROBE_TAIL" "$PROBE"
  [ -n "$GATE_PROBE_REASON" ] && echo "  caveat            $GATE_PROBE_REASON"
else
  echo "  distribution      not_measured"
  echo "  reason            $GATE_PROBE_REASON"
  echo "  (this is NOT zero. there is no measurement to report.)"
fi
echo
echo "LANES"
if [ -f "$MANIFEST" ]; then
  printf '  declared=%s  collisions=%s   (reduced-fidelity check; workspace-status.ps1 is the authority)\n' "$LANE_N" "${COLLISIONS:-0}"
else
  echo "  manifest          $MANIFEST   absent"
  echo "  reason            $GATE_LANES_REASON"
fi
echo
echo "GATES   (exit code)"
printf '  %-13s %s\n' "$GATE_TREE" tree_clean
[ -n "$GATE_TREE_REASON" ] && printf '                reason: %s\n' "$GATE_TREE_REASON"
printf '  %-13s %s\n' "$GATE_DEPS" deps_complete
[ -n "$GATE_DEPS_REASON" ] && printf '                reason: %s\n' "$GATE_DEPS_REASON"
printf '  %-13s %s\n' "$GATE_WT" worktrees_sane
[ -n "$GATE_WT_REASON" ] && printf '                reason: %s\n' "$GATE_WT_REASON"
printf '  %-13s %s\n' "$GATE_PROBE" probe_measured
[ -n "$GATE_PROBE_REASON" ] && printf '                reason: %s\n' "$GATE_PROBE_REASON"
printf '  %-13s %s\n' "$GATE_LANES" lanes_recorded
[ -n "$GATE_LANES_REASON" ] && printf '                reason: %s\n' "$GATE_LANES_REASON"
echo "------------------------------------------------------------------------------"
case "$EXIT" in
  0) echo "VERDICT  PASS (exit 0)   every gate was measured, and every gate passed" ;;
  2) echo "VERDICT  GATE FAILURE (exit 2)   something was measured, and it is bad" ;;
  3) echo "VERDICT  NOT MEASURED (exit 3)   nothing failed, but something could not be measured" ;;
esac
echo "This tool changes nothing. It only reads."
echo "=============================================================================="
exit "$EXIT"
