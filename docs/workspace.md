# The VOD.RIP workspace, in one screen

This page is for the person who owns this machine. It does not assume you know
git. Everything here is read-only observation plus, where stated, one exact
command you can copy.

**One command, and it changes nothing:**

```powershell
.\scripts\workspace-status.ps1
```

To watch the main checkout from a lane worktree, point it at the main path:

```powershell
.\scripts\workspace-status.ps1 -Repo 'C:\Users\Administrador\Desktop\Nova pasta (3)\TESTE\VOD.RIP'
```

The tool never writes, moves, deletes, merges, stashes or installs anything. It
only looks. If you want the same thing as data an agent can read, add `-Json`.

---

## What the three answers mean

The last line of the report is the verdict, and the process exit code says the
same thing:

| Exit code | Meaning | What it is telling you |
|---|---|---|
| `0` | **PASS** | Every check was measured, and every check passed. |
| `2` | **GATE FAILURE** | Something was measured, and it is bad. Look at the `FAIL` lines. |
| `3` | **NOT MEASURED** | Nothing failed, but something could not be measured. Look at the `not_measured` lines. |

`2` and `3` are deliberately different. A missing measurement is not a pass and
is not a failure either, and pretending otherwise is how four wrong numbers
survived for hours. **Never read `3` as "fine".** It means the report could not
see something you may care about.

The one thing to know about the wording: this tool never prints `0` for
something it failed to measure. It prints `not_measured` and a reason. So if you
see `not_measured`, the honest reading is "I do not know", not "it is zero".

---

## What each section is telling you

### GIT

Which branch you are on, how far ahead or behind `origin/main` you are, and
which files are modified.

The dirty list is **classified by owner**:

* `watcher` - `.steady-watcher.json` only. That file belongs to Steady Watcher,
  the governor that throttles heavy jobs. It changes constantly on its own. It
  is **not** a sign that an agent is mid-edit.
* `agent` - everything else. An `agent`-owned dirty file is uncommitted work in
  progress. That is the `tree_clean` gate.

**Stashes are untouchable.** The report lists them and refuses to act on them.
There are three pre-existing stashes in this repo that belong to you; a careless
`git stash push` has already nearly destroyed them. Nothing in this toolchain
runs `stash push`, `stash pop` or `stash drop`.

### WORKTREES

Every worktree registered with git, one line each:

* `MERGED` - `yes` or `NO`. This is **proved**, not guessed: it asks git whether
  that worktree's last commit is an ancestor of `main`. `unknown` means git could
  not answer (for example the directory has vanished).
* `DIRTY` - how many files are modified but not committed in that worktree.
* `JUNC` - whether that worktree's `node_modules` is a **junction** (a shortcut
  onto the main install, created by `scripts/wt-new.ps1` so 40 lanes do not need
  40 copies of a multi-gigabyte dependency tree).
* `STATE` - `wip`, `done`, `unsure`, `archive`, or `unmanaged`.
* `SIZE` - the worktree's own bytes.

Flags on the right (`<<UNMERGED`, `<<DIRTY`, `<<DIR-GONE`, `<<JUNCTION`) are the
things worth reading.

**Two hard rules about this section:**

1. **An unmerged worktree is never shown as `done`.** If a lane is not merged
   into `main`, it has not delivered, whatever its folder is called or whatever
   the manifest claims. The tool downgrades it to `wip` and tells you why. This
   is deliberate: a folder named `done` that is not merged is exactly the kind of
   false report that costs work.
2. **A registered worktree whose directory has disappeared is a problem, not
   something to skip.** Git still believes it exists. That mismatch is reported
   as a failure. It usually means a worktree was deleted by hand.

**The junction warning matters.** `git worktree move` and `git worktree remove`
follow a `node_modules` junction into the main install. Doing that has already
destroyed a dependency tree in this repo. The report flags every junctioned
worktree with `<<JUNCTION` so that cleanup is never a surprise.

### DEPENDENCIES

The state of `node_modules`: whether it exists, how many files are in it, and
whether `.bin` exists.

**This is the section that catches false greens.** `tsc` and `vitest` are
programs inside `node_modules\.bin`. If `.bin` is missing, no test runner can
run, and any "tests pass" or "tsc exit 0" reported at that moment was measured
against a half-installed tree. The report says so explicitly:

> `>> Any tsc/vitest PASS reported right now is a FALSE GREEN.`

When this is red, treat any recent test result as unproven until the install is
repaired by whoever owns it. Do not run `npm install` to "fix" it casually: the
tree is shared by every lane, and it has already been pruned by another agent
mid-flight.

### DISK

Free space per drive, and the total footprint of the worktrees plus `tmp/`.

Heavy data belongs on `G:`, `H:` and `I:`. `C:` is the system NVMe and should not
be filled with project artifacts. When the report shows a `C:` figure for
`tmp/`, that is `C:` being used, and it is worth knowing about.

### LIVE PROBE

The background health check that writes `tmp/liveness.jsonl`. This section
reports a **distribution**, never a single sample: how many samples, over what
time window, and the median / 95th percentile / worst, plus how many failed.

* If the probe file is absent, this reads `not_measured` and gives a reason. It
  does not read `0`.
