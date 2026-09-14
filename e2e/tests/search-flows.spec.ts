/**
 * E2E baseline for the archive search popup (D11): the SEARCH CHAT dialog,
 * the chip re-filter contract (a `video` kind selection must never surface
 * LIVE/STREAM rows), the raw `title`-kind badge (opt-in), and the deep
 * transcript sweep lifecycle against the REAL backend: start → progress →
 * incremental live results → pause/resume → mouse-click Cancel, plus the
 * pause/resume API seam on its own.
 *
 * Run: npx playwright test --config=e2e/playwright.config.ts tests/search-flows.spec.ts
 *
 * These tests hit the owner's live backend and live archive DB on purpose: the
 * baseline is about what the shipped UI shows against real data, so no search
 * endpoint is mocked. The only state-changing calls are POSTs to
 * /api/archive/search/deep*, and every sweep started here is cancelled (and
 * confirmed stopped server-side) before the test ends.
 */

import { test, expect, type Locator, type Page } from '@playwright/test';

// The config sets no `workers`, so a default fan-out would let sibling workers
// collide on the backend's _DEEP_RUNNING_CAP=2 and hammer the (currently
// bot-gated) YouTube path. Keep this spec serial.
test.describe.configure({ mode: 'serial' });

// The playwright config sets UI_URL to the webServer's port; fall back to the
// conventional dev ports so a lone-file run still works.
const UI_URL = process.env.UI_URL || 'http://localhost:5173';
const API_PORT = process.env.PORT || '7897';
const API_URL = `http://localhost:${API_PORT}`;

// The deep sweep target shared by the lifecycle tests: `jb_sniper` is a saved
// channel (twitch/kick slug `jb_sniper`, YouTube handle JBSniperPRIME) whose
// sweep enumerates 66 videos in ~4 s, and `solo` already has cached matches —
// so progress is observably dynamic even while the YouTube bot gate freezes
// the remote pass.
const DEEP_CHANNEL = 'jb_sniper';
const DEEP_QUERY = 'solo';
/** The channel <select> label the popup derives for that saved channel
 *  (`channelUtils.deriveChannelDisplayName`: twitch→kick→youtube, ' / '-joined). */
const DEEP_CHANNEL_LABEL = 'jb_sniper / JBSniperPRIME';

/** The live job snapshot the panel polls and the pause badge reads. */
interface DeepJobSnapshot {
  status: string;
  paused: boolean;
}

/** Runtime narrowing of an untrusted job payload (no unchecked cast): anything
 *  off-contract reads as `null`, so a malformed body can never masquerade as a
 *  settled sweep in the poll predicates below. */
function asDeepJobSnapshot(body: unknown): DeepJobSnapshot | null {
  if (typeof body !== 'object' || body === null) return null;
  if (!('status' in body) || typeof body.status !== 'string') return null;
  const paused = 'paused' in body && body.paused === true;
  return { status: body.status, paused };
}

/** Same for the `{job_id}` a deep POST answers with; a malformed/error body
 *  yields '', which the caller's truthiness assertion reports as a failure. */
function asJobId(body: unknown): string {
  if (typeof body !== 'object' || body === null) return '';
  if (!('job_id' in body) || typeof body.job_id !== 'string') return '';
  return body.job_id;
}

/** POST a JSON body and return the raw Response (status is the assertion). */
async function post(url: string, body: unknown): Promise<Response> {
  return fetch(url, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(body),
    signal: AbortSignal.timeout(30_000),
  });
}

/** Probe result: the parsed snapshot, `gone` (404 — unknown/evicted, so
 *  certainly not sweeping), or `error` (transient failure or off-contract
 *  body; the owner's API occasionally stalls a request past its abort
 *  window). `expect.poll` predicates must not throw, so every outcome is a
 *  value and a blip retries instead of killing the assertion. */
type DeepProbe = DeepJobSnapshot | 'gone' | 'error';

