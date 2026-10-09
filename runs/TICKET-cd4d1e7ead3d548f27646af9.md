# TICKET-cd4d1e7ead3d548f27646af9 — WS_VISIBLE flash vs. two `visible=false` reports

repo: `C:\Users\Administrador\Desktop\Nova pasta (3)\TESTE\VOD.RIP` (HEAD `05cd4c8`)
date: 2026-10-06 · no commit made (parent owns commits)

## Verdict

**None of R1–R4.** The contradiction is real but it is not a bug in repo code and
not a race in the visibility lines. Both reports are true **of the same HWND at
the same instant**, because they measure different properties:

* `WS_VISIBLE` is genuinely **set for 7.524 ms** during startup.
* For every one of those milliseconds, the window's **layered alpha is 0**
  (`WS_EX_LAYERED` + `GetLayeredWindowAttributes` → 0), so DWM composites
  **nothing**. The style bit toggles; **zero pixels reach the owner's desktop**.
* The shell's `IsWindowVisible` read is **not stale** — it is taken after
  `Hide()`, and at that moment the window really is not visible.

So the flash the ticket reports is a **style-bit flash with no painted frame**.
The cause is not in this repo.

## Where it lives (precise)

`C:\Program Files\Python311\Lib\site-packages\webview\platforms\winforms.py:777-781`
(pywebview 6.2.1), the `hidden=True` branch:

```python
if window.hidden:
    browser.Opacity = 0
    browser.Show()      # <-- WS_VISIBLE goes true here
    browser.Hide()      # <-- and false again, two statements later
    browser.Opacity = 1 # <-- never reached while shown: Hide() comes first
```

`Show()`/`Hide()` are consecutive statements on one thread, so alpha is still 0
for the whole visible window. The ordering (`Opacity = 1` **after** `Hide()`) is
what makes the flash unpainted. This is deliberate, and it works.

The repo's only contribution is the flag itself —
`backend/__main_launcher__.py:580-581`, `kwargs["hidden"] = True`, and it is only
set when `--autostart` is in `sys.argv` (`__main_launcher__.py:782, 890`).
**No fix applied: there is no defect in repo code.** Patching a third-party
site-packages file to remove a flash that paints nothing would be a change with
no observable effect.

## The entry point named by the ticket does not exist

`app/webview/run.cmd` is **absent** from the repo, from git history, and from
every worktree:

```
Test-Path "…\TESTE\VOD.RIP\app\webview\run.cmd"                    -> False
git -C <repo> ls-files | Select-String '^app/'                     -> 0 files
git -C <repo> ls-files | Select-String 'run\.cmd'                   -> no match
git -C <repo> log --all --oneline -- 'app/webview/run.cmd'         -> no commits
```

There is no `app/` directory and no tracked `.cmd` in the repo at all; the only
`.cmd` files on disk are under `node_modules/.bin/`. I reproduced the launcher's
**own** code path instead (`_launch_pywebview(start_hidden=True)`), which is what
the ticket's entry point would have run.

## The two visibility lines — found, and they are not reports

Both, and neither one prints anything:

1. `backend/services/app_lifecycle.py:246` — `IsWindowVisible` inside
   `_find_vodrip_hwnd_windows()`; this is the one that targets the panel.
2. `backend/utils.py:557` — `IsWindowVisible` inside
   `_explorer_hwnd_by_title()`; this one targets **Explorer** windows
   (`CabinetWClass`/`ExploreWClass`), not the panel.

Both are **guards that skip a window**, not reports that emit `visible=false`.
The ticket's premise — a shell that *reports* `visible=false` — has no code
referent in this repo: `grep` for `visible=`, `visible:`, `WS_VISIBLE` across
`backend/`, `scripts/`, and the tracked tree returns **no match**. They also do
not disagree with each other, because they do not look at the same window.

## Measurement

Hypothesis under test: is `WS_VISIBLE=true` a **painted** frame?
Out-of-process poller at ~2 kHz (a same-process poller cannot sample a
two-statement window — it fights the GUI thread for the GIL), reading
`GWL_STYLE & WS_VISIBLE`, `IsWindowVisible`, `GetLayeredWindowAttributes`,
and `DwmGetWindowAttribute(DWMWA_CLOAKED)`.

```powershell
py -3.11 tmp\ticket-cd4d1e7ead3d548f27646af9_pass4.py hidden   # EXIT=0
py -3.11 tmp\ticket-cd4d1e7ead3d548f27646af9_pass4.py shown    # EXIT=0 (control)
```

Window of observation: **t = 820 ms → 921 ms after `create_window()`**, measured
from inside the launched process with `time.perf_counter()`, one clock for both
traces.