* If only one sample exists, it says `n=1 - a single sample is not a
  distribution`.

### LANES

Who is currently editing what. Read from `docs/lane-ownership.tsv`.

* `QUIET` means a lane has gone away without tidying up: its worktree is gone,
  its branch is gone, or it was dispatched a long time ago with no commits
  since. A quiet lane is a stale claim on files.
* `!! FILE OWNERSHIP COLLISIONS` is the loud one. It means two lanes' file lists
  overlap. Two writers on one file has already destroyed work in this repo, so
  the `add` command **refuses** to create an overlapping lane in the first
  place, and this section catches any that got in another way.

**Important:** git is the authority on what has been *merged*. This file is the
authority on who is *editing what*. They are different questions and neither
substitutes for the other.

---

## Safe to clean, and explicitly untouchable

**Safe to clean** - regenerable, no unique information:

* `tmp/*.log`, `tmp/*.txt`, `tmp/*.err` - build and test output. Losing these
  loses nothing except the ability to read an old log.
* Build output (`dist/`, `build/`) - regenerated by `scripts/build-install.ps1`.

**Safe, but only after checking the report says the lane is done** - a
worktree whose `MERGED` is `yes`, `STATE` is `done`, `DIRTY` is `0`, and which
appears in no lane's `owns` list. See the rollback below.

**Explicitly UNTOUCHABLE - do not clean, do not delete, do not "tidy":**

* **The three git stashes.** They are the owner's. `git stash push`, `git stash
  pop` and `git stash drop` have each come close to destroying them.
* **Any worktree with `DIRTY` above 0.** It has uncommitted work in it.
* **Any worktree with `MERGED` = `NO`.** Its work has not landed anywhere.
* **Any worktree with `<<JUNCTION`.** Removing it can delete the shared
  `node_modules` the other lanes depend on. Remove the junction first, by hand,
  deliberately - or do not remove the worktree.
* **The other two `archive.db` files** (on `G:` and in `%APPDATA%`). They are
  orphaned predecessors. The live database is `H:\VOD.RIP-data\archive.db`.
  Never delete them here, and never quote a count from them: two wrong reports
  this week came from reading an orphan and believing it.

---

## Declaring a lane (so nobody collides)

Before an agent starts editing files, record what it owns:

```powershell
.\scripts\lane-ownership.ps1 add -Lane my-lane -Scope 'what I am building' `
  -Owns 'src/thing.ts,docs/thing.md' -Branch agent/my-lane -Worktree I:\TEMP\wt-my-lane
```

If that overlaps another live lane, it **refuses** and prints the conflict.
That refusal is the point.

When the lane is finished:

```powershell
.\scripts\lane-ownership.ps1 release -Lane my-lane
```

To ask which lanes have gone quiet:

```powershell
.\scripts\lane-ownership.ps1 quiet -StaleHours 24
```

---

## Rollback: one command per action

Every action below is reversible, and the undo is given first so you never have
to guess.

**Add or change a lane entry** -> undo: `.\scripts\lane-ownership.ps1 release -Lane <id>`
The manifest is a plain tab-separated text file committed to the repo, so
`git restore docs/lane-ownership.tsv` also reverts it.

**Merge a finished lane** (this changes `main`) -> undo:
`git reset --hard <sha-before-the-merge>`, using the sha the report printed for
`main` before you started. This is safe *only* because the lane worktree is
still present; it is your copy of the work.

**Remove a worktree** -> undo: `git worktree add <path> <branch>` followed by
re-applying its commits with `git cherry-pick`. Worktrees are not cheap to
recreate from memory, which is why the report refuses to call a dirty or
unmerged one safe.

**Anything this tool reports** needs no undo, because it changed nothing. That is
the point of it.

---

## Running the tests

```powershell
pwsh -NoProfile -File .\scripts\workspace-status.tests.ps1
```

They cover the parts that decide pass/fail: a missing probe file reporting
`not_measured` rather than `0`, an install with no `.bin` failing, an unmerged
branch never being called `done`, two lanes claiming one file being flagged, a
worktree whose directory vanished being reported, and the exit codes staying
distinct. They need no repository, no network, and no `node_modules` - which
matters, because the shared one is currently damaged.

Exit `0` = all passed. Exit `1` = at least one failed, and the failures are
listed at the end.

---

## Options worth knowing

| Option | Effect |
|---|---|
| `-Json` | Same report as JSON, for an agent to parse. |
| `-Repo <path>` | Observe a different checkout. |
| `-Fast` | Skip the byte-footprint walk. Prints `not_measured` for sizes rather than lying about them. |
| `-MaxWorktrees <n>` | The worktree count above which the gate fails. Default 12. |
| `-StaleHours <n>` | How old a silent lane may be before it counts as quiet. Default 24. |
| `-FootprintBudgetSec <n>` | Time budget for measuring worktree sizes. Default 25. |
| `-GitTimeoutMs <n>` | Time budget for one git call while probing a worktree. Default 10000. A worktree that exceeds it is reported `not_measured`, never dropped and never 0. |
| `-ThrottleLimit <n>` | How many worktrees to probe at once. Default 6, kept modest because Steady Watcher shares this machine. |