async function deepJobSnapshot(jobId: string): Promise<DeepProbe> {
  try {
    const resp = await fetch(`${API_URL}/api/archive/search/deep/${jobId}`, {
      signal: AbortSignal.timeout(20_000),
    });
    if (resp.status === 404) return 'gone';
    if (!resp.ok) return 'error';
    return asDeepJobSnapshot(await resp.json()) ?? 'error';
  } catch {
    return 'error';
  }
}

/** Collapses a probe into the lifecycle token the cancel tests poll for. */
async function deepJobState(jobId: string): Promise<'running' | 'settled' | 'pending'> {
  const probe = await deepJobSnapshot(jobId);
  if (probe === 'gone') return 'settled';
  if (probe === 'error') return 'pending';
  return probe.status === 'running' ? 'running' : 'settled';
}

/** Collect console errors during a test. */
async function collectConsoleErrors(page: Page): Promise<string[]> {
  const errors: string[] = [];
  page.on('pageerror', (err) => errors.push(`Uncaught: ${err.message}`));
  page.on('console', (msg) => {
    if (msg.type() === 'error') {
      errors.push(`Console[${msg.type()}]: ${msg.text()}`);
    }
  });
  return errors;
}

// The owner's backend serves ui_language=pt-BR; the app's async language switch
// mid-test translates labels and breaks the English selectors. Snapshot
// settings once and serve them pinned to English (app.spec.ts idiom).
let settingsEn: string | null = null;
test.beforeAll(async () => {
  for (let attempt = 0; attempt < 3 && settingsEn === null; attempt++) {
    try {
      const resp = await fetch(`${API_URL}/api/settings`, { signal: AbortSignal.timeout(30_000) });
      const body: Record<string, unknown> = await resp.json();
      body.ui_language = 'en';
      settingsEn = JSON.stringify(body);
    } catch {
      await new Promise((resolve) => setTimeout(resolve, 2000));
    }
  }
});

/** Register the pinned-settings route; call before `page.goto`. */
async function pinEnglishUi(page: Page): Promise<void> {
  if (!settingsEn) return;
  const body = settingsEn;
  await page.route('**/api/settings', (route) => {
    if (route.request().method() !== 'GET') return route.continue();
    return route.fulfill({ status: 200, headers: { 'content-type': 'application/json' }, body });
  });
}

/** Row wrapper: the popup's only `items-stretch` element (the hits list). */
const ROW_SELECTOR = 'div.flex.items-stretch';

/**
 * Count mounted rows carrying a badge whose text is exactly `label`. Only the
 * innermost spans are considered: ancestor spans concatenate the row text, and
 * a video title may itself contain "[🔴LIVE]" or the word "title".
 */
async function countRowsWithBadge(rows: Locator, label: RegExp): Promise<number> {
  return rows.evaluateAll(
    (els, pattern) =>
      els.filter((el) =>
        Array.from(el.querySelectorAll('span')).some(
          (s) => new RegExp(pattern).test((s.textContent ?? '').trim()),
        ),
      ).length,
    label.source,
  );
}

/** Open the archive search popup through the real SEARCH CHAT toggle. */
async function openArchiveSearch(page: Page) {
  // The first-run wizard (z-9999) renders in a fresh context and swallows
  // every click; seed the same flags the other specs seed.
  await page.addInitScript(() => {
    localStorage.setItem('vodrip.onboardingDone', '1');
    localStorage.setItem('vodrip.firstTime.cookieInstall', '1');
  });
  await pinEnglishUi(page);
  await page.goto(UI_URL);
  await expect(page.locator('.vod-app-shell')).toBeVisible({ timeout: 60_000 });
  await page.getByRole('button', { name: /SEARCH CHAT|PESQUISAR CHAT/i }).first().click();

  const dialog = page.getByRole('dialog', { name: /Archive search|Pesquisa no arquivo/i });
  await expect(dialog).toBeVisible({ timeout: 10_000 });
  const searchInput = dialog.getByPlaceholder(/SEARCH TRANSCRIPTS \+ CHAT|PESQUISE/i);
  await expect(searchInput).toBeVisible();
  return { dialog, searchInput, rows: dialog.locator(ROW_SELECTOR) };
}

