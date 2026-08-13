/**
 * Instant-preview overlay lifecycle for one preview surface (main preview in
 * App.tsx, mini preview in ChannelExplorePopup.tsx).
 *
 * While the remote session boots, a matched local 6s clip covers the surface
 * and plays instantly (muted autoplay). The overlay disappears on the first
 * of:
 *  - remote session ready  -> handoff: the normal pipeline owns the surface
 *  - clip ended / error    -> honest fallback to the normal loading state
 *  - surface closed        -> nothing (state reset; next open re-arms)
 *
 * The remote boot flow is never touched — this hook only adds an overlay that
 * is unmounted before any of the existing gating logic (spinner, controls,
 * seeks) matters.
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import { findInstantPreview, type InstantPreviewEntry } from '../instantPreview';

/** The backend clips hold the first ~6s of each VOD. */
export const INSTANT_CLIP_SEC = 6;

export interface UseInstantPreviewOptions {
  /** Opened VOD URL (the surface's current media URL). */
  url: string;
  /** True while the preview surface is open/mounted. */
  active: boolean;
  /** True once the remote session is ready (handoff point). */
  remoteReady: boolean;
  /** Preview start position in seconds — only engage inside the clip window. */
  startSec: number;
}

export interface UseInstantPreviewResult {
  /** Matched preview entry (also enables a discreet badge if a caller wants one). */
  matched: InstantPreviewEntry | null;
  /** True while the instant clip should cover the surface. */
  show: boolean;
  /** Ref to attach to the overlay <video>. */
  videoRef: React.RefObject<HTMLVideoElement | null>;
  /** Wire to the overlay video's onEnded. */
  onOverlayEnded: () => void;
  /** Wire to the overlay video's onError. */
  onOverlayError: () => void;
}

export function useInstantPreview(opts: UseInstantPreviewOptions): UseInstantPreviewResult {
  const { url, active, remoteReady, startSec } = opts;
  const [matched, setMatched] = useState<InstantPreviewEntry | null>(null);
  const [show, setShow] = useState(false);
  const videoRef = useRef<HTMLVideoElement | null>(null);
  // URL the current overlay (if any) was armed for. Re-arms only on a NEW
  // media — an ended clip stays ended through retries/trim drags on the same URL.
  const armedUrlRef = useRef<string>('');

  useEffect(() => {
    if (!active) {
      armedUrlRef.current = '';
      setMatched(null);
      setShow(false);
      return;
    }
    if (remoteReady) {
      // Handoff — the remote session owns the surface now.
      setShow(false);
      return;
    }
    if (startSec >= INSTANT_CLIP_SEC) {
      // Trim window starts past the clip — normal flow only.
      return;
    }
    const m = findInstantPreview(url);
    if (!m) return;
    if (armedUrlRef.current !== url) {
      armedUrlRef.current = url;
      setMatched(m);
      setShow(true);
    }
  }, [active, url, remoteReady, startSec]);

  const onOverlayEnded = useCallback(() => setShow(false), []);
  const onOverlayError = useCallback(() => setShow(false), []);

  return { matched, show, videoRef, onOverlayEnded, onOverlayError };
}
