/**
 * MINI EDITOR — the compact range picker that lives INSIDE a preview window.
 *
 * The user asked for "each preview to have its own mini editor where you can
 * pick a part and clip it to Twitch, or cut and download it right there". This
 * component is that surface: a rail with two needles, and two actions over the
 * selected range.
 *
 * It is deliberately the COMPACT sibling of TwitchClipPopup, not a second
 * implementation:
 *   - range maths come from `trimUtils` (clampTrimEndpoints /
 *     adjustTrimEndpointByDelta / trimButtonDeltaForEndpoint), the same calls
 *     the main window's trim rail and ±buttons use;
 *   - the Twitch bounds, validation and editor hand-off come from `twitchClip`
 *     (TWITCH_CLIP_MIN/MAX_SEC, twitchClipDurationError, ensureTwitchClipExtension,
 *     openTwitchClipEditorInBrowser) — the same path TwitchClipPopup drives;
 *   - the local cut POSTs /api/download/clip with crop_start/crop_end, which is
 *     the endpoint the main window already uses, so the trimmed file also gets
 *     its SRT sidecar REBASED by crop_start (see download_sidecars).
 *
 * A11y: both needles are role="slider" with aria-valuemin/max/now/valuetext and
 * are arrow-key operable (Shift = 5s, Home/End snap to the media bounds), and
 * the host marks the editor root `data-preview-mini-editor` so the popup's
 * player key handler ignores events that land inside it — a focused slider
 * owns its own arrow keys instead of also skipping the video.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { AlertCircle, ChevronDown, Loader2, Scissors } from 'lucide-react';
import { useI18n } from '../i18n';
import { formatHmsFull } from '../utils';
import { apiPost } from '../hooks/useApiClient';
import {
  MINI_EDITOR_NUDGE_BUTTONS,
  initialMiniEditorRange,
  miniEditorButtonDelta,
  miniEditorDownloadBody,
  miniEditorDownloadError,
  miniEditorKeyDelta,
  miniEditorRangeFromRail,
  miniEditorRangeLength,
  miniEditorRangeToEdge,
  miniEditorTwitchRangeError,
  miniEditorTwitchTargetError,
  nudgeMiniEditorRange,
  type MiniEditorRange,
} from '../previewMiniEditor';
import {
  ensureTwitchClipExtension,
  openTwitchClipEditorInBrowser,
  reportClipEvent,
} from '../twitchClip';
import type { ChatMarkers } from './ChatRangeMarkers';

export interface PreviewMiniEditorProps {
  /** VOD URL — the download target and the clip editor's source. */
  url: string;
  /** Original VOD title; Twitch's clip title keeps the VOD's title. */
  title: string;
  platform: string | null;
  /** Broadcaster login (Twitch) — required for the clip hand-off. */
  channel?: string | null;
  /** Native archive video id — required for the clip hand-off. */
  videoId?: string | null;
  /** Media duration in seconds; the rail and every clamp are relative to it. */
  durationSec: number;
  /** Current playhead (VOD-absolute seconds) — seeds the default selection. */
  playheadSec: number;
  /** Chat-range markers from the host's chat panel; carried into the cut. */
  chatMarkers?: ChatMarkers | null;
  /** Reuse the host's notice row instead of growing a second one. */
  onNotice?: (kind: 'error' | 'ok', text: string) => void;
  /** Called after a cut is queued (host may switch to the Queue tab). */
  onCutQueued?: () => void;
  /** Called when the panel opens/closes so the host can re-measure its chrome
   *  (the frame-mode snap pin re-centres on a chrome-height change). */
  onOpenChange?: (open: boolean) => void;
  /** Fullscreen uses the lighter overlay button skin. */
  fullscreen?: boolean;
  /** The app's "Save subtitles with downloads" setting. Undefined = on, the
   *  product default; an explicit false is the user turning it OFF, which the
   *  main window already honours — the cut must honour it too. */
  includeTranscript?: boolean;
}

