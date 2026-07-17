/**
 * Direct MSE fMP4 push player for window-HLS preview.
 * 
 * Bypasses hls.js entirely — fetches init.mp4 + segment .m4s files
 * and pushes them directly to a MediaSource SourceBuffer.
 * 
 * Advantages:
 * - No hls.js Web Worker overhead
 * - No TS→fMP4 transmux step (segments are already fMP4/CMAF)
 * - Sub-100ms segment append latency
 * - Direct seek control via SourceBuffer.remove() + appendBuffer()
 * 
 * Trade-offs:
 * - Single quality only (window-HLS is fixed tier)
 * - No ABR (by design — preview is fixed quality)
 * - Requires browser MSE support (all modern browsers)
 * 
 * Usage:
 *   const { attach, seek, destroy } = useDirectMSEPlayer(sessionId, videoRef);
 *   await attach(); // loads init + first segments
 *   // on seek: await seek(newTime);
 */
import { useCallback, useRef, useState, useEffect } from 'react';
import type { ReactElement } from 'react';

interface DirectMSEPlayerState {
  ready: boolean;
  error: string | null;
  bufferedEnd: number;
}

interface DirectMSEPlayerActions {
  attach: (sessionId: string) => Promise<void>;
  seek: (time: number) => Promise<void>;
  destroy: () => void;
  getBufferedRange: () => { start: number; end: number } | null;
}

const SEGMENT_DURATION = 4; // seconds, matches WINDOW_HLS_SEGMENT_SEC
const INIT_SEGMENT_RESOURCE = 'window-init';
const SEGMENT_RESOURCE_PREFIX = 'window-seg-';
const MAX_BUFFER_AHEAD = 30; // seconds to keep buffered ahead
const MAX_BUFFER_BEHIND = 10; // seconds to keep behind playhead

function segmentIndexAtTime(time: number): number {
  return Math.floor(time / SEGMENT_DURATION);
}

function segmentTimeRange(index: number): { start: number; end: number } {
  return {
    start: index * SEGMENT_DURATION,
    end: (index + 1) * SEGMENT_DURATION,
  };
}

