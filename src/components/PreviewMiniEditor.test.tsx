import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import PreviewMiniEditor from './PreviewMiniEditor';

// The download cut POSTs through apiPost; assert on the exact body instead of
// driving a real fetch (the body IS the contract — crop_start/crop_end must
// reach the backend so download_sidecars can rebase the SRT).
const apiPost = vi.fn().mockResolvedValue({ download_id: 'd1', status: 'started' });
vi.mock('../hooks/useApiClient', () => ({
  apiPost: (...args: unknown[]) => apiPost(...args),
  apiGet: vi.fn().mockResolvedValue({ paired: true }),
  apiDelete: vi.fn().mockResolvedValue({ ok: true, removed: 0 }),
}));

// The Twitch hand-off opens a real browser window — stub the two twitchClip
// entry points, keep the real range maths (it is trimUtils that is under test
// here, not the extension pairing).
const openTwitchClipEditorInBrowser = vi.fn();
const ensureTwitchClipExtension = vi.fn().mockResolvedValue({ ok: true, installed: false });
vi.mock('../twitchClip', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../twitchClip')>();
  return {
    ...actual,
    openTwitchClipEditorInBrowser: (...a: unknown[]) => openTwitchClipEditorInBrowser(...a),
    ensureTwitchClipExtension: (...a: unknown[]) => ensureTwitchClipExtension(...a),
    reportClipEvent: vi.fn(),
  };
});

const captureProto = HTMLElement.prototype as unknown as {
  setPointerCapture?: (id: number) => void;
  releasePointerCapture?: (id: number) => void;
};
beforeEach(() => {
  if (!captureProto.setPointerCapture) captureProto.setPointerCapture = () => {};
  if (!captureProto.releasePointerCapture) captureProto.releasePointerCapture = () => {};
  apiPost.mockClear();
  openTwitchClipEditorInBrowser.mockClear();
});

function renderEditor(over: Partial<React.ComponentProps<typeof PreviewMiniEditor>> = {}) {
  const props = {
    url: 'https://www.twitch.tv/videos/123456789',
    title: 'jantando o guiven parte 1',
    platform: 'twitch',
    channel: 'somebody',
    videoId: '123456789',
    durationSec: 3600,
    playheadSec: 120,
    ...over,
  };
  return { ...render(<PreviewMiniEditor {...props} />), props };
}

const inNeedle = () => screen.getByRole('slider', { name: 'Clip in' });
const outNeedle = () => screen.getByRole('slider', { name: 'Clip out' });

