#!/usr/bin/env bash
# scripts/lib/wt-vodrip.sh — VOD.RIP-ONLY additions (dot-source me).
#
# NOT PART OF THE PORT. Everything in scripts/lib/worktree-roots.sh is the house's
# sanctioned, upstream-provenanced code. This file is new, written 2026-10-05 for
# this repo, and holds exactly the two things the upstream tool does not have:
#
#   1. A JUNCTION GUARD. `git worktree remove` has already destroyed a dependency
#      tree in this project because a lane's `node_modules` was a JUNCTION to the
#      shared main tree. So a worktree containing one is not deleted AND NOT MOVED —
#      it is refused and reported. This guard exists on BOTH directions
#      (wt-migrate.sh and wt-restore.sh): a junctioned tree is never relocated.
#   2. A ROLLBACK LEDGER. wt-migrate.sh appends one row per move (lane, original
#      path, state, new path, sha, timestamp). wt-restore.sh reads it to put a lane
#      back exactly where it came from. The ledger is what makes a structural
#      decision reversible by someone who does not know git internals.
#
# Both tools are MOVE-ONLY. There is no code path in this file, or in wt-migrate.sh
# or wt-restore.sh, that removes a worktree, a branch, or a file.

# ---------------------------------------------------------------- junctions

# WTV_JUNCTION_DEPTH — how deep to look for reparse points. Default 3.
#   1 = <worktree>/node_modules
#   2 = <worktree>/packages/*/node_modules
#   3 = one level deeper, covering the monorepo shape
# The ONLY case measured in this repo is depth 1, and an independent PowerShell
# `-Recurse -Depth 2` audit (2026-10-05) found exactly the same 10 lanes, so the
# default is not load-bearing on a guess. Set WTV_JUNCTION_DEPTH=-1 to scan
# unbounded (`wt-migrate.sh --deep`).
: "${WTV_JUNCTION_DEPTH:=3}"

# wtv_junctions <dir> -> prints one reparse/symlink path per line; rc 0 if any found.
#
# TWO INDEPENDENT DETECTORS, because a guard that can silently see nothing is worse
# than no guard at all. BOTH were broken in the first version of this file and both
# failures are recorded here, because a "CLEAN" verdict from a broken detector is
# exactly how a junctioned tree reaches `git worktree move`:
#
#   BROKEN 1 (2026-10-05): `find "$dir" -type l` with git's path form
#     `I:/TEMP/wt-obsv`. MSYS `find` does not accept a forward-slash drive letter;
#     it either walks the wrong thing or hangs. FIXED by converting with
#     `cygpath -u` first. `find` also never followed a junction until then, so the
#     guard reported CLEAN on `wt-obsv`, which HAS one. `git worktree move` then
#     refused on its own ("Permission denied") and the tree was left in place — the
#     backstop held, but the guard had not.
#
#   BROKEN 2 (2026-10-05): `cmd //c dir /AL /S "<path>"`. Under the
#     PowerShell -> bash -> MSYS argv -> cmd chain, the inner quotes make `dir`
#     treat the path as an invalid OPTION ("Opção inválida"), and the unquoted
#     argv form mangles `/AL`/`/S` instead. It reported 0 junctions on a tree that
#     has one. DROPPED rather than patched, because a quoting trick that survives
#     only on one invocation style is not a guard.
#
# The two detectors below are the ones MEASURED to work on this box:
#   * detector 1 — `find -type l` over the `cygpath -u` form. Fast (milliseconds),
#     depth-bounded. On `I:/TEMP/wt-obsv` it prints `I:/TEMP/wt-obsv\node_modules`.
#   * detector 2 — Windows PowerShell `Get-ChildItem -Attributes ReparsePoint`,
#     RECURSIVE and UNBOUNDED, with the path passed through an ENVIRONMENT VARIABLE
#     so no quoting is involved. Slower (~0.6-4s), so it runs only to CONFIRM a
#     negative from detector 1 — which is precisely when a false CLEAN would hurt.
#     A positive from either detector refuses; nothing else does.
wtv_junctions() {
  local dir="${1:?dir required}" depth="$WTV_JUNCTION_DEPTH" posix out rc=1 u
  [ -d "$dir" ] || return 1

  # Detector 1 — POSIX layer. cygpath -u is MANDATORY here (see BROKEN 1).
  u="$(cygpath -u "$dir" 2>/dev/null)" || u=""
  if [ -n "$u" ] && [ -d "$u" ]; then
    if [ "$depth" -lt 0 ]; then
      posix="$(find "$u" -type l 2>/dev/null | head -n 50)"
    else
      posix="$(find "$u" -maxdepth "$depth" -type l 2>/dev/null | head -n 50)"
    fi
    if [ -n "$posix" ]; then
      printf '%s\n' "$posix"
      return 0
    fi
  fi

  # Detector 2 — Windows layer, recursive and unbounded, quoting-free.
  out="$(WTV_SCAN_DIR="$dir" powershell.exe -NoProfile -Command '
        $d = $env:WTV_SCAN_DIR
        if (-not $d) { exit 0 }
        Get-ChildItem -LiteralPath $d -Force -Recurse -Attributes ReparsePoint -ErrorAction SilentlyContinue |
          ForEach-Object { $_.FullName }
      ' 2>/dev/null | tr -d '\r')"
  if [ -n "$out" ]; then
    printf '%s\n' "$out" | head -n 50
    return 0
  fi
  return 1
}