async function fetchSegment(
  sessionId: string,
  resourceId: string,
  signal: AbortSignal
): Promise<Uint8Array> {
  const res = await fetch(`/api/preview/hls/${sessionId}/resource?id=${resourceId}`, {
    signal,
    cache: 'no-cache',
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(err.detail || `HTTP ${res.status}`);
  }
  return new Uint8Array(await res.arrayBuffer());
}

export function useDirectMSEPlayer(
  videoRef: React.RefObject<HTMLVideoElement | null>
): DirectMSEPlayerState & DirectMSEPlayerActions {
  const [ready, setReady] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [bufferedEnd, setBufferedEnd] = useState(0);

  const mediaSourceRef = useRef<MediaSource | null>(null);
  const sourceBufferRef = useRef<SourceBuffer | null>(null);
  const sessionIdRef = useRef<string | null>(null);
  const initSegmentRef = useRef<Uint8Array | null>(null);
  const appendedSegmentsRef = useRef<Set<number>>(new Set());
  const appendingRef = useRef(false);
  const appendQueueRef = useRef<Array<{ segmentIndex: number; data: Uint8Array }>>([]);
  const pendingSeekRef = useRef<number | null>(null);
  const abortControllerRef = useRef<AbortController | null>(null);

  // Codec string for fMP4 (H.264 + AAC)
  const MIME_TYPE = 'video/mp4; codecs="avc1.4d401e,mp4a.40.2"';

  // Process append queue
  const processAppendQueue = useCallback(async () => {
    if (appendingRef.current || appendQueueRef.current.length === 0) return;
    if (!sourceBufferRef.current || sourceBufferRef.current.updating) return;

    appendingRef.current = true;
    const sb = sourceBufferRef.current;

    while (appendQueueRef.current.length > 0 && !sb.updating) {
      const { segmentIndex, data } = appendQueueRef.current.shift()!;
      try {
        sb.appendBuffer(data.buffer, data.byteOffset, data.byteLength);
        appendedSegmentsRef.current.add(segmentIndex);
      } catch (e) {
        console.error('[MSE] appendBuffer failed:', e);
        // Re-queue on QuotaExceededError
        if (e instanceof DOMException && e.name === 'QuotaExceededError') {
          appendQueueRef.current.unshift({ segmentIndex, data });
          // Evict old segments and retry
          await evictOldSegments(sb);
        }
      }
    }

    appendingRef.current = false;
  }, []);

  // Evict segments behind playhead to free buffer space
  const evictOldSegments = useCallback(async (sb: SourceBuffer, playhead = 0) => {
    const evictBefore = playhead - MAX_BUFFER_BEHIND;
    if (evictBefore <= 0) return;

    for (const idx of appendedSegmentsRef.current) {
      const { start, end } = segmentTimeRange(idx);
      if (end <= evictBefore) {
        try {
          sb.remove(start, end);
          appendedSegmentsRef.current.delete(idx);
        } catch (e) {
          console.warn('[MSE] remove failed:', e);
        }
      }
    }
  }, []);

  // Evict segments ahead of buffer window
  const evictAheadSegments = useCallback(async (sb: SourceBuffer, playhead: number) => {
    const evictAfter = playhead + MAX_BUFFER_AHEAD;
    for (const idx of appendedSegmentsRef.current) {
      const { start } = segmentTimeRange(idx);
      if (start >= evictAfter) {
        try {
          sb.remove(start, start + SEGMENT_DURATION);
          appendedSegmentsRef.current.delete(idx);
        } catch (e) {
          console.warn('[MSE] remove ahead failed:', e);
        }
      }
    }
  }, []);

  // Fetch and queue segment
  const fetchAndQueueSegment = useCallback(async (
    sessionId: string,
    segmentIndex: number
  ) => {
    if (appendedSegmentsRef.current.has(segmentIndex)) return;
    if (appendQueueRef.current.some(q => q.segmentIndex === segmentIndex)) return;

    const resourceId = `${SEGMENT_RESOURCE_PREFIX}${segmentIndex.toString().padStart(3, '0')}`;
    try {
      const data = await fetchSegment(sessionId, resourceId, abortControllerRef.current!.signal);
      appendQueueRef.current.push({ segmentIndex, data });
      appendQueueRef.current.sort((a: { segmentIndex: number }, b: { segmentIndex: number }) => a.segmentIndex - b.segmentIndex);
      processAppendQueue();
    } catch (e) {
      console.error(`[MSE] fetch segment ${segmentIndex} failed:`, e);
    }
  }, [processAppendQueue]);

  // Prefetch next N segments
  const prefetchSegments = useCallback(async (
    sessionId: string,
    fromIndex: number,
    count: number = 3
  ) => {
    await Promise.all(
      Array.from({ length: count }, (_, i) => fetchAndQueueSegment(sessionId, fromIndex + i))
    );
  }, [fetchAndQueueSegment]);

  // Main attach: init MediaSource, fetch init segment, start buffering
  const attach = useCallback(async (sessionId: string) => {
    const video = videoRef.current;
    if (!video) {
      setError('No video element');
      return;
    }

    // Clean up any previous session
    destroy();

    sessionIdRef.current = sessionId;
    abortControllerRef.current = new AbortController();
    const signal = abortControllerRef.current.signal;

    try {
      setError(null);

      // 1. Create MediaSource
      const ms = new MediaSource();
      mediaSourceRef.current = ms;
      video.src = URL.createObjectURL(ms);

      await new Promise<void>((resolve, reject) => {
        ms.addEventListener('sourceopen', () => resolve(), { once: true });
        ms.addEventListener('sourceended', () => reject(new Error('MediaSource ended')), { once: true });
      });

      // 2. Add SourceBuffer
      const sb = ms.addSourceBuffer(MIME_TYPE);
      sourceBufferRef.current = sb;

      sb.addEventListener('updateend', () => {
        processAppendQueue();
        // Update buffered end
        if (sb.buffered.length > 0) {
          setBufferedEnd(sb.buffered.end(sb.buffered.length - 1));
        }
      });

      sb.addEventListener('error', (e) => {
        console.error('[MSE] SourceBuffer error:', e);
        setError('SourceBuffer error');
      });

      // 3. Fetch init segment
      const initData = await fetchSegment(sessionId, INIT_SEGMENT_RESOURCE, signal);
      initSegmentRef.current = initData;

      // 4. Append init segment
      sb.appendBuffer(initData.buffer, initData.byteOffset, initData.byteLength);
      await new Promise<void>((resolve, reject) => {
        const onUpdateEnd = () => {
          sb.removeEventListener('updateend', onUpdateEnd);
          resolve();
        };
        const onError = () => {
          sb.removeEventListener('error', onError);
          reject(new Error('Init segment append failed'));
        };
        sb.addEventListener('updateend', onUpdateEnd, { once: true });
        sb.addEventListener('error', onError, { once: true });
      });

      // 5. Fetch and append first 3 segments (0, 1, 2)
      await prefetchSegments(sessionId, 0, 3);

      setReady(true);
      console.log('[MSE] Player attached, init + first segments queued');
    } catch (e) {
      const msg = e instanceof Error ? e.message : 'Attach failed';
      console.error('[MSE] attach failed:', e);
      setError(msg);
      destroy();
      throw e;
    }
  }, [videoRef, fetchAndQueueSegment, prefetchSegments, processAppendQueue]);

  // Seek to new time
  const seek = useCallback(async (time: number) => {
    const sb = sourceBufferRef.current;
    const ms = mediaSourceRef.current;
    const sessionId = sessionIdRef.current;
    if (!sb || !ms || !sessionId) {
      console.warn('[MSE] seek called but not attached');
      return;
    }

    // Clamp to valid range
    const targetTime = Math.max(0, time);
    const targetIndex = segmentIndexAtTime(targetTime);

    console.log(`[MSE] seek to ${targetTime.toFixed(2)}s (segment ${targetIndex})`);

    // If we already have this segment buffered, just let video seek natively
    const { start: segStart, end: segEnd } = segmentTimeRange(targetIndex);
    if (sb.buffered.length > 0) {
      for (let i = 0; i < sb.buffered.length; i++) {
        const bufStart = sb.buffered.start(i);
        const bufEnd = sb.buffered.end(i);
        if (bufStart <= targetTime && targetTime < bufEnd) {
          // Already buffered — native seek will work
          console.log('[MSE] target already buffered, native seek');
          return;
        }
      }
    }

    // Need to load new segment(s)
    pendingSeekRef.current = targetTime;

    try {
      // 1. Abort any in-flight fetches
      abortControllerRef.current?.abort();
      abortControllerRef.current = new AbortController();

      // 2. Remove buffered ranges around target (keep small window)
      const removeStart = Math.max(0, targetTime - 2);
      const removeEnd = targetTime + MAX_BUFFER_AHEAD;
      
      // Remove in chunks to avoid QuotaExceededError during append
      for (let i = 0; i < sb.buffered.length; i++) {
        const bs = sb.buffered.start(i);
        const be = sb.buffered.end(i);
        if (be > removeStart && bs < removeEnd) {
          const rStart = Math.max(bs, removeStart);
          const rEnd = Math.min(be, removeEnd);
          if (rEnd - rStart > 0.1) {
            sb.remove(rStart, rEnd);
          }
        }
      }

      // Clear appended segments that were removed
      for (const idx of appendedSegmentsRef.current) {
        const { start, end } = segmentTimeRange(idx);
        if (end > removeStart && start < removeEnd) {
          appendedSegmentsRef.current.delete(idx);
        }
      }

      // 3. Clear append queue
      appendQueueRef.current = [];

      // 4. Re-append init segment (required after remove())
      if (initSegmentRef.current) {
        sb.appendBuffer(
          initSegmentRef.current.buffer,
          initSegmentRef.current.byteOffset,
          initSegmentRef.current.byteLength
        );
        await new Promise<void>(resolve => {
          const onUpdateEnd = () => {
            sb.removeEventListener('updateend', onUpdateEnd);
            resolve();
          };
          sb.addEventListener('updateend', onUpdateEnd, { once: true });
        });
      }

      // 5. Fetch and append target segment + next 2
      await prefetchSegments(sessionId, targetIndex, 3);

      // 6. Wait for target segment to be appended
      const waitForSegment = async (index: number, timeout = 5000) => {
        const start = Date.now();
        while (!appendedSegmentsRef.current.has(index)) {
          if (Date.now() - start > timeout) throw new Error('Segment append timeout');
          await new Promise(r => setTimeout(r, 50));
        }
      };
      await waitForSegment(targetIndex);

      pendingSeekRef.current = null;
      console.log('[MSE] seek complete');
    } catch (e) {
      pendingSeekRef.current = null;
      console.error('[MSE] seek failed:', e);
      setError(e instanceof Error ? e.message : 'Seek failed');
      throw e;
    }
  }, [fetchAndQueueSegment, prefetchSegments]);

  // Destroy player
  const destroy = useCallback(() => {
    abortControllerRef.current?.abort();
    abortControllerRef.current = null;

    if (sourceBufferRef.current) {
      try {
        sourceBufferRef.current.abort();
      } catch {}
      sourceBufferRef.current = null;
    }

    if (mediaSourceRef.current) {
      try {
        mediaSourceRef.current.endOfStream();
      } catch {}
      mediaSourceRef.current = null;
    }

    if (videoRef.current && videoRef.current.src) {
      URL.revokeObjectURL(videoRef.current.src);
      videoRef.current.src = '';
    }

    initSegmentRef.current = null;
    appendedSegmentsRef.current.clear();
    appendQueueRef.current = [];
    appendingRef.current = false;
    sessionIdRef.current = null;
    pendingSeekRef.current = null;
    setReady(false);
    setBufferedEnd(0);
  }, [videoRef]);

  // Get buffered range
  const getBufferedRange = useCallback(() => {
    const sb = sourceBufferRef.current;
    if (!sb || sb.buffered.length === 0) return null;
    return {
      start: sb.buffered.start(0),
      end: sb.buffered.end(sb.buffered.length - 1),
    };
  }, []);

  // Cleanup on unmount
  useEffect(() => {
    return () => destroy();
  }, [destroy]);

  return {
    ready,
    error,
    bufferedEnd,
    attach,
    seek,
    destroy,
    getBufferedRange,
  };
}

export default useDirectMSEPlayer;