describe('PreviewMiniEditor — range selection', () => {
  it('opens from a single in-preview toggle and seeds the cut at the playhead', () => {
    renderEditor();
    const toggle = document.querySelector('[data-mini-editor-toggle]') as HTMLButtonElement;
    expect(toggle.getAttribute('aria-expanded')).toBe('false');
    expect(screen.queryByRole('slider')).toBeNull();

    fireEvent.click(toggle);
    expect(toggle.getAttribute('aria-expanded')).toBe('true');
    expect(inNeedle()).toBeTruthy();
    expect(inNeedle().getAttribute('aria-valuenow')).toBe('120');
    expect(outNeedle().getAttribute('aria-valuenow')).toBe('150');
  });

  it('exposes slider ARIA (min/max/now/valuetext) on both needles', () => {
    renderEditor();
    fireEvent.click(document.querySelector('[data-mini-editor-toggle]') as HTMLButtonElement);
    const n = inNeedle();
    expect(n.getAttribute('aria-valuemin')).toBe('0');
    expect(n.getAttribute('aria-valuemax')).toBe('3600');
    expect(n.getAttribute('aria-valuetext')).toMatch(/00:0?2:00|2:00/);
    expect(n.getAttribute('tabindex')).toBe('0');
  });

  it('is keyboard operable: arrows nudge the cut, Shift is the fast step', () => {
    renderEditor();
    fireEvent.click(document.querySelector('[data-mini-editor-toggle]') as HTMLButtonElement);

    // ArrowRight on the IN needle trims 1s off the start.
    fireEvent.keyDown(inNeedle(), { key: 'ArrowRight' });
    expect(inNeedle().getAttribute('aria-valuenow')).toBe('121');

    // Shift+ArrowRight on the OUT needle extends the end by 5s.
    fireEvent.keyDown(outNeedle(), { key: 'ArrowRight', shiftKey: true });
    expect(outNeedle().getAttribute('aria-valuenow')).toBe('155');
  });

  it('End on the out-needle snaps to the media bound', () => {
    renderEditor();
    fireEvent.click(document.querySelector('[data-mini-editor-toggle]') as HTMLButtonElement);
    fireEvent.keyDown(outNeedle(), { key: 'End' });
    expect(outNeedle().getAttribute('aria-valuenow')).toBe('3600');
  });

  it('moves the active needle when the rail itself is clicked', () => {
    renderEditor();
    fireEvent.click(document.querySelector('[data-mini-editor-toggle]') as HTMLButtonElement);
    const rail = document.querySelector('[data-mini-editor-panel] .preview-needle-rail') as HTMLElement;
    // jsdom has no layout — stub the rail rect so clientX maps to a fraction.
    rail.getBoundingClientRect = () =>
      ({ left: 0, width: 1000, top: 0, height: 10, right: 1000, bottom: 10, x: 0, y: 0, toJSON: () => ({}) }) as DOMRect;

    // Focus (not click) is what marks a needle active — a click with no
    // pointerdown is not a real user gesture.
    fireEvent.focus(inNeedle());
    // 10px of 1000 = 1% of 3600 = 36s, which sits INSIDE the [120,150] range.
    fireEvent.click(rail, { clientX: 10 });
    expect(inNeedle().getAttribute('aria-valuenow')).toBe('36');
    expect(outNeedle().getAttribute('aria-valuenow')).toBe('150');
  });

  it('does not let a needle be dragged past the other one', () => {
    renderEditor();
    fireEvent.click(document.querySelector('[data-mini-editor-toggle]') as HTMLButtonElement);
    const rail = document.querySelector('[data-mini-editor-panel] .preview-needle-rail') as HTMLElement;
    rail.getBoundingClientRect = () =>
      ({ left: 0, width: 1000, top: 0, height: 10, right: 1000, bottom: 10, x: 0, y: 0, toJSON: () => ({}) }) as DOMRect;

    fireEvent.focus(inNeedle());
    // 100px = 10% = 360s, well past the out-needle at 150s — the pin holds.
    fireEvent.click(rail, { clientX: 100 });
    expect(inNeedle().getAttribute('aria-valuenow')).toBe('149');
  });

  it('the ±buttons adjust the active endpoint', () => {
    renderEditor();
    fireEvent.click(document.querySelector('[data-mini-editor-toggle]') as HTMLButtonElement);
    fireEvent.focus(outNeedle());
    fireEvent.click(document.querySelector('[data-mini-editor-nudge="5"]') as HTMLButtonElement);
    expect(outNeedle().getAttribute('aria-valuenow')).toBe('155');
  });
});

