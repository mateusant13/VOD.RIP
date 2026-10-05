#!/usr/bin/env bash
# scripts/lib/worktree-roots.sh — PORTFOLIO worktree-placement rules (dot-source me).
#
# PROVENANCE — PORTED INTO VOD.RIP, 2026-10-05, from the house's sanctioned tool:
#   I:\!manager\scripts\lib\worktree-roots.sh   (authoritative upstream, NOT modified)
#   I:\!manager\scripts\wt-new.sh                (its caller)
#   I:\!manager\worktree-roots.tsv               (the SHARED portfolio manifest)
#
# WHY A REPO-SCOPED COPY AND NOT A DEPENDENCY ON I:\!manager
#   Upstream is owned by another repo (`I:\!manager`, whose own manifest is a
#   portfolio sidecar). A tool that resolves its manifest from OUTSIDE this repo
#   breaks the moment that path moves, and makes this project's topology
#   unauditable from the project alone. So the DEFAULT MANIFEST IS NOW REPO-LOCAL
#   (`<repo>/scripts/worktree-roots.tsv`) and the shared one is reachable only by
#   explicit override (`WORKTREE_ROOTS=...`). This is the ONLY functional change
#   made in the port; see wtnew_port_note() at the bottom for the full diff list.
#   It changes WHERE the truth lives, not WHAT is refused.
#
# !!! THE REFUSALS IN THIS FILE AND IN wt-new.sh ARE LOAD-BEARING. !!!
#   Do not "simplify" any guard away to make a lane create successfully. Every one
#   of them was added because the wrong placement was MEASURED to succeed, and
#   `git` has no concept of a correct location — this wrapper is the CHOKEPOINT,
#   so a removed guard is an unrecoverable hole, not a cleanup.
#
# WHAT THIS IS
#   The single implementation of two rules, shared by the sanctioned creation path
#   (scripts/wt-new.sh), the migration path (scripts/wt-migrate.sh) and the
#   rollback path (scripts/wt-restore.sh):
#
#     RULE A — a project's worktrees live at <worktree_root>/<state>/<lane>.
#     RULE B — a registered worktree OUTSIDE that root is a placement violation.
#
#   It is project-PARAMETERIZED: every project is a row in the manifest
#   (default <repo>/scripts/worktree-roots.tsv, override WORKTREE_ROOTS=<path>).
#   No per-project branches, no hardcoded repo lists in this file — the drive and
#   the root come from the manifest, so a new project is a new row and nothing else.
#
# WHY A CHOKEPOINT AND NOT A WARNING (the question this answers)
#   Owner, 2026-09-25: "o que impede a gente de fazer worktree no lugar errado e
#   prosseguir?" MEASURED answer: nothing. `git worktree add I:/Temp/wrong-place-wt
#   -b tmp/x` returned rc=0 and registered the worktree; no guard fired. A warning
#   is worthless AFTER the fact — the lane already runs inside the tree. Every other
#   actor (shell, PowerShell, cargo, the janitor, a TUI) calls `git` directly and git
#   has no concept of a correct location. So the fix is a missing CHOKEPOINT: the
#   wrapper below owns the destination and does not accept a path from the caller.
#
#   VOD.RIP-SPECIFIC, 2026-10-05: this repo ALSO had scripts/wt-new.ps1, which took
#   `-Root I:\TEMP` as a CALLER ARGUMENT (the exact hole this choke closes) and which
#   created a `node_modules` JUNCTION per lane. Those junctions are the reason 10 of
#   the 42 registered worktrees can never be `git worktree move`d — `git worktree
#   remove` destroyed a dependency tree through one of them. New lanes use this
#   tool, which creates no junction and refuses on an ambiguous destination.
#
# THE TRAP THIS FILE EXISTS TO AVOID (measured, do not "simplify" this back)
#   A substring test on `superharness-wt` reports 35 worktrees inside it and 140
#   outside. The TRUTH is 4 inside and 171 outside (total 175, measured 2026-09-25).
#   Cause: sibling roots that merely START with the same prefix —
#   `G:/superharness-wt-build`, `G:/superharness-wt-land`, `H:/superharness-wt-pin-a0`,
#   and a batch of `G:\superharness-wt-*\.git` gitlink rows — are NOT children of
#   `G:/superharness-wt`. wtroot_inside() therefore compares a normalized path to the
#   DECLARED root with a trailing-separator boundary, never a substring.
#
# NORMALIZATION (borrowed from I:/!manager/scripts/wt-janitor.sh, not re-invented)
#   Backslashes -> forward slashes; trailing slashes dropped; WHOLE path lowercased
#   (Windows comparison is case-insensitive and git's casing differs from the caller's);
#   a trailing `/.git` (the gitlink form) dropped. Normalize ONLY for comparing — the
#   result is never handed back to git as a registry key (that produces "is not a
#   working tree": see wt-janitor.sh's malformed-row section).
#
# STATES — VERIFIED, not assumed, 2026-10-05.
#   `wtroot_states` below returns exactly: wip unsure done archive. That is the
#   upstream list verbatim, and it matches the one structure that already works
#   (G:/superharness-wt, which wt-janitor.sh also reads). The owner's belief that
#   these four are correct is CONFIRMED by reading the function, not by convention.