| run | HWND | WS_VISIBLE span | samples inside | layered alpha | dwm_cloaked |
|---|---|---|---|---|---|
| `hidden=True` (the launcher's autostart path) | 28975850 | **7.524 ms** (911.04→918.56) | 5 | **max 0, min 0** | 0 |
| `shown` (control, `hidden=False`) | 72165002 | 2553.46 ms | 2244 | not layered (α=256) | 0 |

The control is what makes the hidden row readable: the identical probe held a
genuinely visible window for 2.55 s across 2244 samples. A probe that could not
see a real window would not have produced that row, so `max_layered_alpha = 0`
over the flash is a measurement, not a blind spot.

**Cross-check with the repo's own line** (pass 2, same clock): an in-process
thread calling `_find_vodrip_hwnd_windows()` returned **17178920** — the *same*
HWND the poller saw flip — at t=972 ms, then 0 again at t=974 ms. So the repo's
visibility line does see the panel when it samples inside the window; it reports
false only because by then the window is hidden again. **Not R1 (one HWND, not
two processes), not R2 (the read is current, not stale), not R3 (the bit goes
true ~91 ms *after* the window exists, not before), not R4 (same handle).**

## Evidence artifacts

- `tmp/ticket-cd4d1e7ead3d548f27646af9_flash_probe.py` — pass 1 (HWND + style bit)
- `tmp/ticket-cd4d1e7ead3d548f27646af9_pass2.py` + `tmp/pass2_hidden_merged.json` — repo's own line vs. poller
- `tmp/ticket-cd4d1e7ead3d548f27646af9_pass3.py` + `tmp/pass3_hidden.json` — alpha/cloaked on transitions
- `tmp/ticket-cd4d1e7ead3d548f27646af9_pass4.py` + `tmp/pass4_hidden.json`, `tmp/pass4_shown.json` — per-poll alpha + control

All left in place (untracked, in `tmp/`); nothing deleted.

## What was NOT verified

- I did **not** run the whole app or `VOD-RIP.EXE`, so I did not observe a flash
  on the owner's real desktop with their real window geometry and z-order. I ran
  the launcher's exact kwargs in isolation. The alpha=0 result is a property of
  the branch, not of my probe.
- I did **not** verify what the owner actually saw. If they saw a frame, my
  measurement says DWM composited nothing in this configuration; a second
  configuration (their saved `window_geometry`, `maximized=True`, a different
  backend) is unmeasured.
- I did not test the `mshtml`/`None` fallback backends; `edgechromium` was used,
  which is the one the launcher tries first (`__main_launcher__.py:611`).
- No multi-run population: **one** hidden run and **one** control. The 7.524 ms
  figure is a single observation, not a distribution.

review: skipped (orchestrator discretion, single-mechanism forensics lane)

## SELF-AUDIT

1. **Protocols missing:** no task card was opened (`state/cards/`); the ticket id
   is the card substitute named by the parent. No `closure-admission.sh` run —
   this lane produces a receipt, not a repo change.
2. **Extra verification:** pass 4 exists only to try to **refute** pass 3. It did
   — pass 3 sampled alpha on transitions only and so could not see inside the
   window; pass 4 samples every poll and adds a control. Pass 3's conclusion
   survived, but it had not earned it.
3. **New checkboxes:** none added.
4. **Review by another subagent:** none — single-lane forensics.
5. **Confidence:** high that no repo-code defect exists (mechanism is two
   statements in site-packages, confirmed by reading them and by alpha
   measurement). Medium on "the owner saw nothing" — that is a claim about human
   perception I did not measure; I measured that DWM composited nothing.
6. **What was NOT verified:** the four items above.
7. **Gate-doubt:**
   - `verde-de-verdade:` the control run (2244 samples / 2553 ms visible) is what
     makes the 5-sample alpha=0 reading load-bearing rather than a sampling miss.
   - `falta-no-gate:` a future change to `winforms.py`'s `hidden` branch (e.g.
     moving `Opacity = 1` before `Hide()`, or an upstream pywebview bump) would
     make this flash **painted** with no repo diff at all. A version pin or a
     startup-time `IsWindowVisible` assertion would cross that boundary.
   - `gate-melhor:` a one-line startup probe in `_launch_pywebview` that records
     `WS_VISIBLE` + layered alpha in the first 2 s would turn this whole lane
     from forensics into a standing check — mechanical, and it fails loudly if
     the flash ever becomes real.

Does your implementation meet the spec? YES - R1-R4 are each ruled out by a named
measurement on a single HWND (same handle, current-not-stale read, bit set after
creation, alpha 0 across all 5 samples with a control that reaches 255), the
missing `app/webview/run.cmd` is reported rather than invented around, the flash
window is timed at 7.524 ms in a stated observation window, and no code change
was made because the cause is in site-packages rather than in code I own.