# wtv_refuse_junction <dir> <verb> — hard stop.
#
# SIGN CONTRACT (the first version of this function was INVERTED and silently let
# every junctioned lane through, so it is stated here): `wtv_junctions` returns 0
# when it FOUND a junction and non-zero when the tree is clean. Therefore the
# refusal fires on rc 0. `wtv_refuse_junction` returns non-zero when it refused,
# so callers use it as `wtv_refuse_junction "$d" migrate || exit 3`.
wtv_refuse_junction() {
  local dir="$1" verb="${2:-move}" links rc
  links="$(wtv_junctions "$dir")"; rc=$?
  if [ "$rc" -eq 0 ]; then
    printf 'wt-safety: REFUSED to %s "%s": it contains a JUNCTION / reparse point.\n' "$verb" "$dir" >&2
    printf '%s\n' "$links" | sed 's/^/    /' >&2
    printf 'wt-safety: `git worktree remove` destroyed a dependency tree in this project through exactly such a junction,\n' >&2
    printf 'wt-safety: so a junctioned tree is not moved either. Leave it where it is and report it. NO-OP.\n' >&2
    return 1
  fi
  return 0
}

# ---------------------------------------------------------------- rollback ledger

# wtv_repo_root — the CHECKOUT the tool was invoked from (the current worktree's
# toplevel), NOT the primary tree. This matters: the ledger has to land in the branch
# that is doing the migration, because that is where docs/worktree-topology.md and
# this file live and they are committed together. Resolving to the primary tree would
# write an untracked file into `main`'s working tree on every migration, which is a
# side effect on a checkout the caller did not name and may be forbidden from
# touching (this project: never commit to main, and main is kept clean).
wtv_repo_root() {
  git rev-parse --show-toplevel 2>/dev/null
}

# wtv_ledger_path -> docs/wt-migration-ledger.tsv in the primary tree.
# It is a COMMITTED file on purpose: the rollback record must survive the rollback.
wtv_ledger_path() {
  local r; r="$(wtv_repo_root)" || return 1
  printf '%s/docs/wt-migration-ledger.tsv\n' "${r%/}"
}

wtv_ledger_header() {
  printf '# wt-migration-ledger.tsv — one row per `git worktree move`, the record that makes the\n'
  printf '# worktree-topology migration reversible. Read docs/worktree-topology.md for the procedure.\n'
  printf '#\n'
  printf '# NOTHING IN THIS FILE IS A DELETION RECORD. Every row describes a MOVE. To undo one row,\n'
  printf '# run:  scripts/wt-restore.sh --lane <lane>      (moves it back to <original_path>)\n'
  printf '# To undo the whole migration:  scripts/wt-restore.sh --all\n'
  printf '#\n'
  printf '# lane<TAB>original_path<TAB>state<TAB>new_path<TAB>sha<TAB>moved_utc\n'
}