# ---------------------------------------------------------------- manifest
# Absolute path of THIS lib, Windows form, resolved at source time.
# `pwd -W` is the MSYS/Git-Bash form (`I:/vod-rip-wt/...`); plain `pwd` would yield
# `/i/vod-rip-wt/...`, which fails wtroot_validate_root's `[A-Za-z]:/*` test and every
# comparison against a manifest-declared `X:/...` path. Fall back for non-MSYS bash.
wtroot_lib_dir() {
  local d
  d="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd -W 2>/dev/null)" \
    || d="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd)"
  printf '%s\n' "$d"
}

# wtroot_manifest -> the manifest in force.
#   1. WORKTREE_ROOTS, if the caller set it (upstream parity; lets the shared
#      I:/!manager/worktree-roots.tsv still be consulted deliberately).
#   2. <this repo>/scripts/worktree-roots.tsv  — the PORT DEFAULT (see header).
wtroot_manifest() {
  if [ -n "${WORKTREE_ROOTS:-}" ]; then printf '%s\n' "$WORKTREE_ROOTS"; return 0; fi
  local lib scripts
  lib="$(wtroot_lib_dir)" || return 1
  scripts="$(cd "$lib/.." 2>/dev/null && pwd -W 2>/dev/null)" \
    || scripts="$(cd "$lib/.." 2>/dev/null && pwd)"
  printf '%s/worktree-roots.tsv\n' "${scripts%/}"
}

wtroot_err() { printf 'worktree-roots: %s\n' "$*" >&2; }

# wtroot_row <project> -> "repo_root \t worktree_root \t ceiling"; rc 1 = unknown project
wtroot_row() {
  local project="${1:?project required}" m
  m="$(wtroot_manifest)"
  [ -f "$m" ] || { wtroot_err "manifest not found: $m"; return 1; }
  awk -F'\t' -v p="$project" '
    /^[[:space:]]*#/ { next }
    NF < 4          { next }
    $1 == p         { printf "%s\t%s\t%s\n", $2, $3, $4; found = 1 }
    END             { exit(found ? 0 : 1) }
  ' "$m"
}

# wtroot_projects -> one project name per line, manifest order
wtroot_projects() {
  local m; m="$(wtroot_manifest)"
  [ -f "$m" ] || { wtroot_err "manifest not found: $m"; return 1; }
  awk -F'\t' '/^[[:space:]]*#/ { next } NF >= 4 { print $1 }' "$m"
}

wtroot_field() { # wtroot_field <project> <1|2|3>
  local row; row="$(wtroot_row "$1")" || return 1
  printf '%s\n' "$row" | cut -f"$2"
}

