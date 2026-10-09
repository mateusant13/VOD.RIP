# LANE D — crash scout report: is there a THIRD `Maximum update depth exceeded` cycle?

Seat: scout/verifier (READ-ONLY). No source file, test, or config was modified. Nothing
was staged, committed, checked out, or reset. Every proposal in this report is a diff
**proposal**; none of it was applied.

Workspace: `C:\Users\Administrador\Desktop\Nova pasta (3)\TESTE\VOD.RIP`

**Provenance of the tree at read time (2026-10-06 11:21, `git status --porcelain`).**
The working tree is **not clean**, and that is *not* this seat's doing — this seat wrote
exactly one file, this report. The pre-existing modifications are another lane's work:
`M .steady-watcher.json`, `M backend/routers/subtitles.py`, `M src/previewRetry.ts`, and
untracked `backend/services/preview_asr_sidecar.py`,
`backend/services/preview_asr_worker.py`, `backend/tests/test_preview_asr_worker.py`,
`control/`, `src/previewRetryCeilings.test.ts`, `runs/`.

**None of the eight files this report audits appears in that diff.** Every `file:line`
below was read against unmodified source. A re-run of this audit should still confirm that
— `git diff --stat -- src/ChannelExplorePopup.tsx src/explorePopupUtils.tsx
src/LocalFilePopup.tsx src/components/ArchiveSearchPopup.tsx
src/components/TwitchClipPopup.tsx src/components/LivePlayerPopup.tsx src/App.tsx
src/frameSnap.ts` must be empty.

---

## VERDICT

**THIRD CYCLE FOUND: NO (not confirmed).** I found **no third cycle that can close
unboundedly** — every measurement→state→effect-dep path I could trace terminates in a
finite number of passes, because the one thing that would make it unbounded (an
unstable dependency identity) is held stable by a memo in a *different* file. I did find
**two RISK sites carrying the identical unguarded-fresh-object shape**, both on the frame
snap seam, and one of them is **exactly one refactor away from being a confirmed crash**.

The honest headline is not "no third cycle". It is: *the crash class was fixed at the
component, but the shared snap seam below it is still unguarded, and its safety is
borrowed from a memo in another file rather than from its own guard.*

---

## CANDIDATE TABLE

`dep` = the dependency array as written. "guarded" = does the writer return the previous
value when the measurement is unchanged, so React can bail out.