describe('PreviewMiniEditor — cut and download', () => {
  it('POSTs /api/download/clip with the selected crop window and the SRT sidecar', async () => {
    renderEditor();
    fireEvent.click(document.querySelector('[data-mini-editor-toggle]') as HTMLButtonElement);
    fireEvent.click(document.querySelector('[data-mini-editor-cut]') as HTMLButtonElement);

    await waitFor(() => expect(apiPost).toHaveBeenCalledTimes(1));
    const [path, body] = apiPost.mock.calls[0] as [string, Record<string, unknown>];
    expect(path).toBe('/api/download/clip');
    // THE alignment contract: the trim travels to the backend as crop_start /
    // crop_end, and download_sidecars rebases the SRT by crop_start.
    expect(body.crop_start).toBe(120);
    expect(body.crop_end).toBe(150);
    expect(body.include_transcript).toBe(true);
    expect(body.url).toBe('https://www.twitch.tv/videos/123456789');
    expect(body.title).toBe('jantando o guiven parte 1');
  });

  it('carries the chat markers into the cut when they are set', async () => {
    renderEditor({ chatMarkers: { start: 122, end: null } });
    fireEvent.click(document.querySelector('[data-mini-editor-toggle]') as HTMLButtonElement);
    fireEvent.click(document.querySelector('[data-mini-editor-cut]') as HTMLButtonElement);
    await waitFor(() => expect(apiPost).toHaveBeenCalledTimes(1));
    const body = apiPost.mock.calls[0][1] as Record<string, unknown>;
    expect(body.include_chat).toBe(true);
    expect(body.chat_start_sec).toBe(122);
  });

  it('stays enabled for a cut longer than Twitch allows (local cuts are unbounded)', () => {
    renderEditor();
    fireEvent.click(document.querySelector('[data-mini-editor-toggle]') as HTMLButtonElement);
    fireEvent.keyDown(outNeedle(), { key: 'End' });
    expect((document.querySelector('[data-mini-editor-cut]') as HTMLButtonElement).disabled).toBe(false);
  });

  it('reports a failed cut through the host notice row', async () => {
    apiPost.mockRejectedValueOnce(new Error('boom'));
    const onNotice = vi.fn();
    renderEditor({ onNotice });
    fireEvent.click(document.querySelector('[data-mini-editor-toggle]') as HTMLButtonElement);
    fireEvent.click(document.querySelector('[data-mini-editor-cut]') as HTMLButtonElement);
    await waitFor(() => expect(onNotice).toHaveBeenCalledWith('error', 'boom'));
  });
});

describe('PreviewMiniEditor — clip to Twitch', () => {
  it('hands the selected range to the Twitch clip editor', async () => {
    renderEditor();
    fireEvent.click(document.querySelector('[data-mini-editor-toggle]') as HTMLButtonElement);
    fireEvent.click(document.querySelector('[data-mini-editor-twitch]') as HTMLButtonElement);

    await waitFor(() => expect(openTwitchClipEditorInBrowser).toHaveBeenCalledTimes(1));
    expect(openTwitchClipEditorInBrowser).toHaveBeenCalledWith(
      '123456789',
      'somebody',
      120,
      150,
      'jantando o guiven parte 1',
      3600,
    );
    // The clip action must NOT queue a download behind the user's back.
    expect(apiPost).not.toHaveBeenCalled();
  });

  it('reuses the cookie-extension pairing hand-off', async () => {
    renderEditor();
    fireEvent.click(document.querySelector('[data-mini-editor-toggle]') as HTMLButtonElement);
    fireEvent.click(document.querySelector('[data-mini-editor-twitch]') as HTMLButtonElement);
    await waitFor(() => expect(ensureTwitchClipExtension).toHaveBeenCalled());
  });

  it('is disabled when the range is outside Twitch 5..60s', () => {
    renderEditor();
    fireEvent.click(document.querySelector('[data-mini-editor-toggle]') as HTMLButtonElement);
    const btn = document.querySelector('[data-mini-editor-twitch]') as HTMLButtonElement;
    expect(btn.disabled).toBe(false);

    fireEvent.keyDown(outNeedle(), { key: 'End' }); // ~3480s
    expect(btn.disabled).toBe(true);
    expect(btn.title).toMatch(/60s or less/);
  });

  it('is disabled for a non-Twitch VOD and explains why', () => {
    renderEditor({ platform: 'youtube' });
    fireEvent.click(document.querySelector('[data-mini-editor-toggle]') as HTMLButtonElement);
    const btn = document.querySelector('[data-mini-editor-twitch]') as HTMLButtonElement;
    expect(btn.disabled).toBe(true);
    expect(btn.title).toMatch(/Twitch VOD/);
  });

  it('never calls the editor when the target is unusable', async () => {
    renderEditor({ platform: 'youtube' });
    fireEvent.click(document.querySelector('[data-mini-editor-toggle]') as HTMLButtonElement);
    fireEvent.click(document.querySelector('[data-mini-editor-twitch]') as HTMLButtonElement);
    await Promise.resolve();
    expect(openTwitchClipEditorInBrowser).not.toHaveBeenCalled();
  });
});