# wtroot_superseded <project> <path> -> 0 if the owner RETIRED that root in the
# manifest's 6th field (`superseded_roots`, `;`-separated), else 1.
#
# Added 2026-10-02 with the ambiguity-wall correction in wt-new.sh. A retired root
# may still HOLD worktrees — the manager case has 12 registered worktrees under
# I:/manager-wt, 11 of them dirty/locked/leased, and the house law forbids discarding
# work this repo did not create. Retiring the root is the explicit alternative to
# deleting it: new lanes go to the canonical root, the old pile is migrated on its own
# schedule. It is a declared, nameable, revertible act, not something the wall infers.
wtroot_superseded() { # wtroot_superseded <project> <path>
  # NAO usa wtroot_row de proposito: aquele reconstrói a linha com so $2,$3,$4
  # (worktree-roots.sh) e por isso nunca entregaria o campo 6. Aqui a linha e
  # lida crua do manifesto, que e a unica fonte de verdade do 6o campo.
  local project="${1:?project required}" want="${2:?path required}" m field got
  m="$(wtroot_manifest)"
  [ -f "$m" ] || return 1
  want="$(wtroot_norm "$want")" || return 1
  field="$(awk -F'\t' -v p="$project" '
    /^[[:space:]]*#/ { next }
    NF < 4          { next }
    $1 == p         { print $6; found = 1; exit }
    END             { if (!found) exit 1 }
  ' "$m" | tr -d '\r')" || return 1
  [ -n "$field" ] || return 1
  # SEM pipeline: `printf | tr | while` roda o while em SUBSHELL, e o `exit 0`
  # de dentro dele nao propaga para a funcao — o retorno seria sempre 1. Por isso
  # o field e partido com parameter expansion e percorrido num for do shell atual.
  local rest="$field" head
  while [ -n "$rest" ]; do
    head="${rest%%;*}"
    if [ "$head" = "$rest" ]; then rest=""; else rest="${rest#*;}"; fi
    [ -n "$head" ] || continue
    if [ "$(wtroot_norm "$head")" = "$want" ]; then
      return 0
    fi
  done
  return 1
}

# wtroot_has_registered <path-prefix> -> 0 if the git worktree registry holds at least
# one worktree under that prefix, else 1.
#
# This is the predicate the ambiguity wall SHOULD have used. A bare `[ -d ]` treats an
# empty leftover directory as a competing home; measured 2026-10-02, i:/superharness-wt
# was refusing every superharness lane while holding ZERO registered worktrees.
#
# VOD.RIP note: this predicate is what lets the port keep the ambiguity wall intact
# while `G:/vod-rip-wt` (the root the SHARED portfolio manifest still declares, with
# four empty state dirs) exists. That directory holds ZERO registered worktrees, so it
# is not a competing home and does not refuse. Measured 2026-10-05.
wtroot_has_registered() { # wtroot_has_registered <path-prefix>
  local pref listing line
  pref="$(wtroot_norm "$1")" || return 1
  listing="$(git worktree list --porcelain 2>/dev/null)" || return 1
  [ -n "$listing" ] || return 1
  while IFS= read -r line; do
    case "$line" in
      worktree\ *) line="${line#worktree }" ;;
      *) continue ;;
    esac
    [ -n "$line" ] || continue
    [ "$(wtroot_norm "$line")" != "$pref" ] || return 0
    case "$(wtroot_norm "$line")" in
      "$pref"/*) return 0 ;;
    esac
  done <<EOF
$listing
EOF
  return 1
}

# VERIFIED 2026-10-05: these four, in this order, are the states. Read from the
# upstream lib (line 143) rather than inferred from a directory listing.
wtroot_states() { printf '%s\n' wip unsure done archive; }

# ---------------------------------------------------------------- paths
# <path> -> comparable form. Comparison only; NEVER a git registry key.
# FULL case-folding, not just the drive letter: MEASURED 2026-09-25, git reports
# `I:/TEMP/wtpo-xxx/repo` while the caller's `pwd -W` gives `I:/Temp/wtpo-xxx`
# (TEMP is set to `I:/Temp`). Folding only the drive left every row mismatched and
# the oracle counted 3/3 OUTSIDE on a fixture with 1/3 — a false RED on correct
# placements. This matches comparisonPath() in omp's worktree-ownership-guard.ts
# (.toLowerCase() over the whole path), i.e. the house's own Windows convention.
wtroot_norm() {
  local p="${1//\\//}"
  while [ "${p%/}" != "$p" ]; do p="${p%/}"; done
  case "$p" in */.git) p="${p%/.git}" ;; esac
  printf '%s\n' "$p" | tr 'A-Z' 'a-z'
}