wtv_ledger_init() {
  local l; l="$(wtv_ledger_path)" || return 1
  [ -f "$l" ] && return 0
  mkdir -p "$(dirname "$l")" || return 1
  wtv_ledger_header > "$l" || return 1
}

# wtv_ledger_find <lane> -> the row for that lane, or rc 1
wtv_ledger_find() {
  local lane="${1:?lane required}" l; l="$(wtv_ledger_path)" || return 1
  [ -f "$l" ] || return 1
  awk -F'\t' -v p="$lane" '/^#/ {next} NF>=6 && $1==p {print; found=1} END {exit(found?0:1)}' "$l" | tr -d '\r'
}

# wtv_ledger_append <lane> <original> <state> <newpath> <sha> <utc>
wtv_ledger_append() {
  local l; l="$(wtv_ledger_path)" || return 1
  wtv_ledger_init
  printf '%s\t%s\t%s\t%s\t%s\t%s\n' "$1" "$2" "$3" "$4" "$5" "$6" >> "$l" || return 1
}

wtv_utc() { date -u '+%Y-%m-%dT%H:%M:%SZ'; }

# ---------------------------------------------------------------- the move itself

# wtv_drive <path> -> lowercase "x:" drive prefix, or "" when there is none (UNC).
# Pure parameter expansion + tr: no `cut -d` (the `cut` on PATH is a third-party
# coreutils build whose -d rejects "/", MEASURED 2026-10-05) and no GNU-sed \L.
wtv_drive() {
  local p="${1//\\//}"
  case "$p" in
    [A-Za-z]:/*) printf '%s\n' "$(printf '%s' "${p%%:*}" | tr 'A-Z' 'a-z')" ;;
    *) printf '\n' ;;
  esac
}

# wtv_move <repo> <src> <dest> — ONE same-drive `git worktree move` with a verified
# post-condition. Refuses (never falls back to copy-then-delete, which is how data
# gets lost) on: cross-drive, existing destination, or a failed registry update.
wtv_move() {
  local repo="$1" src="$2" dest="$3" out sd dd
  sd="$(wtv_drive "$src")"; dd="$(wtv_drive "$dest")"
  if [ -n "$sd" ] && [ -n "$dd" ] && [ "$sd" != "$dd" ]; then
    printf 'wt-move: REFUSED: cross-drive move %s: -> %s: (`git worktree move` is same-drive-only).\n' "$sd" "$dd" >&2
    return 1
  fi
  if [ -e "$dest" ]; then
    printf 'wt-move: REFUSED: destination already exists: %s\n' "$dest" >&2
    return 1
  fi
  mkdir -p "$(dirname "$dest")" || { printf 'wt-move: cannot create parent of %s\n' "$dest" >&2; return 1; }
  if ! out="$(git -C "$repo" worktree move "$src" "$dest" 2>&1)"; then
    printf '%s\n' "$out" >&2
    printf 'wt-move: REFUSED: git worktree move failed; the tree was left where it was.\n' >&2
    return 1
  fi
  printf '%s\n' "$out"
  # POST-CONDITION — the registry must now point at the destination, and the old
  # path must be gone from it. Verified, not assumed.
  if ! git -C "$repo" worktree list --porcelain | grep -qF "worktree $dest"; then
    printf 'wt-move: REFUSED: postcondition failed — %s is not registered after the move.\n' "$dest" >&2
    return 1
  fi
  if git -C "$repo" worktree list --porcelain | grep -qF "worktree $src"; then
    printf 'wt-move: REFUSED: postcondition failed — %s is still registered after the move.\n' "$src" >&2
    return 1
  fi
  return 0
}