/**
 * Type a query and await the local-archive search it fires. The live box
 * answers these in ~4 s, but the same query has been observed near 18 s under
 * load — hence the generous timeout.
 */
async function awaitSearch(page: Page, searchInput: Locator, query: string): Promise<void> {
  await Promise.all([
    page.waitForResponse(
      (r) =>
        r.url().includes('/api/archive/search?') &&
        r.request().method() === 'GET' &&
        !r.url().includes('/remote'),
      { timeout: 90_000 },
    ),
    searchInput.fill(query),
  ]);
}

test.describe('Archive search popup', () => {
  test('opens through SEARCH CHAT with an empty, error-free result area', async ({ page }) => {
    const errors = await collectConsoleErrors(page);
    const { dialog, searchInput, rows } = await openArchiveSearch(page);

    // No query yet → the search effect short-circuits below 2 chars, so there
    // are no rows and no error block, and the input took autofocus.
    await expect(rows).toHaveCount(0);
    await expect(dialog.getByRole('button', { name: /^Retry$/i })).toHaveCount(0);
    await expect(searchInput).toBeFocused();

    // A 1-char query is a keystroke mid-word, not a search: it must not fire a
    // request (the effect bails before the debounce elapses).
    const fired: string[] = [];
    page.on('request', (req) => {
      if (req.url().includes('/api/archive/search?')) fired.push(req.url());
    });
    await searchInput.fill('c');
    await page.waitForTimeout(1_500);
    expect(fired).toHaveLength(0);

    expect(errors.filter((e) => !e.includes('favicon'))).toHaveLength(0);
  });

  test('kind chip re-filter drops every LIVE/STREAM row', async ({ page }) => {
    const { dialog, searchInput, rows } = await openArchiveSearch(page);

    await awaitSearch(page, searchInput, 'campeonato');
    // Live data: `campeonato` = 2217 hits whose first 200 carry 59 LIVE and 67
    // STREAM badges. Only HITS_RENDER_CHUNK (200) rows mount, so 200 is the
    // ceiling for any DOM count here.
    await expect.poll(() => rows.count(), { timeout: 90_000 }).toBeGreaterThan(0);
    expect(await countRowsWithBadge(rows, /^(LIVE|STREAM)$/)).toBeGreaterThan(0);

    // `live` is deliberately NOT a chip (the filter set is vod/clip/short/
    // video), so narrowing to `video` must drop live/stream rows server-side.
    const videoChip = dialog.getByRole('button', { name: 'VIDEO', exact: true });
    await expect(videoChip).toHaveAttribute('aria-pressed', 'false');
    await videoChip.click();
    await page.waitForResponse(
      (r) => r.url().includes('/api/archive/search?') && r.url().includes('kind=video'),
      { timeout: 90_000 },
    );
    await expect(videoChip).toHaveAttribute('aria-pressed', 'true');

    // 246 `video`-kind hits for the same query — the list must not go empty,
    // and no row may keep a live/stream badge.
    await expect.poll(() => rows.count(), { timeout: 90_000 }).toBeGreaterThan(0);
    expect(await countRowsWithBadge(rows, /^(LIVE|STREAM)$/)).toBe(0);
  });

  test('renders the raw `title` kind badge once the TITLE source is opted in', async ({ page }) => {
    // Two live searches on the owner's 7 GB archive.
    test.setTimeout(180_000);
    const { dialog, searchInput, rows } = await openArchiveSearch(page);

    // Titles are the noisiest pass, so `video`/'title' is an explicit opt-in
    // (ARCHIVE_SOURCE_DEFAULTS = transcript+chat, archiveSearchUtils.ts:83) and
    // goes on the wire as `source=transcript,chat`. Without it a `papiflix`
    // search returns only the 2 chat hits, so there is no title badge to assert.
    const titleChip = dialog.getByRole('button', { name: 'title', exact: true });
    await expect(titleChip).toHaveAttribute('aria-pressed', 'false');
    await searchInput.fill('papiflix');
    await expect.poll(() => rows.count(), { timeout: 90_000, intervals: [1_000] }).toBeGreaterThan(0);
    expect(await countRowsWithBadge(rows, /^title$/)).toBe(0);

    // Opting titles in re-runs the search and takes `papiflix` from 2 hits to 8
    // — 6 of kind `title`, which carry neither a transcript nor a chat badge, so
    // the badge falls through to the raw kind string.
    await titleChip.click();
    await expect(titleChip).toHaveAttribute('aria-pressed', 'true');
    await expect
      .poll(() => countRowsWithBadge(rows, /^title$/), { timeout: 90_000, intervals: [1_000] })
      .toBeGreaterThan(0);
  });

  test('deep transcript sweep starts, reports progress, and cancels', async ({ page }) => {
    // Enumeration runs three yt-dlp tab listings, then pass 1 scans cached
    // transcripts with zero network. The sweep is cancelled here, never awaited
    // to `done`: the YouTube IP bot gate is active
    // (/api/session/cookies/status → youtube_gate_active), and pass 2 parks in
    // _wait_gate() until it lifts (~25 min at the time of writing). Asserting
    // `done` would make this spec gate-dependent, so assert the honest,
    // gate-independent contract: start → dynamic progress → cancel.
    test.setTimeout(240_000);
    const { dialog, searchInput } = await openArchiveSearch(page);

    // `jb_sniper` is a saved channel (twitch/kick slug `jb_sniper`, YouTube
    // handle JBSniperPRIME) whose sweep enumerates 66 videos in ~4 s and
    // reaches `scanned 28` from cache — so progress is observably dynamic even
    // while the gate freezes pass 2.
    const channelSelect = dialog.locator('select#archive-filter-channel');
    await expect(channelSelect).toBeVisible();
    await channelSelect.selectOption({ label: 'jb_sniper / JBSniperPRIME' });

    await searchInput.fill('solo');
    await expect(dialog.getByText('Deep transcript search', { exact: true })).toBeVisible({
      timeout: 90_000,
    });
    await expect(
      dialog.getByText('Search transcripts of every video (uploads, shorts, streams)'),
    ).toBeVisible();

    const confirmInput = dialog.getByLabel('Type confirmar to enable', { exact: true });
    const startButton = dialog.getByRole('button', { name: /Start deep search/i });
    // The destructive-ish sweep stays locked behind the literal confirm word.
    await expect(startButton).toBeDisabled();
    await confirmInput.fill('confirmar');
    await expect(startButton).toBeEnabled();

    const started = page.waitForResponse(
      (r) => new URL(r.url()).pathname === '/api/archive/search/deep' && r.request().method() === 'POST',
      { timeout: 90_000 },
    );
    await startButton.click();
    const startResp = await started;
    // 409 = the backend's 2-sweep capacity was taken by someone else.
    expect(startResp.status()).toBe(200);
    const jobId = asJobId(await startResp.json());
    expect(jobId).toBeTruthy();

    const progress = dialog.getByTestId('deep-progress');
    await expect(progress).toBeVisible({ timeout: 90_000 });

    const seen = new Set<string>();
    let enumerated = false;
    await expect
      .poll(
        async () => {
          const text = ((await progress.textContent()) ?? '').trim();
          const match = /^Scanning (\d+) \/ (\d+)$/.exec(text);
          if (!match) return false;
          if (Number(match[2]) > 0 && Number(match[1]) > 0) enumerated = true;
          seen.add(text);
          return enumerated && seen.size >= 2;
        },
        { timeout: 120_000, intervals: [1_000] },
      )
      .toBe(true);

    // Cancel flips the panel to the cancelled summary locally, before the
    // cancel POST resolves.
    //
    // Real mouse click, deliberately: this is the shipped regression for
    // 684b159, which stopped the popup's own PanelResizeHandles corner from
    // swallowing its footer buttons. Before that fix the `sw` hit-box covered
    // this button and the pointer was intercepted (437 retries in the
    // baseline run), forcing a keyboard workaround that no longer belongs here.
    const cancel = dialog.getByRole('button', { name: /^Cancel$/i });
    await expect(cancel).toBeEnabled();
    await cancel.click();
    await expect(dialog.getByTestId('deep-summary')).toContainText('Deep search cancelled', {
      timeout: 30_000,
    });
    await expect(startButton).toBeVisible();

    // Regression for the chip contract on the sweep section (the payload is
    // enumerated kind-blind, so the panel itself re-checks every row): the
    // sweep searches TRANSCRIPTS only, so switching that source off must drop
    // the deep rows. Deep hits are the popup's only `target="_blank"` anchors,
    // which makes the count an unambiguous read of that section.
    const transcriptChip = dialog.getByRole('button', { name: 'transcription', exact: true });
    const deepLinks = dialog.locator('a[target="_blank"]');
    await expect(transcriptChip).toHaveAttribute('aria-pressed', 'true');
    const shown = await deepLinks.count();
    await transcriptChip.click();
    await expect(transcriptChip).toHaveAttribute('aria-pressed', 'false');
    await expect.poll(() => deepLinks.count(), { timeout: 90_000, intervals: [1_000] }).toBe(0);
    await transcriptChip.click();
    await expect(transcriptChip).toHaveAttribute('aria-pressed', 'true');
    if (shown > 0) {
      await expect
        .poll(() => deepLinks.count(), { timeout: 90_000, intervals: [1_000] })
        .toBe(shown);
    }

    // …and the backend really stops sweeping, so no job is left running.
    await expect
      .poll(() => deepJobState(jobId), { timeout: 120_000, intervals: [2_000] })
      .toBe('settled');
  });

  test('deep sweep pause/resume round-trip parks and unparks the job', async () => {
    // The pause/resume endpoints (72c8fbe) drive the panel's single
    // Pause⇄Resume button and the `Paused` badge shipped in b94418f (both
    // covered by test 6). This pins the API seam itself on one job id:
    // running → pause 200 + paused:true → resume 200 + paused:false → cancel
    // → terminal, and a terminal job refusing a late pause (409).
    test.setTimeout(180_000);

    const start = await post(`${API_URL}/api/archive/search/deep`, {
      channel: DEEP_CHANNEL,
      query: DEEP_QUERY,
    });
    expect(start.ok).toBe(true);
    const jobId = asJobId(await start.json());
    expect(jobId).toBeTruthy();

    // `null` on a transient probe failure: the poll retries instead of
    // mistaking a blip for a state change.
    const jobStatus = async (): Promise<string | null> => {
      const probe = await deepJobSnapshot(jobId);
      return typeof probe === 'string' ? null : probe.status;
    };
    const jobPaused = async (): Promise<boolean | null> => {
      const probe = await deepJobSnapshot(jobId);
      return typeof probe === 'string' ? null : probe.paused;
    };

    await expect
      .poll(jobStatus, { timeout: 30_000, intervals: [1_000] })
      .toBe('running');

    const pause = await post(`${API_URL}/api/archive/search/deep/${jobId}/pause`, {});
    expect(pause.status).toBe(200);
    await expect
      .poll(jobPaused, { timeout: 20_000, intervals: [1_000] })
      .toBe(true);

    const resume = await post(`${API_URL}/api/archive/search/deep/${jobId}/resume`, {});
    expect(resume.status).toBe(200);
    await expect
      .poll(jobPaused, { timeout: 20_000, intervals: [1_000] })
      .toBe(false);

    // Cancel wins over a parked sweep, and a terminal job has nothing left to
    // park (409) — the seam the UI relies on to leave no orphan behind.
    const cancel = await post(`${API_URL}/api/archive/search/deep/${jobId}/cancel`, {});
    expect(cancel.status).toBe(200);
    await expect
      .poll(() => deepJobState(jobId), { timeout: 120_000, intervals: [2_000] })
      .toBe('settled');
    const latePause = await post(`${API_URL}/api/archive/search/deep/${jobId}/pause`, {});
    expect(latePause.status).toBe(409);
  });

  test('deep sweep renders results live and its Pause control parks the job', async ({ page }) => {
    // b94418f shipped two things on the running panel: the result list is
    // gated on `deepResults.length > 0` instead of a terminal status, so hits
    // appear per poll WHILE the sweep runs, and a single Pause⇄Resume button
    // whose `Paused` badge follows the polled `paused` flag. This is the
    // regression for the bug previously pinned as `test.fixme`.
    //
    // jb_sniper + `solo` is the target for exactly this reason: pass 1 flushes
    // 6 cached matches by `scanned 28 / 66` with zero network, so rows exist
    // long before pass 2 (remote fetches / the YouTube bot gate) can end it.
    test.setTimeout(300_000);
    const { dialog, searchInput } = await openArchiveSearch(page);
    await dialog.locator('select#archive-filter-channel').selectOption({ label: DEEP_CHANNEL_LABEL });
    await searchInput.fill(DEEP_QUERY);
    await dialog.getByLabel('Type confirmar to enable', { exact: true }).fill('confirmar');

    const started = page.waitForResponse(
      (r) => new URL(r.url()).pathname === '/api/archive/search/deep' && r.request().method() === 'POST',
      { timeout: 90_000 },
    );
    await dialog.getByRole('button', { name: /Start deep search/i }).click();
    const jobId = asJobId(await (await started).json());
    expect(jobId).toBeTruthy();

    const progress = dialog.getByTestId('deep-progress');
    await expect(progress).toBeVisible({ timeout: 90_000 });

    // The live header is running copy, not the terminal summary.
    const live = dialog.getByTestId('deep-live');
    await expect(live).toBeVisible({ timeout: 90_000 });
    await expect(dialog.getByTestId('deep-summary')).toHaveCount(0);

    // Rows mount before any terminal status: `deep-progress` (rendered only
    // while status === 'running') is still on screen when they appear.
    const deepLinks = dialog.locator('a[target="_blank"]');
    await expect
      .poll(() => deepLinks.count(), { timeout: 150_000, intervals: [2_000] })
      .toBeGreaterThan(0);
    await expect(progress).toBeVisible();
    await expect(live).toContainText('transcript matches', { timeout: 30_000 });

    // Park it from the UI. The badge is the polled state, so it survives the
    // 2 s tick; the button flips to its Resume face.
    const pauseButton = dialog.getByRole('button', { name: /^Pause$/i });
    await expect(pauseButton).toBeEnabled();
    await pauseButton.click();
    const badge = dialog.getByTestId('deep-paused-badge');
    await expect(badge).toBeVisible({ timeout: 30_000 });
    await expect(badge).toHaveText('Paused');
    const resumeButton = dialog.getByRole('button', { name: /^Resume$/i });
    await expect(resumeButton).toBeVisible();

    // …and unpark: the badge is gone while the sweep is still running.
    await resumeButton.click();
    await expect(badge).toHaveCount(0, { timeout: 30_000 });
    await expect(progress).toBeVisible();

    // Cancel with a real click, then confirm the job is not left running.
    await dialog.getByRole('button', { name: /^Cancel$/i }).click();
    await expect(dialog.getByTestId('deep-summary')).toContainText('Deep search cancelled', {
      timeout: 30_000,
    });
    await expect
      .poll(() => deepJobState(jobId), { timeout: 120_000, intervals: [2_000] })
      .toBe('settled');
  });
});