# wtroot_inside <path> <declared_root> -> rc 0 when <path> IS the root or under it.
# Trailing-separator boundary, NOT a substring: `G:/superharness-wt-build` is OUTSIDE
# `G:/superharness-wt`. This one line is the difference between 4 and 35.
wtroot_inside() {
  local p r
  p="$(wtroot_norm "$1")"; r="$(wtroot_norm "$2")"
  [ -n "$p" ] && [ -n "$r" ] || return 1
  [ "$p" = "$r" ] && return 0
  case "$p" in "$r"/*) return 0 ;; esac
  return 1
}

# wtroot_convention_root_of <normalized path> -> "<drive>:/<name>-wt"; rc 1 if the
# path is not of the convention shape. Used by the wrapper's stray-root wall.
wtroot_convention_root_of() {
  local p="$1" drive rest name
  case "$p" in */*) ;; *) return 1 ;; esac
  drive="${p%%/*}"; rest="${p#*/}"; name="${rest%%/*}"
  case "$drive" in [a-z]:) ;; *) return 1 ;; esac
  case "$name" in *-wt) ;; *) return 1 ;; esac
  [ -n "$name" ] || return 1
  printf '%s/%s\n' "$drive" "$name"
}

# Same but on a RAW (un-normalized) path, for caller-facing messages.
wtroot_destination_root() { wtroot_convention_root_of "$(wtroot_norm "$1")"; }

# ---------------------------------------------------------------- the oracle primitive
# wtroot_worktrees <repo_root> -> one RAW registered worktree path per line; rc 4 = probe failed
wtroot_worktrees() {
  local repo="${1:?repo required}" out
  out="$(git -C "$repo" worktree list --porcelain 2>/dev/null)" || return 4
  printf '%s\n' "$out" | awk '/^worktree /{ sub(/^worktree /,""); print }'
}

wtroot_primary() { wtroot_worktrees "$1" | head -n1; }

# wtroot_scan <repo_root> <declared_root> -> "<strays>\t<gross>\t<total>" on stdout
#   ONE `git worktree list --porcelain` call, ONE pass. The three numbers are therefore
#   internally consistent by construction — three separate calls could each observe a
#   different registry (a lane is created or removed between them) and report a row
#   that never existed at any single instant.
#     total  = every registered row, INCLUDING the primary checkout
#     gross  = rows not under <declared_root> (includes the primary -> floor of 1)
#     strays = gross MINUS the primary = the ACTIONABLE count a ceiling compares to
#   The primary is the FIRST porcelain row: `git worktree list` emits the main working
#   tree first (MEASURED; wt-janitor.sh's own row order relies on it too).
#   rc 0 = measured · rc 4 = probe failed (caller MUST treat as UNKNOWN, never as 0 —
#   a probe that cannot speak may not report "clean").
wtroot_scan() {
  local repo="${1:?repo required}" root="${2:?root required}" out line
  local n=0 g=0 s=0 first=1
  out="$(git -C "$repo" worktree list --porcelain 2>/dev/null)" || return 4
  # A valid repo always lists at least its own working tree. Empty output is a probe
  # failure, NOT a count of zero — folding it to 0 is how a broken instrument reports
  # "clean" while measuring nothing.
  [ -n "$out" ] || return 4
  while IFS= read -r line; do
    case "$line" in
      "worktree "*)
        n=$((n + 1))
        if ! wtroot_inside "${line#worktree }" "$root"; then
          g=$((g + 1))
          [ "$first" = 1 ] || s=$((s + 1))   # the primary is never a stray
        fi
        first=0 ;;
    esac
  done <<<"$out"
  printf '%s\t%s\t%s\n' "$s" "$g" "$n"
}

# Projections of wtroot_scan — one implementation, two readers.
wtroot_count_outside() { # GROSS, primary included (superharness 2026-09-25 = 171)
  local o; o="$(wtroot_scan "$1" "$2")" || return 4
  printf '%s\n' "$o" | cut -f2
}

wtroot_count_strays() { # ACTIONABLE, primary excluded (superharness 2026-09-25 = 170)
  local o; o="$(wtroot_scan "$1" "$2")" || return 4
  printf '%s\n' "$o" | cut -f1
}