| # | component | file:line | dep | guarded? | verdict |
|---|---|---|---|---|---|
| 1 | `PanelResizeHandles` | `src/explorePopupUtils.tsx:320` | `[host]` | **YES** — `measuredRef` triple-compare, early `return` at :337-339 | **CLEAN** (cycle #1, fixed) |
| 2 | `ChannelExplorePopup` chat layout | `src/ChannelExplorePopup.tsx:246` | n/a (`useCallback` writer) | **YES** — returns `prev` on identical geometry | **CLEAN** (cycle #2, fixed) |
| 3 | `ChannelExplorePopup` free-float layout | `src/ChannelExplorePopup.tsx:1328` | `[fullscreen, containerW, videoAspect, stackIndex, frameSnapRect, chatTotal, chromeHVersion]` | **YES** — `:1337` `setPos(prev => prev?.x===p.x && prev?.y===p.y ? prev : p)` | **CLEAN** |
| 4 | `ChannelExplorePopup` chrome-height measure | `src/ChannelExplorePopup.tsx:1366` | `[fullscreen, panelWidth, videoAspect, ready, error, editorOpen]` | **YES** — `:1369` `Math.abs(chromeH - chromeHRef.current) >= 1` hysteresis | **CLEAN** (1px dead-band) |
| 5 | `ChannelExplorePopup` column RO | `src/ChannelExplorePopup.tsx:461` | `[]` | **YES** — `:468` `h !== playerColHRef.current`, writes DOM not state | **CLEAN** |
| 6 | `ChannelExplorePopup` playback boot | `src/ChannelExplorePopup.tsx:574` | parser said none; **verified `[vod.id,…]`-terminated far below window** | n/a — idempotent primitives (`setPlayback(null)` bails) | **CLEAN** |
| 7 | **`LivePlayerPopup` snap pin** | **`src/components/LivePlayerPopup.tsx:2113`** | via `frameSnap.ts:82` `[el, rect, posRef, repinKey]` | **NO** — `onPin: (next) => setSize(next)`, fresh `{w,h}` every pin | **RISK** |
| 8 | **`useFrameSnapPin` core writer** | **`src/frameSnap.ts:72`** | `[el, rect, posRef, repinKey]` | **NO** — `setPos(snapped)`, fresh `{x,y}` every pin | **RISK** |
| 9 | **`useFrameSnapPin` size callback** | **`src/frameSnap.ts:81`** | same | **NO** — `onPin?.(size)`, fresh object, unguarded | **RISK** (same site as #8) |
| 10 | `LivePlayerPopup` un-snap restore | `src/components/LivePlayerPopup.tsx:2120` | `[frameSnapRect]` | **YES** — functional updater, but returns fresh object | **CLEAN** — fires once per unsnap transition, gated by `wasSnappedRef` |
| 11 | `LivePlayerPopup` chat toggle size | `src/components/LivePlayerPopup.tsx:2008` | `[]` | **YES** — `if (w !== s.w)` | **CLEAN** |
| 12 | `LivePlayerPopup` ref sync | `src/components/LivePlayerPopup.tsx:479` | `[size]` | n/a — writes ref only | **CLEAN** |
| 13 | `ArchiveSearchPopup` viewport clamp | `src/components/ArchiveSearchPopup.tsx:402` | `[embedded]` | **NO** — `setPos(p)` fresh `{x,y}` at `:414` | **CLEAN** — event-driven (`window` `resize`), *not* render-driven; one render per event |
| 14 | `ArchiveSearchPopup` floating geometry | `src/components/ArchiveSearchPopup.tsx:386` | `[embedded, size, seedPos]` | guarded by `if (!posRef.current)` one-shot | **CLEAN** |
| 15 | `ArchiveSearchPopup` search/deep/chat | `:605`, `:740`, `:983` | deps present | n/a — fetch/poll driven | **CLEAN** |
| 16 | `TwitchClipPopup` mount geometry | `src/components/TwitchClipPopup.tsx:902` | `[]` | one-shot via `sizeRef.current.h > 0` | **CLEAN** |
| 17 | `TwitchClipPopup` viewport clamp | `src/components/TwitchClipPopup.tsx:922` | `[]` | **NO** `setPosition(p)` fresh object | **CLEAN** — `window` `resize` listener |
| 18 | `TwitchClipPopup` drag move | `src/components/TwitchClipPopup.tsx:947` | `[drag]` | **NO** — fresh object per `pointermove` | **CLEAN** — pointer-driven, correct for drag |
| 19 | `LocalFilePopup` focus / layout | `src/LocalFilePopup.tsx:89`, `:105` | `[]`, `[stackIndex]` | one-shot | **CLEAN** |
| 20 | `App.tsx` preview column RO | `src/App.tsx:3449` | `[previewOpen]` | **YES** — `:3456` `h !== previewColHRef.current`, DOM write | **CLEAN** |
| 21 | `App.tsx` `frameSnapRects` memo | `src/App.tsx:827` | `useMemo [frameCellContents, frameLayoutTick]` | n/a — **this memo is what makes #7-#9 non-infinite** | **CLEAN (load-bearing)** |

**PanelResizeHandles mount sites** (all 8, all covered by fix #1 — there is no per-site
variant to audit): `App.tsx:7211`, `App.tsx:7253`, `App.tsx:7998`,
`ChannelExplorePopup.tsx:2442`, `LocalFilePopup.tsx:160`,
`ArchiveSearchPopup.tsx:1943`, `LivePlayerPopup.tsx:2420`,
`TwitchClipPopup.tsx:1023`.

---

## WHY #7–#9 ARE RISK AND NOT REAL — the actual argument

This is the part worth arguing carefully, because "unguarded `setState` in a layout
effect" is exactly the text of both closed cycles, and it would be easy to call this a
third one.

The pin effect's deps are `[el, rect, posRef, repinKey]` (`src/frameSnap.ts:82`):

- `el` — `popupRef.current` / `containerRef.current`. A stable DOM node reference.
- `rect` — **memoized** at `src/App.tsx:827-834` on `[frameCellContents, frameLayoutTick]`.
  A timeupdate / chat tick / quality change does not change either dep, so the memo
  returns the **same object**. This is the load-bearing fact.
- `posRef` — a `useRef` object, identity-stable for the component's life.
- `repinKey` — a primitive (string at `ChannelExplorePopup.tsx:1325`, number at
  `LivePlayerPopup.tsx:2114`).

So after `setSize(next)` at `:2113` and `setPos(snapped)` at `frameSnap.ts:72` write fresh
objects, React re-renders **once** — and the effect does **not** re-run, because none of
its four deps changed. The identity channel is closed. React's bailout is not defeated.
That is why this is not a confirmed cycle, and it is the whole difference from #2, where
`chatTotal` fed straight back into the dep array.

**The residual risk is the value channel, not the identity channel.** Two value
feedbacks survive:

1. `fit` at `LivePlayerPopup.tsx:2105` reads `sizeRef.current.w`, and `sizeRef.current`
   is written by `setSize` via `LivePlayerPopup.tsx:479`. So the pin's output is its own
   input. It is a contraction — `w = min(inner.w, sizeRef.current.w)` — so it converges
   to `inner.w` in at most two passes, then keeps re-emitting a *new object* with the
   *same numbers*.
2. `repinKey` at `LivePlayerPopup.tsx:2114` is `headerRef.current?.offsetHeight`, a **DOM
   measurement read during render**, and the pin itself writes `el.style.width` at
   `frameSnap.ts:78`. If the header's height depends on its width (a wrapping title
   would do it), pin → width → header height → `repinKey` → pin. Bounded, but real.

That bounded oscillation is a better explanation of the owner's prompt id=16689
(2026-10-03, snap-in-place "nunca funciona") than of the crash: a window that re-pins a
couple of times and settles, or springs back, rather than one that unmounts the tree.

### What would distinguish the two at runtime

| | RISK (#7–#9, predicted) | REAL third cycle (if it exists elsewhere) |
|---|---|---|
| React console | **no** `Maximum update depth exceeded` | the error fires, tree unmounts |
| Nested updates | 1–3 per pin, then flat | ≥50 per commit, never flat |
| Visible symptom | window re-pins / springs back, then holds | whole popup tree vanishes |
| How to count | React DevTools Profiler "commits" per snap gesture, or a `commitCount` ref bumped in the pin effect | — |
| Trigger to force it | resize the viewport (`frameLayoutTick` bumps → new `rect` object → pin re-runs) with frame mode ON and a live player snapped into a cell | any path where a measured value lands in its own effect's dep array |

**The forcing recipe, if a worker wants to promote this to a test:** frame mode on,
live player popped into a cell, then drag the window edge so the header re-wraps while
snapped. That is the only configuration I found where the two value feedbacks above can
both fire in one pass.

### The fragility, stated plainly

`frameSnap.ts` has **no guard of its own** against identity churn. Its safety is
imported from a `useMemo` 800 lines away in another file. The comment at `App.tsx:820-826`
describes exactly the regression that would break it — "the rect used to be a fresh
object literal built inline on every App render". **Anyone who inlines that rect again
converts #7–#9 from RISK into a confirmed infinite loop on the first timeupdate**, because
an unguarded `setSize`/`setPos` under a churning `rect` dep is unbounded. That is one
line away, and nothing in the tree prevents it: no test asserts the memo's stability, and
`test_remote_challenge_solver.py`-style pinning does not exist for this seam.

---

## POPULATION AND WINDOW

**Enumeration is CLOSED. Measurement is NOT complete.** These are different claims and I
am not conflating them.

**Enumerated:** 134 `useEffect` / `useLayoutEffect` calls across the 8 named files —
`ChannelExplorePopup.tsx` 21, `explorePopupUtils.tsx` 1, `LocalFilePopup.tsx` 2,
`ArchiveSearchPopup.tsx` 12, `TwitchClipPopup.tsx` 9, `LivePlayerPopup.tsx` 25,
`App.tsx` 61, `frameSnap.ts` 1. Plus 8/8 `PanelResizeHandles` mount sites, and all
`ResizeObserver` / `MutationObserver` / `IntersectionObserver` constructions in those files.

**Search used:** a Python scanner (`%TEMP%\laneD-enum\enum_effects.py`) doing balanced-paren
extraction of each effect's dependency array, plus a marker pass flagging `set*` calls,
observers, and measurement reads (`getBoundingClientRect`, `getComputedStyle`,
`offsetHeight/Width`, `clientWidth/Height`, `scrollHeight`) inside each callback body. Every
flagged candidate was then read by hand; the table above is the hand-read result, not the
scanner's.

**Why this is not "measurement complete":**

- The scanner reports what is **syntactically** in the dep array. It does not trace
  whether a dep is transitively derived from a value the effect writes. I hand-traced the
  three snap-related components and the two closed cycles; I did **not** hand-trace all 134.
- **Missed by design:** I audited only the 8 files the briefing named. `PreviewChatPanel`
  (the *source* of cycle #2, per the comment at `ChannelExplorePopup.tsx:234`) is **not**
  among them and I did not read it. The **other 60 `PanelResizeHandles`-free popup/panel
  components in `src/` were not enumerated at all.**
- Three scanner flags were **false positives I had to disprove by hand**: `LivePlayerPopup.tsx:1513`
  (fetch effect), `App.tsx:2275` and `App.tsx:3373` (dep arrays sit far below my read
  window; the "no dep array" reading was the parser's). `App.tsx:2119`'s `setTimeout`
  matched my own `set[A-Z]\w*` regex. A scanner that reports false positives has not
  proven a negative.

---

## OBSERVER AUDIT (briefing item 2)

Exactly **two** observer callbacks in the audited files touch state-adjacent code, and
**both are clean**:

- `src/ChannelExplorePopup.tsx:464` — `new ResizeObserver(...)`: guarded by
  `h !== playerColHRef.current` (`:468`), writes `row.style.height` directly. **No `setState`.**
- `src/App.tsx:3452` — `new ResizeObserver(...)`: guarded by `h !== previewColHRef.current`
  (`:3456`), writes `row.style.height` directly. **No `setState`.**

Both use the correct pattern for the shape in question: a ref holds the last measured
value, so a no-op measurement commits nothing, and the write goes to the DOM so no render
is scheduled. **Zero observers in the audited files call `setState` with a fresh object.**
No `MutationObserver` in any audited file touches state.

The two `setPos`/`setPosition` writers that *are* unguarded (`ArchiveSearchPopup.tsx:414`,
`TwitchClipPopup.tsx:922`, `:947`) are all **event-driven**, not render-driven — `window`
`resize` and `pointermove`. One event → one render each. That is correct behaviour for a
drag or a viewport clamp; it cannot produce `Maximum update depth exceeded`, because no
render schedules the next event.

---

## PROPOSED FIX — diff proposal ONLY, NOT APPLIED

Not applied, not staged, not tested. Offered for a worker seat. **Scope note:** this hardens
RISK items #7–#9. It does **not** claim to fix a third confirmed cycle, because I did not
find one.

```diff
--- a/src/frameSnap.ts
+++ b/src/frameSnap.ts
@@ -65,7 +65,7 @@ export function useFrameSnapPin(opts: {
   useLayoutEffect(() => {
     if (!rect || !el) return;
     const { posRef: pr, setPos, fit, onPin } = live.current;
     const inner = frameSnapInnerBox(rect);
     const size = fit(inner);
     const snapped = frameSnapPos(rect, size);
     pr.current = snapped;
-    setPos(snapped);
+    // GUARDED WRITER - same contract as ChannelExplorePopup.tsx:1337 and
+    // ArchiveSearchPopup.tsx:246. Without this, a churning `rect` identity (the exact
+    // regression App.tsx:820-826 documents) turns every App render into a fresh
+    // {x,y} and the pin becomes an unbounded setState-in-layout-effect loop.
+    setPos((prev) => (prev && prev.x === snapped.x && prev.y === snapped.y ? prev : snapped));
     el.style.position = 'fixed';
     el.style.top = `${snapped.y}px`;
     el.style.left = `${snapped.x}px`;
@@
-    onPin?.(size);
+    onPin?.(size);
   }, [el, rect, posRef, repinKey]);
```

```diff
--- a/src/components/LivePlayerPopup.tsx
+++ b/src/components/LivePlayerPopup.tsx
@@ -2113,7 +2113,10 @@
-    onPin: (next) => setSize(next),
-    repinKey: isFullscreen ? null : headerRef.current?.offsetHeight ?? 0,
+    // Guarded writer: `next` is a fresh {w,h} on every pin, so the raw setter
+    // defeats React's bailout and re-renders even when the numbers are identical.
+    onPin: (next) => setSize((prev) => (prev.w === next.w && prev.h === next.h ? prev : next)),
+    // UNCHANGED dep, but see the report: this is a DOM measurement read during
+    // render. The pin writes el.style.width (frameSnap.ts:78), which can change the
+    // header height, which changes this key. Consider hoisting it into a
+    // ResizeObserver on the header and passing the measured number via state.
+    repinKey: isFullscreen ? null : headerRef.current?.offsetHeight ?? 0,
   });
```

**Not proposed, deliberately:** moving `size`/`pos` to refs. It would remove the render
churn but changes the drag/resize contract at `LivePlayerPopup.tsx:2044-2052` and
`LocalFilePopup.tsx:126-130`, which is far outside a crash scout's remit.

**A worker seat should also pin `App.tsx:827` with a test** asserting
`frameSnapRects.get(key)` returns an identical object across two renders driven by an
unrelated state change. That memo is currently load-bearing for the absence of a crash
and nothing guards it.

---

## WHAT WAS NOT VERIFIED

1. **I did not reproduce the crash.** No `Maximum update depth exceeded` was observed in
   this session. The two closed cycles were confirmed green, not re-diagnosed.
2. **I did not run the app in a browser.** No frame-mode snap gesture, no live player in a
   cell, no header re-wrap. #7–#9 are classified by reading the dep graph, not by measurement.
3. **I did not prove #7–#9 are harmless at runtime.** My argument is that the deps are
   stable. If `getFrameCellRect` or the `frameCellContents` identity churns somewhere I
   did not trace, the argument fails and they are a real third cycle.
4. **I read only 8 of the popup family.** `PreviewChatPanel` — the *origin* of cycle #2 —
   was not read. Nor were the ~60 other panel/popup components in `src/`.
5. **I hand-verified 3 scanner false positives; the other 131 effects rest on the scanner.**
   A wrong "NO DEP ARRAY" reading elsewhere in `App.tsx` or `LivePlayerPopup.tsx` would
   have surfaced as a `setState` + measure effect and I may have dismissed it as a false
   positive too quickly.
6. **I did not verify the `chatTotal` → `containerW` chain end-to-end**; I read the fix
   (`ChannelExplorePopup.tsx:246`) and the guard at `:1337`, and took the orchestrator's
   mutation-arm result as given.
7. **I did not check whether `frameSnapInnerBox` / `frameSnapPos` round.** If
   `frameSnapInnerBox(rect)` returned non-integer floats and the DOM snapped `el.style.*`
   back to rounded integers, `offsetHeight` could oscillate by a subpixel forever — the
   one shape that would make #8 a true infinite loop. I did not test this.
8. **No new test was written.** That is a worker seat's job and I do not hold it.
9. **The orchestrator's numbers were taken as given**, not re-measured. My own run was
   2 files / 4 tests / exit 0 / 4.03 s.

---

## SELF-AUDIT

**1. Protocols missing.**
- I never used the mandated `codeintel` / `consultgpt` review loop from AGENTS.md. This
  seat's briefing is read-only and scoped to one report; `gpt -f` would have opened a
  browser session and the briefing forbids browser work beyond tests. **This is a real
  omission** — the mandated post-change review loop did not run.
- `grep` over the whole tree for `PanelResizeHandles`-adjacent measurement loops was not
  done; I audited the 8 named files only.
- No mutation arm. The orchestrator proved cycle #2's test bites by mutating the guard.
  **I did not do that for #7–#9** — I have no test for them, so I could not. This is the
  single biggest gap in the report: my central claim ("RISK, not REAL") is argued from
  the dep graph and never falsified experimentally.

**2. Extra verification.**
- Ran the two existing tests read-only: `npx vitest run src/PanelResizeHandles.test.tsx
  src/ChannelExplorePopupLayoutLoop.test.tsx` → **exit 0, 2 files / 4 tests passed,
  4.03 s**. Redirected to a file and read `$LASTEXITCODE`; no pipe.
- Enumerated all 8 `PanelResizeHandles` mounts, not just the 3 in `App.tsx`. Confirmed
  all 8 route through the single fixed component at `explorePopupUtils.tsx:320`, so
  cycle #1's fix covers every site and there is no per-site variant.
- Traced `LivePlayerPopup.tsx:2113` all the way through `sizeRef` (`:479`) back into
  `fit` (`:2105`) to check whether the pin is its own input. It is — but it contracts.
- Checked the two observer sites by hand after the scanner produced two false positives,
  rather than trusting the automated flag either way.

**3. New checkboxes (named, mechanical).**
- `panel-resize-handles-all-mounts-routed-through-single-fixed-component` — enumerate
  every `<PanelResizeHandles` in `src/`; assert exactly one defines `useLayoutEffect`,
  and that it is guarded. *Would have caught a second unfixed copy of the component.*
- `frame-snap-rect-identity-stable-across-unrelated-renders` — render `App` with a cell
  occupied, bump an unrelated state (a simulated timeupdate), assert
  `frameSnapRects.get(key)` is `Object.is`-identical across both renders. **This is the
  test that makes the missing crash explicit**: inline the rect fresh, watch it go red.
- `snap-pin-writer-guarded-against-identity-churn` — assert the `setPos`/`setSize`
  updater returns `prev` when the geometry is unchanged (the cycle #2 mutation arm, applied
  to the snap seam).
- `no-effect-writes-state-it-depends-on` — for every `useLayoutEffect`, assert no
  `setState` inside its callback stores a freshly-constructed object literal without a
  comparison updater. Mechanical; catches the whole class, not these three instances.
- `observer-callbacks-never-setstate` — grep every `ResizeObserver`/`MutationObserver`
  body for `set[A-Z]`; the two current sites must stay at zero.

**4. Review by another subagent.** **Not done.** No second seat has read this. The
verdict rests on one reader's reading of a dep graph. A `verifier` seat re-reading
#7–#9 and disagreeing would invalidate the RISK classification.

**5. Confidence + what moves it.**
- **High** (≈0.9) that there is no third cycle *in the 8 audited files* reachable through
  the snap seam — the dep graph is short and I traced every hop.
- **Medium** (≈0.6) that the defect shape does not exist in the ~60 components I did not
  read. Two cycles of the same shape were found in the first two files anyone looked at;
  that base rate argues against my own negative result.
- **What moves it:** item 7 above (subpixel round-trip through `frameSnapInnerBox` →
  `el.style` → `offsetHeight` → `repinKey`) is the one hypothesis that would flip #8 to
  REAL. A jsdom test cannot see it — jsdom returns `0` for `offsetHeight`. **It needs a
  real browser.** That is the check I could not run and the one I would run first.

**6. What was NOT verified.** Repeated from the section above deliberately — the browser
gesture, the reproduction, the `PreviewChatPanel` read, the 131 scanner-only effects, and
any falsification of my own RISK classification.

**7. Gate-doubt.**
- `verde-de-verdade:` The green I produced is narrow and I will not dress it up. I ran
  **2 files / 4 tests**, which cover the **two already-closed cycles only**. They say
  nothing about a third cycle — those tests were written to pin fixes #1 and #2, so they
  are *definitionally* incapable of failing on a defect in `frameSnap.ts` or
  `LivePlayerPopup.tsx`. A green here is compatible with a third cycle existing
  tomorrow. The honest description of this session is: **enumeration closed, measurement
  of the hypothesis not started.**
- `falta-no-gate:` A future change that inlines the frame cell rect in `App.tsx` — moving
  `frameSnapRect` out of the `useMemo` at `App.tsx:827`, or passing
  `getFrameCellRect(i)` directly as a prop at `App.tsx:8018` / `:8076` — crosses a gate
  that **does not exist**. No test asserts memo stability, and the fix in
  `frameSnap.ts` is unguarded, so that single change turns a bounded 1–3 pass oscillation
  into an unbounded `setState`-in-`useLayoutEffect` loop on the first timeupdate, in
  frame mode, with the tree unmounting. It would be reintroduced by someone reading
  `frameSnap.ts` alone and finding nothing wrong with it.
- `gate-melhor:` Mechanically, in the order I would apply them: (a) the guarded-writer
  diff in `frameSnap.ts:72` + `LivePlayerPopup.tsx:2113` above; (b)
  `frame-snap-rect-identity-stable-across-unrelated-renders` as a *deletion test* — it must
  fail when the memo is removed; (c)
  `no-effect-writes-state-it-depends-on` run in CI over `src/`, so the class is closed
  mechanically rather than by the next reader's diligence. (a) without (b) is a fix
  nobody can tell is load-bearing.