import { memo } from 'react';
import { useI18n } from '../i18n';
import { formatHmsFull } from '../utils';
import { usePreviewTime } from '../hooks/usePreviewTime';
import ClipDurationAdjustButtons from './ClipDurationAdjustButtons';
import { secToFrac, fracToSec, type TrimViewWindow } from '../trimUtils';

/**
 * Live preview timeline controls (rail + needles + playhead + scrubber + trim
 * panel resizer). Owns the ~4 Hz timeupdate ticks: it subscribes to the
 * preview-time external store, so App no longer re-renders on every tick.
 * The trim window, endpoints, panel height, and the resizer refs/setters stay
 * owned by App and arrive as props — only the JSX that reads the live playhead
 * lives here.
 *
 * ponytail: both this bar and the preview transport re-render on App-level
 * changes today; the upgrade path is to lift the whole preview
 * transport/player controls into a single child later.
 */
const PreviewTimelineBar = memo(function PreviewTimelineBar({
  trimView,
  trimStart,
  trimEnd,
  vodDurationSec,
  clipLengthSec,
  zoom,
  fullscreen,
  videoReady,
  panelHeight,
  adjustEndpoint,
  needleEndpoints,
  panelResizeRef,
  panelDragRef,
  onSeek,
  onAdjust,
  onNeedleDrag,
  setPanelHeight,
  bumpPreviewFsControls,
}: {
  trimView: TrimViewWindow;
  trimStart: number;
  trimEnd: number;
  vodDurationSec: number;
  clipLengthSec: number;
  /** Trim rail zoom — only used for the ×N badge. */
  zoom: number;
  fullscreen: boolean;
  videoReady: boolean;
  /** Trim panel container height (0 → auto). Applied to the root row. */
  panelHeight: number;
  /** Active trim endpoint for the ±duration adjust buttons. */
  adjustEndpoint: 'in' | 'out';
  /** RefObjects forwarded so App's imperative playhead write + drag handlers
   *  (which keep App refs/state) keep working. */
  needleEndpoints: {
    rail: React.RefObject<HTMLDivElement | null>;
    playhead: React.RefObject<HTMLDivElement | null>;
  };
  /** App-owned resize drag state (trimPanelResizeRef / trimDragActiveRef). */
  panelResizeRef: { current: { startY: number; startHeight: number } | null };
  panelDragRef: { current: boolean };
  onSeek: (sec: number) => void;
  onAdjust: (delta: number) => void;
  onNeedleDrag: (e: React.PointerEvent<HTMLElement>, which: 'in' | 'out') => void;
  setPanelHeight: (h: number) => void;
  bumpPreviewFsControls: () => void;
}) {
  const { t } = useI18n();
  const previewTime = usePreviewTime();

  let startPct = 0;
  let endPct = 0;
  let playPct = 0;
  if (vodDurationSec > 0) {
    startPct = secToFrac(trimStart, trimView) * 100;
    endPct = secToFrac(trimEnd, trimView) * 100;
    playPct = secToFrac(previewTime, trimView) * 100;
  }

  return (
    <div
      className="flex flex-col gap-0.5 w-full"
      style={panelHeight > 0 ? { height: panelHeight + 'px' } : undefined}
    >
      {vodDurationSec > 0 && (
        <div className="flex items-stretch gap-2 flex-1 min-h-0">
          <span
            className={`text-[8px] font-mono uppercase w-11 shrink-0 tracking-wider self-center ${
              fullscreen ? 'text-zinc-400' : 'text-zinc-600'
            }`}
          >
            Clip
            {zoom > 1 && (
              <span
                className="block text-[7px] text-zinc-500"
                title={t('Scroll on the rail to zoom')}
              >
                ×{zoom >= 10 ? Math.round(zoom) : zoom.toFixed(1)}
              </span>
            )}
          </span>
          <div
            ref={needleEndpoints.rail}
            className={`preview-needle-rail relative flex-1 ${
              fullscreen ? 'bg-white/10' : 'bg-zinc-800/80'
            }`}
            title={t('Drag needles to set preview clip range')}
            onClick={(e) => {
              if (e.target !== e.currentTarget) return;
              const rail = needleEndpoints.rail.current;
              if (!rail || vodDurationSec <= 0) return;
              const rect = rail.getBoundingClientRect();
              const frac = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
              onSeek(fracToSec(frac, trimView));
            }}
          >
            <div
              className="preview-needle-region absolute top-1/2 -translate-y-1/2 h-1 pointer-events-none"
              style={{
                left: `${startPct}%`,
                width: `${Math.max(0, endPct - startPct)}%`,
              }}
            />
            <div
              ref={needleEndpoints.playhead}
              className="preview-needle-playhead absolute top-0 bottom-0 w-px bg-white/50 -translate-x-1/2 pointer-events-none z-[1]"
              style={{ left: `${playPct}%` }}
            />
            <div
              role="slider"
              aria-label={t('Clip in')}
              aria-valuemin={0}
              aria-valuemax={vodDurationSec}
              aria-valuenow={trimStart}
              className="preview-needle preview-needle-in absolute top-0 bottom-0 -translate-x-1/2 z-[2] touch-none cursor-ew-resize"
              style={{ left: `${startPct}%` }}
              onPointerDown={(e) => onNeedleDrag(e, 'in')}
            />
            <div
              role="slider"
              aria-label={t('Clip out')}
              aria-valuemin={0}
              aria-valuemax={vodDurationSec}
              aria-valuenow={trimEnd}
              className="preview-needle preview-needle-out absolute top-0 bottom-0 -translate-x-1/2 z-[2] touch-none cursor-ew-resize"
              style={{ left: `${endPct}%` }}
              onPointerDown={(e) => onNeedleDrag(e, 'out')}
            />
          </div>
          <ClipDurationAdjustButtons
            compact
            onAdjust={onAdjust}
            activeEndpoint={adjustEndpoint}
            disabled={vodDurationSec <= 0 || trimEnd <= trimStart}
          />
          <span
            className={`text-[8px] font-mono w-11 shrink-0 text-right ${
              fullscreen ? 'text-zinc-300/90' : 'text-zinc-500'
            }`}
            title={t('Selected clip length')}
          >
            {formatHmsFull(clipLengthSec)}
          </span>
        </div>
      )}
      {vodDurationSec > 0 && (
        <div
          className="h-2 cursor-ns-resize flex items-center justify-center gap-1 select-none shrink-0 hover:bg-zinc-800/50 rounded"
          onMouseMove={fullscreen ? bumpPreviewFsControls : undefined}
          onPointerDown={(e) => {
            e.preventDefault();
            e.currentTarget.setPointerCapture(e.pointerId);
            panelResizeRef.current = { startY: e.clientY, startHeight: panelHeight };
            panelDragRef.current = true;
            bumpPreviewFsControls();
          }}
          onPointerMove={(e) => {
            if (!panelResizeRef.current) return;
            const startY = panelResizeRef.current.startY;
            const startH = panelResizeRef.current.startHeight;
            const delta = e.clientY - startY;
            const minH = fullscreen ? 60 : 40;
            const maxH = fullscreen ? Math.floor(window.innerHeight * 0.5) : Infinity;
            const h = Math.min(maxH, Math.max(minH, startH - delta));
            setPanelHeight(h);
          }}
          onPointerUp={(e) => {
            panelResizeRef.current = null;
            panelDragRef.current = false;
            try { e.currentTarget.releasePointerCapture(e.pointerId); } catch {}
          }}
          onPointerCancel={(e) => {
            panelResizeRef.current = null;
            panelDragRef.current = false;
            try { e.currentTarget.releasePointerCapture(e.pointerId); } catch {}
          }}
        >
          <span className="w-8 h-0.5 rounded-full bg-zinc-600" />
        </div>
      )}
      <div className="flex items-center gap-2">
        <span
          className={`text-[9px] font-mono w-11 shrink-0 ${fullscreen ? 'text-zinc-300/90' : 'text-zinc-400'}`}
        >
          {formatHmsFull(Math.max(0, previewTime - trimStart))}
        </span>
        <input
          type="range"
          min={trimStart}
          max={trimEnd}
          step={0.25}
          value={Math.min(Math.max(previewTime, trimStart), trimEnd)}
          disabled={!videoReady || clipLengthSec <= 0}
          onChange={(e) => onSeek(parseFloat(e.target.value))}
          className="flex-1 accent-white disabled:opacity-40"
        />
        <span
          className={`text-[9px] font-mono w-11 shrink-0 text-right ${fullscreen ? 'text-zinc-400/80' : 'text-zinc-500'}`}
          title={t('Selected clip length')}
        >
          {formatHmsFull(clipLengthSec)}
        </span>
      </div>
    </div>
  );
});
export default PreviewTimelineBar;