# ---------------------------------------------------------------- RULE A enforcement
# wtroot_validate_root <declared_root> -> rc 0 when the declaration is well-formed.
#   WHY THIS EXISTS (MEASURED reasoning, not decoration): wtroot_inside() compares
#   normalized strings and does NOT collapse `..` — it must not, because it also
#   compares git's own output verbatim. So a MANIFEST row declaring
#   `G:/brandops-wt/../evil-wt` would make `<root>/wip/x` compare as "inside" while
#   git would actually create `G:/evil-wt/wip/x`, i.e. OUTSIDE. That is a false GREEN
#   with only the manifest to blame, so the declaration is validated instead:
#     * absolute drive path (`X:/...`)
#     * basename ends in `-wt` (the portfolio naming convention)
#     * no `.` or `..` component anywhere (conservative: also rejects a name that
#       merely CONTAINS `..`, which is a naming cost we accept for the guarantee)
wtroot_validate_root() {
  local r="$1"
  case "$r" in
    [A-Za-z]:/*) ;;
    *) return 1 ;;
  esac
  case "$r" in *..*) return 1 ;; esac
  case "$r" in */.|*/./*) return 1 ;; esac
  case "${r##*/}" in *-wt) ;; *) return 1 ;; esac
  return 0
}

# wtroot_resolve_destination <project> <state> <lane> -> the ONLY legal destination for
# that (project,state,lane). Refuses on any shape/state/lane violation. rc != 0 = refusal.
wtroot_resolve_destination() {
  local project="${1:?project required}" state="${2:?state required}" lane="${3:?lane required}"
  local root
  root="$(wtroot_field "$project" 2)" || { wtroot_err "unknown project '$project'"; return 1; }
  wtroot_validate_root "$root" \
    || { wtroot_err "manifest declares a malformed worktree_root for '$project': '$root' (need absolute X:/..., a '-wt' basename, and no '.'/'..' component)"; return 1; }
  case "$state" in
    wip|unsure|done|archive) ;;
    *) wtroot_err "invalid state '$state' (expected one of: $(wtroot_states | tr '\n' ' '))"; return 1 ;;
  esac
  case "$lane" in
    "") wtroot_err "lane name must not be empty"; return 1 ;;
    */*|*\\*) wtroot_err "lane name must be a single path component, got '$lane'"; return 1 ;;
    .|..) wtroot_err "lane name must not be '$lane'"; return 1 ;;
  esac
  printf '%s/%s/%s\n' "${root%/}" "$state" "$lane"
}

# wtnew_port_note — the complete, auditable list of what this port changed.
# Kept here (not only in the commit) so a future reader can see the diff from
# upstream without diffing against a path that may itself move.
wtnew_port_note() {
  cat <<'EOF'
PORT DIFF vs I:\!manager\scripts\lib\worktree-roots.sh (2026-10-05)

CHANGED (2 functions, both about WHERE the manifest lives):
  wtroot_manifest   default is now <repo>/scripts/worktree-roots.tsv instead of
                    I:/!manager/worktree-roots.tsv. WORKTREE_ROOTS still overrides,
                    so the shared portfolio manifest is still reachable on request.
  wtroot_lib_dir    NEW helper: resolves this lib's own directory in Windows form
                    (`pwd -W`), so the default manifest path is absolute and
                    drive-lettered even when the tool is invoked from a worktree.

UNCHANGED BY DELIBERATE DECISION (every refusal, every oracle primitive, the
four-state list, the normalization rules, the boundary-not-substring containment
test, the malformed-root validation, and the six-field manifest shape):
  wtroot_row, wtroot_projects, wtroot_field, wtroot_superseded,
  wtroot_has_registered, wtroot_states, wtroot_norm, wtroot_inside,
  wtroot_convention_root_of, wtroot_destination_root, wtroot_worktrees,
  wtroot_primary, wtroot_scan, wtroot_count_outside, wtroot_count_strays,
  wtroot_validate_root, wtroot_resolve_destination.

COMMENTS ADDED (no behaviour change): the provenance header, the ported-warnings
about the 10 node_modules junctions, the verified state list, and the
G:/vod-rip-wt note on wtroot_has_registered.
EOF
}