export default function PreviewMiniEditor({
  url,
  title,
  platform,
  channel,
  videoId,
  durationSec,
  playheadSec,
  chatMarkers,
  onNotice,
  onCutQueued,
  onOpenChange,
  fullscreen = false,
  includeTranscript = true,
}: PreviewMiniEditorProps) {
  const { t } = useI18n();
  const [open, setOpen] = useState(false);
  const [range, setRange] = useState<MiniEditorRange>(() =>
    initialMiniEditorRange(playheadSec, durationSec),
  );
  const [active, setActive] = useState<'in' | 'out'>('in');
  const [busy, setBusy] = useState(false);
  const railRef = useRef<HTMLDivElement>(null);
  const dragRef = useRef<'in' | 'out' | null>(null);
  const activeRef = useRef<'in' | 'out'>('in');
  activeRef.current = active;

  const dur = Number.isFinite(durationSec) && durationSec > 0 ? durationSec : 0;
  const rangeRef = useRef(range);
  rangeRef.current = range;

  const notice = useCallback(
    (kind: 'error' | 'ok', text: string) => onNotice?.(kind, text),
    [onNotice],
  );

  // Opening the editor re-seeds the selection around the current playhead, so
  // the default cut always covers what the user is actually looking at.
  const toggleOpen = useCallback(() => {
    const next = !open;
    setOpen(next);
    if (next) setRange(initialMiniEditorRange(playheadSec, dur));
    onOpenChange?.(next);
  }, [open, playheadSec, dur, onOpenChange]);

  // A media change (new VOD in the same popup) invalidates the old range.
  useEffect(() => {
    setRange(initialMiniEditorRange(playheadSec, dur));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [url, dur]);

  const len = miniEditorRangeLength(range);
  const dlError = miniEditorDownloadError(range, dur);
  const twitchRangeError = miniEditorTwitchRangeError(range);
  const twitchTargetError = miniEditorTwitchTargetError({ platform, channel, videoId });
  const twitchDisabled = busy || !!twitchTargetError || !!twitchRangeError;
  const cutDisabled = busy || !!dlError;

  const startPct = dur > 0 ? (range.start / dur) * 100 : 0;
  const endPct = dur > 0 ? (range.end / dur) * 100 : 0;
  const playPct = dur > 0 ? Math.max(0, Math.min(100, (playheadSec / dur) * 100)) : 0;

  const secFromClientX = useCallback(
    (clientX: number): number => {
      const rail = railRef.current;
      if (!rail || dur <= 0) return 0;
      const rect = rail.getBoundingClientRect();
      if (rect.width <= 0) return 0;
      const frac = Math.max(0, Math.min(1, (clientX - rect.left) / rect.width));
      return frac * dur;
    },
    [dur],
  );

  // --- rail pointer drag: pins the opposite end, like the main trim rail ----
  const beginDrag = useCallback(
    (which: 'in' | 'out') => (e: React.PointerEvent<HTMLElement>) => {
      e.preventDefault();
      e.stopPropagation();
      setActive(which);
      activeRef.current = which;
      const rail = railRef.current;
      if (rail) {
        try { rail.setPointerCapture(e.pointerId); } catch { /* ignore */ }
      }
      dragRef.current = which;
      setRange((r) => miniEditorRangeFromRail(r, dur, which, secFromClientX(e.clientX)));
    },
    [dur, secFromClientX],
  );

  const onRailPointerMove = useCallback(
    (e: React.PointerEvent<HTMLDivElement>) => {
      const which = dragRef.current;
      if (!which) return;
      e.preventDefault();
      setRange((r) => miniEditorRangeFromRail(r, dur, which, secFromClientX(e.clientX)));
    },
    [dur, secFromClientX],
  );

  const endDrag = useCallback((e: React.PointerEvent<HTMLDivElement>) => {
    if (!dragRef.current) return;
    dragRef.current = null;
    try { e.currentTarget.releasePointerCapture(e.pointerId); } catch { /* ignore */ }
  }, []);

  /** A bare rail click moves the ACTIVE needle there (no drag needed). */
  const onRailClick = useCallback(
    (e: React.MouseEvent<HTMLDivElement>) => {
      if (e.target !== e.currentTarget) return;
      const which = activeRef.current;
      setActive(which);
      setRange((r) => miniEditorRangeFromRail(r, dur, which, secFromClientX(e.clientX)));
    },
    [dur, secFromClientX],
  );

  // --- keyboard: arrows nudge, Shift = 5s, Home/End snap to the bounds -----
  const onNeedleKeyDown = useCallback(
    (which: 'in' | 'out') => (e: React.KeyboardEvent<HTMLElement>) => {
      const k = miniEditorKeyDelta({ key: e.key, shiftKey: e.shiftKey }, which);
      if (!k) return;
      e.preventDefault();
      e.stopPropagation();
      setActive(which);
      activeRef.current = which;
      setRange((r) =>
        'edge' in k
          ? miniEditorRangeToEdge(r, dur, which, k.edge)
          : nudgeMiniEditorRange(r, dur, which, k.deltaSec),
      );
    },
    [dur],
  );

  // Reads the ACTIVE endpoint from the ref, never from a state updater body —
  // a setState call inside an updater is a side effect React may replay.
  const onAdjust = useCallback(
    (buttonDelta: number) => {
      const which = activeRef.current;
      setRange((r) => miniEditorButtonDelta(r, dur, which, buttonDelta));
    },
    [dur],
  );

  // --- action: cut + download the selected range ---------------------------
  const cutAndDownload = useCallback(async () => {
    const sel = rangeRef.current;
    const err = miniEditorDownloadError(sel, dur);
    if (err) { notice('error', err); return; }
    const body = miniEditorDownloadBody({
      url,
      range: sel,
      title,
      channel,
      durationSec: dur,
      includeTranscript,
      includeChat: !!(chatMarkers && (chatMarkers.start != null || chatMarkers.end != null)),
      chatStartSec: chatMarkers?.start ?? null,
      chatEndSec: chatMarkers?.end ?? null,
    });
    setBusy(true);
    try {
      await apiPost<{ download_id: string; status: string }>('/api/download/clip', body);
      notice(
        'ok',
        includeTranscript
          ? t('Cutting {start}-{end} — the SRT sidecar is rebased to the cut', {
              start: formatHmsFull(sel.start),
              end: formatHmsFull(sel.end),
            })
          : t('Cutting {start}-{end}', {
              start: formatHmsFull(sel.start),
              end: formatHmsFull(sel.end),
            }),
      );
      onCutQueued?.();
    } catch (e) {
      notice('error', e instanceof Error ? e.message : t('Download failed'));
    } finally {
      setBusy(false);
    }
  }, [url, title, channel, dur, chatMarkers, includeTranscript, notice, onCutQueued, t]);

  // --- action: clip the same range to Twitch -------------------------------
  const clipToTwitch = useCallback(async () => {
    const sel = rangeRef.current;
    const targetErr = miniEditorTwitchTargetError({ platform, channel, videoId });
    if (targetErr) { notice('error', targetErr); return; }
    const rangeErr = miniEditorTwitchRangeError(sel);
    if (rangeErr) { notice('error', rangeErr); return; }
    const clipTitle = (title || '').trim();
    if (!clipTitle) { notice('error', t('Original VOD title unavailable')); return; }
    setBusy(true);
    reportClipEvent('mini_editor_create_clicked', {
      method: 'browser',
      startSec: sel.start,
      endSec: sel.end,
      durationSec: sel.end - sel.start,
      title: clipTitle,
    });
    try {
      // Same extension pairing hand-off TwitchClipPopup uses; failures here are
      // non-fatal (the editor still opens, the extension just may not publish).
      await ensureTwitchClipExtension();
      openTwitchClipEditorInBrowser(
        videoId as string,
        channel as string,
        sel.start,
        sel.end,
        clipTitle,
        dur,
      );
      notice('ok', t('Opened in your browser — the VOD.RIP extension fills the editor and publishes'));
    } catch (e) {
      notice('error', e instanceof Error ? e.message : t('Could not open the Twitch editor'));
    } finally {
      setBusy(false);
    }
  }, [platform, channel, videoId, title, dur, notice, t]);

  const labelCls = fullscreen ? 'text-zinc-300/90' : 'text-zinc-500';
  const btnCls = fullscreen
    ? 'border border-white/20 bg-black/25 text-zinc-100 backdrop-blur-[1px] hover:border-white/50 disabled:opacity-30'
    : 'border border-zinc-700 bg-zinc-900 text-zinc-200 hover:border-white hover:text-white disabled:opacity-40';

  const needle = useMemo(
    () => (which: 'in' | 'out') => {
      const sec = which === 'in' ? range.start : range.end;
      return (
        <div
          role="slider"
          tabIndex={0}
          aria-label={which === 'in' ? t('Clip in') : t('Clip out')}
          aria-valuemin={0}
          aria-valuemax={Math.max(0, Math.floor(dur))}
          aria-valuenow={Math.round(sec)}
          aria-valuetext={formatHmsFull(sec)}
          data-mini-editor-needle={which}
          className={`preview-needle ${which === 'in' ? 'preview-needle-in' : 'preview-needle-out'} absolute top-0 bottom-0 -translate-x-1/2 z-[2] touch-none cursor-ew-resize focus:outline-none focus-visible:ring-1 focus-visible:ring-white`}
          style={{ left: `${which === 'in' ? startPct : endPct}%` }}
          onPointerDown={beginDrag(which)}
          onFocus={() => { setActive(which); activeRef.current = which; }}
          onKeyDown={onNeedleKeyDown(which)}
        />
      );
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [range.start, range.end, dur, startPct, endPct, beginDrag, onNeedleKeyDown, t],
  );

  return (
    <div data-preview-mini-editor="" className="w-full shrink-0">
      <button
        type="button"
        onClick={toggleOpen}
        aria-expanded={open}
        aria-controls="mini-editor-panel"
        data-mini-editor-toggle
        className={`w-full flex items-center gap-1.5 px-1 py-0.5 text-left ${btnCls}`}
        title={t('Pick a range, then clip to Twitch or cut and download it here')}
      >
        <Scissors size={11} className="shrink-0" />
        <span className="text-[8px] font-mono uppercase tracking-widest font-bold truncate">
          {t('Editor')}
        </span>
        <span className={`text-[8px] font-mono ml-auto tabular-nums ${labelCls}`}>
          {formatHmsFull(range.start)}–{formatHmsFull(range.end)}
        </span>
        <ChevronDown
          size={11}
          className={`shrink-0 transition-transform ${open ? 'rotate-180' : ''}`}
        />
      </button>

      {open && (
        <div
          id="mini-editor-panel"
          role="group"
          aria-label={t('Mini editor')}
          data-mini-editor-panel
          className="flex flex-col gap-1 mt-1 px-0.5"
        >
          <div className="flex items-stretch gap-1.5">
            <span className={`text-[8px] font-mono w-11 shrink-0 self-center ${labelCls}`}>
              {t('Cut')}
            </span>
            <div
              ref={railRef}
              className={`preview-needle-rail relative flex-1 ${
                fullscreen ? 'bg-white/10' : 'bg-zinc-800/80'
              }`}
              title={t('Click the rail to move the active needle')}
              onClick={onRailClick}
              onPointerMove={onRailPointerMove}
              onPointerUp={endDrag}
              onPointerCancel={endDrag}
            >
              <div
                className="preview-needle-region absolute top-1/2 -translate-y-1/2 h-1 pointer-events-none"
                style={{
                  left: `${startPct}%`,
                  width: `${Math.max(0, endPct - startPct)}%`,
                }}
              />
              <div
                className="preview-needle-playhead absolute top-0 bottom-0 w-px bg-white/50 -translate-x-1/2 pointer-events-none z-[1]"
                style={{ left: `${playPct}%` }}
              />
              {needle('in')}
              {needle('out')}
            </div>
            <span
              className={`text-[8px] font-mono w-11 shrink-0 text-right self-center tabular-nums ${labelCls}`}
              data-mini-editor-length
              title={t('Selected cut length')}
            >
              {formatHmsFull(len)}
            </span>
          </div>

          <div className="flex items-center gap-1.5 flex-wrap">
            <span className={`text-[8px] font-mono w-11 shrink-0 ${labelCls}`}>
              {active === 'in' ? t('Start') : t('End')}
            </span>
            <div className="flex items-center gap-0.5 shrink-0">
              {MINI_EDITOR_NUDGE_BUTTONS.map((d) => (
                <button
                  key={d}
                  type="button"
                  onClick={() => onAdjust(d)}
                  disabled={busy || dur <= 0}
                  data-mini-editor-nudge={d}
                  className={`px-1 py-0 text-[8px] font-mono font-bold ${btnCls}`}
                  title={
                    active === 'in'
                      ? d < 0
                        ? t('Extend cut 5s at start')
                        : t('Trim 5s from start')
                      : d > 0
                        ? t('Extend cut 5s at end')
                        : t('Trim 5s from end')
                  }
                >
                  {d > 0 ? `+${d}s` : `${d}s`}
                </button>
              ))}
            </div>
            <div className="flex items-center gap-1 ml-auto">
              <button
                type="button"
                onClick={() => void clipToTwitch()}
                disabled={twitchDisabled}
                data-mini-editor-twitch
                className={`flex items-center gap-1 px-1.5 py-0.5 text-[8px] font-mono font-bold uppercase tracking-wider ${btnCls}`}
                title={
                  twitchTargetError
                    || twitchRangeError
                    || t('Open this range in the Twitch clip editor')
                }
              >
                {busy ? <Loader2 size={10} className="animate-spin" /> : null}
                {t('Clip')}
              </button>
              <button
                type="button"
                onClick={() => void cutAndDownload()}
                disabled={cutDisabled}
                data-mini-editor-cut
                className={`flex items-center gap-1 px-1.5 py-0.5 text-[8px] font-mono font-bold uppercase tracking-wider ${btnCls}`}
                title={dlError || t('Cut this range and download it here')}
              >
                {busy ? <Loader2 size={10} className="animate-spin" /> : null}
                {t('Cut & download')}
              </button>
            </div>
          </div>

          {/* Screen-reader legibility: the reason an action is unavailable is
              announced, not only shown as a disabled button. */}
          <p aria-live="polite" className="sr-only">
            {twitchTargetError
              || twitchRangeError
              || dlError
              || t('Selected range {start} to {end}', {
                start: formatHmsFull(range.start),
                end: formatHmsFull(range.end),
              })}
          </p>
          {(twitchRangeError || dlError) && !twitchTargetError && (
            <span
              data-mini-editor-hint
              className="flex items-center gap-1 text-[8px] font-mono text-zinc-500"
            >
              <AlertCircle size={9} className="shrink-0" />
              <span className="truncate">{dlError || twitchRangeError}</span>
            </span>
          )}
        </div>
      )}
    </div>
  );
}
