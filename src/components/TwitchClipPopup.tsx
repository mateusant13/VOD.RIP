/**
 * Twitch clip mini-preview — the "Twitch clip" button on the main preview and
 * the explore popup opens this floating player on a ±60s window around the
 * click moment instead of jumping straight to Twitch. The user trims a 5..60s
 * selection on the window timeline, then 'Create clip' opens Twitch's own
 * clip editor in the OS default browser with the selection as vodrip_* params
 * (vod_offset = selection END — see twitchClip.ts); the VOD.RIP cookie
 * extension (clip_assist.mjs content script) fills the title, drives the
 * editor's window and clicks Save, then posts the published clip URL to
 * /api/twitch/clips/record so it shows in the app's clip history with a
 * download button. The browser path works with the plain session cookie —
 * no API token scopes or editor role needed.
 *
 * The mini preview reuses the main preview's session machinery: one session
 * per popup with crop_start/crop_end = the window, exactly like App.tsx's
 * openPreview passes the trim range. ponytail: no quality menu here — the
 * session starts at the fast-start tier (360p) and plays the default level;
 * the main/explore previews remain the full quality experience.
 */

import {
  useCallback, useEffect, useMemo, useRef, useState,
  type PointerEvent as ReactPointerEvent,
} from 'react';
import { createPortal } from 'react-dom';
import Hls from 'hls.js';
import { Loader2, Pause, Play, RefreshCw, Volume2, VolumeX, X } from 'lucide-react';
import { apiDelete, apiGet, apiPost } from '../hooks/useApiClient';
import { useI18n } from '../i18n';
import {
  TWITCH_CLIP_MAX_SEC,
  TWITCH_CLIP_MIN_SEC,
  TWITCH_CLIP_TITLE_MAX,
  clampClipSelection,
  initialClipSelection,
  openTwitchClipEditorInBrowser,
  reportClipEvent,
  twitchClipDurationError,
  twitchClipWindow,
} from '../twitchClip';
import {
  PREVIEW_FAST_START_HEIGHT,
  attachPreviewBufferingListeners,
  attachProgressivePreview,
  createPreviewSessionWithRetry,
  detachProgressivePreview,
  playPreviewWithAudio,
  resolvePreviewPlayback,
} from '../previewPlayerUtils';
import { fracToSec, secToFrac, trimButtonDeltaForEndpoint } from '../trimUtils';
import { PREVIEW_DEFAULT_VOLUME } from '../layoutUtils';
import { twitchAdBlockHlsConfig } from '../twitchAdBlock';
import { formatHmsFull } from '../utils';
import ClipDurationAdjustButtons from './ClipDurationAdjustButtons';
import EditableHmsTime from './EditableHmsTime';
import TwitchLogoIcon from './TwitchLogoIcon';

const POPUP_W = 460;

interface TwitchClipPopupProps {
  /** VOD URL the mini preview plays (same session machinery as the main preview). */
  url: string;
  broadcasterLogin: string;
  vodId: string;
  /** VOD time of the click — the ±60s window is centred here (unless
   * anchorRange is set, which takes precedence). */
  playheadSec: number;
  /** VOD length; <=0/unknown → the upper window edge is unclamped. */
  vodDurationSec: number;
  /** The opening preview's typed trim range (H:M:S fields). When valid
   * (end > start), the popup window centres on it and the initial clip
   * selection IS the trim — the clip comes from where the user pointed. */
  anchorRange?: { start: number; end: number };
  /** VOD/live title — the clip title defaults to it (the user requires the
   * live's title verbatim, never a "VOD.RIP …" default). */
  vodTitle?: string;
  zIndex: number;
  onClose: () => void;
  /** Volume the popup should start at — inherited from the opening preview
   * so opening the clip window never resets the user's volume level. */
  initialVolume?: number;
  /** Session of the opening preview (same VOD, full-VOD HLS proxy): reuse it
   * so segments already fetched hit that session's disk cache instead of
   * re-downloading from the CDN. Skipped when trimTimeline is true (the
   * preview HLS is a short trim, not the full VOD). */
  reuseSession?: { sessionId: string; trimTimeline: boolean } | null;
}

export default function TwitchClipPopup({
  url,
  broadcasterLogin,
  vodId,
  playheadSec,
  vodDurationSec,
  anchorRange,
  vodTitle,
  zIndex,
  onClose,
  initialVolume = PREVIEW_DEFAULT_VOLUME,
  reuseSession = null,
}: TwitchClipPopupProps) {
  const { t } = useI18n();
  const volumeRef = useRef(initialVolume);
  const win = useMemo(
    () => twitchClipWindow(playheadSec, vodDurationSec, anchorRange),
    [playheadSec, vodDurationSec, anchorRange],
  );
  const winLen = win.end - win.start;
  const windowTooShort = winLen < TWITCH_CLIP_MIN_SEC;

  const [selection, setSelection] = useState(() => initialClipSelection(win, anchorRange));
  const selectionRef = useRef(selection);
  const commitSelection = useCallback((next: { start: number; end: number }) => {
    selectionRef.current = next;
    setSelection(next);
  }, []);
  const [lastEndpoint, setLastEndpoint] = useState<'in' | 'out'>('out');
  const lastEndpointRef = useRef<'in' | 'out'>('out');
  const markEndpoint = useCallback((which: 'in' | 'out') => {
    lastEndpointRef.current = which;
    setLastEndpoint(which);
  }, []);
  const selLen = Math.max(0, selection.end - selection.start);
  // User-chosen clip title — becomes the clip's Twitch title and the local
  // Defaults to the VOD/live title (user-mandated: the clip title is the
  // live's title, never a "VOD.RIP …" placeholder). Empty only when the
  // caller has no title either — then Twitch auto-titles.
  const [clipTitle, setClipTitle] = useState(vodTitle ?? '');

  // Floating panel position (draggable via the header, like the other popups).
  const [position, setPosition] = useState(() => ({
    x: Math.max(8, window.innerWidth - POPUP_W - 24),
    y: 80,
  }));
  const posRef = useRef(position);
  const [drag, setDrag] = useState<{
    startX: number; startY: number; offsetX: number; offsetY: number;
  } | null>(null);

  const popupRef = useRef<HTMLDivElement>(null);
  const videoRef = useRef<HTMLVideoElement>(null);
  const railRef = useRef<HTMLDivElement>(null);
  const hlsRef = useRef<Hls | null>(null);
  const sessionIdRef = useRef<string | null>(null);
  const timelineOffsetRef = useRef(0);
  const currentTimeRef = useRef(Math.max(win.start, Math.min(win.end, playheadSec)));

  const [playback, setPlayback] = useState<{
    url: string;
    kind: 'hls' | 'progressive';
  } | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [ready, setReady] = useState(false);
  const [buffering, setBuffering] = useState(false);
  const [playing, setPlaying] = useState(false);
  const [muted, setMuted] = useState(false);
  const [currentTime, setCurrentTime] = useState(currentTimeRef.current);
  const [retryTick, setRetryTick] = useState(0);
  const [clipNotice, setClipNotice] = useState<{ kind: 'error' | 'ok'; text: string } | null>(null);
  const clipNoticeTimerRef = useRef<number | null>(null);

  const showClipNotice = useCallback((kind: 'error' | 'ok', text: string) => {
    if (clipNoticeTimerRef.current) window.clearTimeout(clipNoticeTimerRef.current);
    setClipNotice({ kind, text });
    clipNoticeTimerRef.current = window.setTimeout(() => setClipNotice(null), 4000);
  }, []);

  // ── Preview session (mirrors App.tsx openPreview: crop window = trim range) ──
  useEffect(() => {
    let cancelled = false;
    // Only delete the session we created — a reused parent session is owned
    // by the opening preview and must survive the popup.
    let ownsSession = !reuseSession?.sessionId || reuseSession.trimTimeline === true;
    (async () => {
      if (reuseSession?.sessionId && !reuseSession.trimTimeline) {
        // Same-VOD full-HLS session from the opening preview: adopt it so the
        // proxy serves segments from that session's disk cache instead of
        // re-downloading from the CDN (and skip the GQL re-resolve). Probe the
        // master first — a stale session falls through to a fresh one.
        const sid = reuseSession.sessionId;
        try {
          const probe = await fetch(`/api/preview/hls/${sid}/master.m3u8`);
          if (!probe.ok) throw new Error(`stale session ${sid.slice(0, 8)}`);
          if (cancelled) return;
          sessionIdRef.current = sid;
          // Full-VOD HLS proxy (no window mux): video time is absolute VOD time.
          timelineOffsetRef.current = 0;
          setPlayback({ url: `/api/preview/hls/${sid}/master.m3u8`, kind: 'hls' });
          setLoading(false);
          return;
        } catch {
          ownsSession = true; // stale/missing — fall through to a fresh session
        }
      }
      try {
        const res = await createPreviewSessionWithRetry({
          url,
          crop_start: win.start,
          crop_end: win.end,
          prefer_height: PREVIEW_FAST_START_HEIGHT,
        });
        if (cancelled) {
          // ponytail: StrictMode double-invoke — both runs share the SAME
          // session (inflight dedup in createPreviewSessionWithRetry). Only
          // delete when the applied run used a different session.
          const orphanSid = res.session_id;
          window.setTimeout(() => {
            if (sessionIdRef.current !== orphanSid) {
              void apiDelete(`/api/preview/session/${orphanSid}`).catch(() => {});
            }
          }, 5000);
          return;
        }
        sessionIdRef.current = res.session_id;
        // Window-muxed MP4 (trim_timeline) is 0-based; Twitch VOD HLS is
        // absolute VOD time. offset maps video time → VOD time.
        timelineOffsetRef.current = res.trim_timeline === true ? win.start : 0;
        setPlayback({ ...resolvePreviewPlayback(url, res) });
        setLoading(false);
      } catch (err: unknown) {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : t('Could not start preview'));
          setLoading(false);
        }
      }
    })();
    return () => {
      cancelled = true;
      const hls = hlsRef.current;
      if (hls) {
        try { hls.stopLoad(); hls.detachMedia(); hls.destroy(); } catch { /* ignore */ }
        hlsRef.current = null;
      }
      const video = videoRef.current;
      if (video) detachProgressivePreview(video);
      const sid = sessionIdRef.current;
      sessionIdRef.current = null;
      if (sid && ownsSession) {
        void apiDelete(`/api/preview/session/${sid}`).catch(() => {});
      }
    };
  }, [url, win.start, win.end, retryTick, reuseSession]);

  // ── Playback attach (HLS for Twitch VODs, progressive fallback) ──
  useEffect(() => {
    if (!playback?.url) return;
    let cancelled = false;
    const video = videoRef.current;
    if (!video) return;
    const bufferingHandle = attachPreviewBufferingListeners(video, (s) => {
      if (!cancelled) setBuffering(s);
    });
    setLoading(true);
    setBuffering(false);
    setReady(false);

    const initialVideoTime = timelineOffsetRef.current > 0
      ? Math.max(0, playheadSec - win.start)
      : Math.max(win.start, Math.min(win.end, playheadSec));

    const clampToWindow = () => {
      const base = timelineOffsetRef.current;
      const lo = Math.max(0, win.start - base);
      const hi = Math.max(lo, win.end - base);
      let t = video.currentTime;
      if (t < lo) t = lo;
      else if (t > hi) {
        t = hi;
        if (!video.paused) video.pause();
      }
      if (Math.abs(video.currentTime - t) > 0.05) video.currentTime = t;
      const vodTime = t + base;
      currentTimeRef.current = vodTime;
      setCurrentTime(vodTime);
    };
    video.addEventListener('timeupdate', clampToWindow);

    const onCanPlay = () => {
      if (cancelled) return;
      setReady(true);
      setBuffering(false);
      setLoading(false);
      if (video.paused) {
        void playPreviewWithAudio(video, setMuted, volumeRef.current).then(() => {
          setPlaying(!video.paused);
        });
      }
    };

    if (playback.kind === 'progressive') {
      attachProgressivePreview(video, playback.url);
      video.addEventListener('loadedmetadata', () => {
        // window-muxed MP4 is 0-based (offset set above) — land the click moment.
        const t = timelineOffsetRef.current > 0
          ? Math.max(0, playheadSec - win.start)
          : Math.max(win.start, Math.min(win.end, playheadSec));
        video.currentTime = t;
      }, { once: true });
      video.addEventListener('canplay', onCanPlay, { once: true });
      return () => {
        cancelled = true;
        video.removeEventListener('timeupdate', clampToWindow);
        video.removeEventListener('canplay', onCanPlay);
        detachProgressivePreview(video);
        bufferingHandle.detach();
      };
    }

    const hls = new Hls({
      enableWorker: true,
      lowLatencyMode: false,
      backBufferLength: 12,
      maxBufferLength: 6,
      maxMaxBufferLength: 12,
      startFragPrefetch: true,
      fragLoadingTimeOut: 20000,
      manifestLoadingTimeOut: 10000,
      testBandwidth: false,
      ...twitchAdBlockHlsConfig({}),
      startPosition: initialVideoTime,
    });
    hlsRef.current = hls;
    hls.attachMedia(video);
    hls.on(Hls.Events.MANIFEST_PARSED, () => {
      if (!cancelled) hls.startLoad(initialVideoTime);
    });
    hls.on(Hls.Events.ERROR, (_evt, data) => {
      if (cancelled || !data.fatal) return;
      if (data.type === Hls.ErrorTypes.NETWORK_ERROR) {
        hls.startLoad();
      } else if (data.type === Hls.ErrorTypes.MEDIA_ERROR) {
        hls.recoverMediaError();
      } else {
        setError(t('Preview failed — retry'));
        setLoading(false);
      }
    });
    hls.loadSource(playback.url);
    video.addEventListener('canplay', onCanPlay, { once: true });

    return () => {
      cancelled = true;
      video.removeEventListener('timeupdate', clampToWindow);
      video.removeEventListener('canplay', onCanPlay);
      try { hls.stopLoad(); hls.detachMedia(); hls.destroy(); } catch { /* ignore */ }
      if (hlsRef.current === hls) hlsRef.current = null;
      bufferingHandle.detach();
    };
    // Re-attach only when the session's playback actually changes (a retry
    // creates a fresh session → new playback object). retryTick must NOT be a
    // dep: it would rebuild the player against the just-deleted session.
  }, [playback, win.start, win.end, playheadSec]);

  const seekTo = useCallback((vodSec: number) => {
    const video = videoRef.current;
    if (!video) return;
    const t = Math.max(win.start, Math.min(win.end, vodSec));
    video.currentTime = Math.max(0, t - timelineOffsetRef.current);
    currentTimeRef.current = t;
    setCurrentTime(t);
  }, [win]);

  const togglePlay = useCallback(() => {
    const video = videoRef.current;
    if (!video || !ready) return;
    if (video.paused) {
      void playPreviewWithAudio(video, setMuted, volumeRef.current).then(() => {
        setPlaying(!video.paused);
      });
    } else {
      video.pause();
      setPlaying(false);
    }
  }, [ready]);

  const toggleMute = useCallback(() => {
    const video = videoRef.current;
    if (!video) return;
    video.muted = !video.muted;
    setMuted(video.muted);
  }, []);

  // ── Trimmer ──
  const beginHandleDrag = useCallback((
    e: ReactPointerEvent<HTMLElement>,
    which: 'in' | 'out',
  ) => {
    markEndpoint(which);
    e.preventDefault();
    e.stopPropagation();
    const rail = railRef.current;
    if (!rail) return;
    const handle = e.currentTarget;
    const pointerId = e.pointerId;
    handle.setPointerCapture(pointerId);
    const fixed = which === 'in' ? selectionRef.current.end : selectionRef.current.start;
    const prevUserSelect = document.body.style.userSelect;
    document.body.style.userSelect = 'none';

    const xToSec = (clientX: number) => {
      const rect = rail.getBoundingClientRect();
      if (rect.width <= 0) return win.start;
      const frac = Math.max(0, Math.min(1, (clientX - rect.left) / rect.width));
      return fracToSec(frac, win);
    };

    let ended = false;
    const endDrag = () => {
      if (ended) return;
      ended = true;
      document.body.style.userSelect = prevUserSelect;
      handle.removeEventListener('pointermove', onMove);
      handle.removeEventListener('pointerup', endDrag);
      handle.removeEventListener('pointercancel', endDrag);
      handle.removeEventListener('lostpointercapture', endDrag);
      try { handle.releasePointerCapture(pointerId); } catch { /* ignore */ }
    };
    const onMove = (ev: PointerEvent) => {
      if (ev.pointerId !== pointerId) return;
      const sec = xToSec(ev.clientX);
      const res = which === 'in'
        ? clampClipSelection(sec, fixed, win.start, win.end, { move: 'in', fixedEnd: fixed })
        : clampClipSelection(fixed, sec, win.start, win.end, { move: 'out', fixedStart: fixed });
      commitSelection(res);
    };
    handle.addEventListener('pointermove', onMove);
    handle.addEventListener('pointerup', endDrag);
    handle.addEventListener('pointercancel', endDrag);
    handle.addEventListener('lostpointercapture', endDrag);
  }, [win, commitSelection, markEndpoint]);

  const adjustSelection = useCallback((buttonDelta: number) => {
    const sel = selectionRef.current;
    const which = lastEndpointRef.current;
    const delta = trimButtonDeltaForEndpoint(which, buttonDelta);
    const res = which === 'in'
      ? clampClipSelection(
        sel.start - delta, sel.end, win.start, win.end,
        { move: 'in', fixedEnd: sel.end },
      )
      : clampClipSelection(
        sel.start, sel.end + delta, win.start, win.end,
        { move: 'out', fixedStart: sel.start },
      );
    commitSelection(res);
  }, [win, commitSelection]);

  // Editable H:M:S input for the clip length — CLIP-RELATIVE, like Twitch's
  // own editor: Start is always 00:00:00 (the clip begins at its own 0) and
  // End is the clip duration (e.g. 0:30). The absolute VOD position comes
  // from where the selection sits on the rail (see the VOD readout below).
  // Committing End pins the absolute start and enforces the 5..60s length,
  // capped by the window.
  const commitDurationInput = useCallback((relSec: number) => {
    const sel = selectionRef.current;
    const winLen = win.end - win.start;
    const minDur = Math.min(TWITCH_CLIP_MIN_SEC, winLen);
    const maxDur = Math.min(TWITCH_CLIP_MAX_SEC, winLen);
    const dur = Math.max(minDur, Math.min(relSec, maxDur));
    commitSelection({
      start: sel.start,
      end: Math.min(win.end, sel.start + dur),
    });
  }, [win, commitSelection]);

  // Same range, but driven in the OS browser by the VOD.RIP cookie extension
  // (clip_assist.mjs content script) — works with the plain browser-login
  // session cookie, no API clip scopes needed. When the
  // extension is not paired yet, run the app's auto-installer FIRST (it
  // stages the extension, restarts the browser, and the extension self-pairs
  // via /api/session/cookies), then open the editor once pairing settles.
  const createInBrowser = useCallback(async () => {
    const sel = selectionRef.current;
    const err = twitchClipDurationError(sel.end - sel.start);
    if (err) {
      showClipNotice('error', err);
      return;
    }
    try {
      const status = await apiGet<{
        paired: boolean;
        platforms?: { twitch?: { lastGrabAt?: string | null } };
      }>('/api/session/cookies/status');
      if (!status.paired) {
        // The bridge token can be empty while the extension is installed and
        // running (the pair lives in the extension's storage; the backend
        // re-pairs on the extension's next cookie push — its 10-min heartbeat).
        // The editor clip is published by the extension's content script using
        // the PAGE session cookie, so a de-paired-but-active extension still
        // completes the clip. Only run the auto-installer when the extension
        // is demonstrably inactive (no Twitch cookie push in ~11 min) — the
        // chrome://extensions window it opens is pure noise for an extension
        // that is already loaded.
        const lastGrab = status.platforms?.twitch?.lastGrabAt;
        const extActive =
          !!lastGrab && Date.now() - new Date(lastGrab).getTime() < 11 * 60_000;
        if (!extActive) {
          showClipNotice('error', t('Installing the VOD.RIP cookie extension — one moment…'));
          const inst = await apiPost<{ ok: boolean; started?: boolean; alreadyInstalled?: boolean }>(
            '/api/session/cookies/auto-install',
            {},
          );
          if (!inst.ok && !inst.started && !inst.alreadyInstalled) {
            showClipNotice('error', t('Could not install the cookie extension — open Settings → Cookies'));
            return;
          }
          // The extension's service worker pushes cookies on install/startup
          // and on a 10-minute alarm; give the re-pair room so a running-but-
          // quiet extension never dead-ends into "timed out".
          const deadline = Date.now() + 700_000;
          let paired = false;
          while (Date.now() < deadline && !paired) {
            await new Promise((r) => setTimeout(r, 2500));
            try {
              const st = await apiGet<{ paired: boolean; auto_install?: { state?: string } }>(
                '/api/session/cookies/status',
              );
              // The extension pairs the moment it boots — BEFORE the installer
              // reports done and closes its chrome://extensions window. Opening
              // the editor at 'paired' alone can land the tab inside that window
              // and lose it to the cleanup. Wait for the install to actually
              // finish (state leaves 'running') before opening.
              const installDone = !st.auto_install || st.auto_install.state !== 'running';
              paired = !!st.paired && installDone;
            } catch { /* backend mid-restart during the install — keep polling */ }
          }
          if (!paired) {
            showClipNotice('error', t('Extension install timed out — open Settings → Cookies'));
            return;
          }
          showClipNotice('ok', t('Cookie extension ready — opening Twitch…'));
        }
      }
    } catch {
      // status/install endpoints unreachable — open the editor anyway; the
      // page still loads (the extension just won't auto-publish).
    }
    reportClipEvent('create_clicked', {
      method: 'browser',
      startSec: sel.start,
      endSec: sel.end,
      durationSec: sel.end - sel.start,
      title: clipTitle.trim() || null,
    });
    openTwitchClipEditorInBrowser(vodId, broadcasterLogin, sel.start, sel.end, clipTitle.trim() || undefined, vodDurationSec);
    showClipNotice('ok', t('Opened in your browser — the VOD.RIP extension fills the editor and publishes'));
  }, [vodId, broadcasterLogin, clipTitle, showClipNotice]);

  const railView = useMemo(() => ({ start: win.start, end: win.end }), [win]);
  const playFrac = secToFrac(currentTime, railView) * 100;
  const selStartFrac = secToFrac(selection.start, railView) * 100;
  const selEndFrac = secToFrac(selection.end, railView) * 100;

  const createDisabled = windowTooShort
    || selLen < TWITCH_CLIP_MIN_SEC || selLen > TWITCH_CLIP_MAX_SEC;
  const createDisabledTitle = windowTooShort
    ? t('The {seconds}s window is too short to clip (min {min}s)', { seconds: Math.round(winLen), min: TWITCH_CLIP_MIN_SEC })
    : selLen > TWITCH_CLIP_MAX_SEC
      ? t('Trim the selection to {max}s or less', { max: TWITCH_CLIP_MAX_SEC })
      : selLen < TWITCH_CLIP_MIN_SEC
        ? t('Select at least {min}s', { min: TWITCH_CLIP_MIN_SEC })
        : t("Open Twitch's clip editor — {len}s ending at {time}", { len: Math.round(selLen), time: formatHmsFull(selection.end) });

  const handleHeaderMouseDown = useCallback((e: React.MouseEvent) => {
    const t = e.target as HTMLElement;
    if (t.closest('.twitch-clip-popup-close')) return;
    setDrag({ startX: e.clientX, startY: e.clientY, offsetX: posRef.current.x, offsetY: posRef.current.y });
  }, []);

  useEffect(() => {
    if (!drag) return;
    const onMove = (e: MouseEvent) => {
      setPosition({
        x: Math.max(0, Math.min(window.innerWidth - POPUP_W, drag.offsetX + e.clientX - drag.startX)),
        y: Math.max(0, Math.min(window.innerHeight - 320, drag.offsetY + e.clientY - drag.startY)),
      });
    };
    const onUp = () => setDrag(null);
    window.addEventListener('mousemove', onMove);
    window.addEventListener('mouseup', onUp);
    return () => {
      window.removeEventListener('mousemove', onMove);
      window.removeEventListener('mouseup', onUp);
    };
  }, [drag]);

  return createPortal(
    <div
      ref={popupRef}
      className="border-2 border-zinc-700 bg-zinc-950 flex flex-col"
      data-twitch-clip-popup
      style={{
        position: 'fixed',
        left: position.x,
        top: position.y,
        width: POPUP_W,
        zIndex,
        boxShadow: '6px 6px 0px 0px rgba(9,9,11,0.9)',
      }}
    >
      {/* Header — drag handle + close */}
      <div
        onMouseDown={handleHeaderMouseDown}
        className="flex items-center justify-between gap-2 px-2 py-1.5 bg-zinc-900 border-b-2 border-zinc-800 select-none shrink-0"
        style={{ cursor: drag ? 'grabbing' : 'grab' }}
      >
        <div className="flex items-center gap-1.5 min-w-0">
          <TwitchLogoIcon size={12} className="text-[#9146FF] shrink-0" />
          <div className="min-w-0">
            <span className="text-[8px] font-mono uppercase tracking-widest text-zinc-500 block">
              {t('Twitch clip')}
            </span>
            <p className="text-[10px] font-bold uppercase truncate text-zinc-200 leading-tight">
              {broadcasterLogin}
            </p>
          </div>
        </div>
        <button
          type="button"
          className="twitch-clip-popup-close text-zinc-500 hover:text-white p-1 shrink-0"
          onClick={onClose}
          title={t('Close')}
        >
          <X size={16} />
        </button>
      </div>

      {/* Video — click toggles play/pause */}
      <div className="relative bg-black shrink-0" style={{ aspectRatio: '16/9' }}>
        <div
          className="absolute inset-0 z-0 cursor-pointer"
          onClick={() => { if (!loading && !error) togglePlay(); }}
        >
          <video
            ref={videoRef}
            autoPlay
            playsInline
            muted={muted}
            style={{ width: '100%', height: '100%', objectFit: 'contain' }}
          />
        </div>
        {loading && (
          <div className="absolute inset-0 z-[1] flex items-center justify-center bg-black/60 text-zinc-300 text-xs font-mono pointer-events-none">
            <Loader2 size={28} className="animate-spin text-zinc-300 mr-2" />
            {t('Preparing clip window…')}
          </div>
        )}
        {error && (
          <div className="absolute inset-0 z-[1] flex flex-col items-center justify-center gap-2.5 bg-black/70 text-red-400 text-xs font-mono text-center px-4">
            <div>{error}</div>
            <button
              type="button"
              onClick={() => { setError(null); setLoading(true); setRetryTick((t) => t + 1); }}
              title={t('Retry the clip window preview')}
              className="flex items-center gap-1.5 text-[10px] font-black uppercase tracking-wider text-red-400 border-2 border-red-800 bg-red-950/30 px-2.5 py-1 hover:border-red-500 hover:text-red-300 cursor-pointer"
            >
              <RefreshCw size={12} />
              {t('Retry')}
            </button>
          </div>
        )}
        {!loading && !error && buffering && (
          <div className="absolute inset-0 z-[1] flex items-center justify-center bg-black/50 text-zinc-300 text-xs font-mono pointer-events-none">
            <Loader2 size={24} className="animate-spin text-zinc-200/90 mr-2" />
            {t('Buffering…')}
          </div>
        )}
        {/* Transport: play/pause + mute */}
        {!loading && !error && (
          <div
            className="absolute bottom-0 left-0 right-0 z-10 flex items-center gap-1.5 px-2 py-1.5 bg-gradient-to-t from-black/85 to-black/0"
          >
            <button
              type="button"
              onClick={togglePlay}
              disabled={!ready}
              className="flex items-center gap-1 border-2 border-zinc-600 bg-zinc-900/80 px-1.5 py-1 text-zinc-200 hover:border-white disabled:opacity-40 disabled:pointer-events-none"
              title={playing ? t('Pause') : t('Play')}
            >
              {playing ? <Pause size={13} /> : <Play size={13} />}
            </button>
            <button
              type="button"
              onClick={toggleMute}
              disabled={!ready}
              className="flex items-center gap-1 border-2 border-zinc-600 bg-zinc-900/80 px-1.5 py-1 text-zinc-200 hover:border-white disabled:opacity-40 disabled:pointer-events-none"
              title={muted ? t('Unmute') : t('Mute')}
            >
              {muted ? <VolumeX size={13} /> : <Volume2 size={13} />}
            </button>
            <span className="ml-auto text-[9px] font-mono text-zinc-400 tabular-nums">
              {formatHmsFull(currentTime)}
            </span>
          </div>
        )}
      </div>

      {/* Trim rail: 5..60s selection on the ±60s window timeline */}
      <div className="px-2 py-1.5 flex flex-col gap-1">
        <div className="flex items-center gap-2">
          <span className="text-[8px] font-mono uppercase w-9 shrink-0 tracking-wider text-zinc-600">
            {t('Range')}
          </span>
          <span className="flex items-center gap-1" title={t('Clip start — always 00:00:00 (clip time, like the Twitch editor)')}>
            <span className="text-[8px] font-mono uppercase tracking-wider text-zinc-500">{t('Start')}</span>
            <span className="text-[10px] font-bold text-zinc-500">00:00:00</span>
          </span>
          <span className="text-[9px] font-mono text-zinc-600">–</span>
          <span className="flex items-center gap-1" title={t('Clip length (clip time — the clip ends here)')}>
            <span className="text-[8px] font-mono uppercase tracking-wider text-zinc-500">{t('End')}</span>
            <EditableHmsTime
              valueSec={selection.end - selection.start}
              minSec={Math.min(TWITCH_CLIP_MIN_SEC, winLen)}
              maxSec={Math.min(TWITCH_CLIP_MAX_SEC, winLen)}
              onChange={commitDurationInput}
              className="text-[10px] font-bold text-[#9146FF]"
            />
          </span>
          <span className="ml-auto text-[8px] font-mono uppercase tracking-wider text-zinc-600">
            {t('H:M:S')}
          </span>
        </div>
        <div className="flex items-center gap-1 pl-9" title={t('Absolute VOD time of the selection (debug)')}>
          <span className="text-[7px] font-mono uppercase tracking-wider text-zinc-700">{t('VOD')}</span>
          <span className="text-[8px] font-mono text-zinc-600">
            {formatHmsFull(selection.start)} – {formatHmsFull(selection.end)}
          </span>
        </div>
        <div className="flex items-stretch gap-2">
          <span className="text-[8px] font-mono uppercase w-9 shrink-0 tracking-wider text-zinc-600 self-center">
            {t('Clip')}
          </span>
          <div
            ref={railRef}
            className="relative flex-1 h-6 bg-zinc-800/80 cursor-pointer"
            title={t('Drag handles to set the clip range (5–60s)')}
            onClick={(e) => {
              if (e.target !== e.currentTarget) return;
              const rail = railRef.current;
              if (!rail) return;
              const rect = rail.getBoundingClientRect();
              if (rect.width <= 0) return;
              const frac = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
              seekTo(fracToSec(frac, railView));
            }}
          >
            <div
              className="absolute top-1/2 -translate-y-1/2 h-1.5 bg-[#9146FF]/60 pointer-events-none"
              style={{
                left: `${selStartFrac}%`,
                width: `${Math.max(0, selEndFrac - selStartFrac)}%`,
              }}
            />
            <div
              className="absolute top-0 bottom-0 w-px bg-white/70 -translate-x-1/2 pointer-events-none z-[1]"
              style={{ left: `${playFrac}%` }}
            />
            <div
              role="slider"
              aria-label={t('Clip start')}
              aria-valuemin={win.start}
              aria-valuemax={win.end}
              aria-valuenow={selection.start}
              className="absolute top-0 bottom-0 w-2 -translate-x-1/2 z-[2] touch-none cursor-ew-resize bg-white border-x border-zinc-900"
              style={{ left: `${selStartFrac}%` }}
              onPointerDown={(e) => beginHandleDrag(e, 'in')}
            />
            <div
              role="slider"
              aria-label={t('Clip end')}
              aria-valuemin={win.start}
              aria-valuemax={win.end}
              aria-valuenow={selection.end}
              className="absolute top-0 bottom-0 w-2 -translate-x-1/2 z-[2] touch-none cursor-ew-resize bg-[#9146FF] border-x border-zinc-900"
              style={{ left: `${selEndFrac}%` }}
              onPointerDown={(e) => beginHandleDrag(e, 'out')}
            />
          </div>
          <ClipDurationAdjustButtons
            compact
            onAdjust={adjustSelection}
            activeEndpoint={lastEndpoint}
            disabled={windowTooShort}
          />
          <span
            className="text-[9px] font-mono w-11 shrink-0 text-right text-zinc-300 tabular-nums self-center"
            title={t('Selected clip length')}
          >
            {formatHmsFull(selLen)}
          </span>
        </div>
        <div className="flex items-center gap-2">
          <input
            type="text"
            value={clipTitle}
            onChange={(e) => setClipTitle(e.target.value)}
            maxLength={TWITCH_CLIP_TITLE_MAX}
            placeholder={t('Clip title (optional — used as the file name)')}
            aria-label={t('Clip title')}
            autoComplete="off"
            spellCheck={false}
            className="flex-1 min-w-0 bg-zinc-950 border-2 border-zinc-800 text-white px-2 py-1 text-[10px] font-mono focus:outline-none focus:border-[#9146FF]"
          />
        </div>
        <div className="flex items-center justify-between gap-2">
          <span className="text-[8px] font-mono text-zinc-600 tabular-nums">
            {t('window {start} – {end}', { start: formatHmsFull(win.start), end: formatHmsFull(win.end) })}
          </span>
          <div className="flex items-center gap-1.5">
            <button
              type="button"
              onClick={() => void createInBrowser()}
              disabled={createDisabled}
              className="flex items-center gap-1.5 border-2 border-[#9146FF] bg-[#9146FF]/20 px-2.5 py-1 text-[9px] font-bold uppercase tracking-wider text-white hover:bg-[#9146FF]/35 disabled:opacity-40 disabled:pointer-events-none"
              title={createDisabledTitle}
            >
              <TwitchLogoIcon size={12} />
              {t('Create clip')}
            </button>
          </div>
        </div>
        {clipNotice && (
          <div className={`flex items-center gap-1.5 text-[9px] font-mono uppercase tracking-wider ${
            clipNotice.kind === 'error' ? 'text-red-400' : 'text-[#53fc18]'
          }`}>
            <span className="truncate">{clipNotice.text}</span>
          </div>
        )}
      </div>
    </div>,
    document.body,
  );
}
