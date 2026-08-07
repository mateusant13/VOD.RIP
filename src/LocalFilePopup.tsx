import {
  useCallback,
  useLayoutEffect,
  useRef,
  useState,
  type PointerEvent as ReactPointerEvent,
} from 'react';
import { Search, X } from 'lucide-react';
import ArchiveSearchPopup from './components/ArchiveSearchPopup';
import { useI18n } from './i18n';
import type { ArchiveSearchHit, ArchiveVideoRow } from './archiveSearchUtils';
import type { PanelSize, SavedChannel } from './types';
import {
  EXPLORE_PANEL_DEFAULT_W,
  EXPLORE_VIDEO_ASPECT_DEFAULT,
  layoutExplorePopupWindow,
  startFloatingPanelDrag,
  PanelResizeHandles,
  type PanelPos,
  type ResizeEdge,
} from './explorePopupUtils';
import { panelPosAfterResize, startPanelResizeDrag } from './layoutUtils';
import { platformCardShadow, type PlatformStyleKey } from './platformStyles';
import PlatformVodIcon from './components/PlatformVodIcon';

/** Video-friendly resize floors — EXPLORE_PANEL_MIN_W (100) is too narrow to watch in. */
const LOCAL_PANEL_MIN_W = 260;
/**
 * The height floor must sit at or above the shared PANEL_MIN.h (180) that
 * startPanelResizeDrag applies internally — below it the caller's clamp is
 * dead code and the first frame of any east/west drag snaps the panel taller.
 */
const LOCAL_PANEL_MIN_H = 180;
/** Keep at least 32px of the popup on screen while resizing. */
const RESIZE_MARGIN = 32;
/**
 * Fixed chrome around the video area: p-3 (24) + border-2 (4) vertically and
 * horizontally, plus the header row + gap (measured ~56px tall). The default
 * height is derived from the width at the video's 16:9 so the video area
 * fills the panel instead of opening as a short letterboxed strip (the old
 * 149px default was also below the resize floors, so ANY handle drag snapped
 * the panel taller on the first pointermove).
 */
const LOCAL_PANEL_CHROME_W = 28;
const LOCAL_PANEL_CHROME_H = 56;

function platformKey(raw: string): PlatformStyleKey {
  const p = raw.toLowerCase();
  if (p === 'twitch') return 'twitch';
  if (p === 'youtube') return 'youtube';
  return 'kick';
}

export interface LocalFilePopupItem {
  id: string;
  filePath: string;
  title: string;
  platform: string;
}

type Props = {
  item: LocalFilePopupItem;
  zIndex: number;
  stackIndex: number;
  onClose: () => void;
  onBringToFront: () => void;
  /** Open an archive hit in the explore-player flow (App owns the popup stack). */
  onOpenHit: (hit: ArchiveSearchHit, video: ArchiveVideoRow | undefined) => void;
  /** Optional saved channels (App state) — unioned into the channel dropdown. */
  savedChannels?: SavedChannel[];
};

export default function LocalFilePopup({
  item,
  zIndex,
  stackIndex,
  onClose,
  onBringToFront,
  onOpenHit,
  savedChannels,
}: Props) {
  const { t } = useI18n();
  const containerRef = useRef<HTMLDivElement>(null);
  const posRef = useRef<PanelPos | null>(null);
  const [pos, setPos] = useState<PanelPos | null>(null);
  /** Docked archive-search panel (global search — local files have no archive identity). */
  const [searchOpen, setSearchOpen] = useState(false);
  const platform = platformKey(item.platform);
  const sizeRef = useRef<PanelSize>({
    w: EXPLORE_PANEL_DEFAULT_W,
    h: Math.round((EXPLORE_PANEL_DEFAULT_W - LOCAL_PANEL_CHROME_W) / EXPLORE_VIDEO_ASPECT_DEFAULT) + LOCAL_PANEL_CHROME_H,
  });
  const [size, setSize] = useState<PanelSize>(sizeRef.current);
  const src = `/api/local/media?path=${encodeURIComponent(item.filePath)}`;

  // Lay the popup out once on mount (bottom-right, staggered by stackIndex).
  // Must NOT re-run on resize — repositioning from the layout origin would
  // snap the panel back to the corner mid-drag.
  useLayoutEffect(() => {
    const el = containerRef.current;
    if (!el || posRef.current) return;
    layoutExplorePopupWindow(el, sizeRef.current.w, posRef, stackIndex);
    // layoutExplorePopupWindow clears the inline height; re-assert it (the
    // panel's height is state-driven now, not implicit from content).
    el.style.height = `${sizeRef.current.h}px`;
  }, [stackIndex]);

  const onDrag = (e: ReactPointerEvent<HTMLDivElement>) => {
    const el = containerRef.current;
    if (!el) return;
    if (!posRef.current) {
      posRef.current = layoutExplorePopupWindow(el, sizeRef.current.w, posRef, stackIndex);
      el.style.height = `${sizeRef.current.h}px`;
      setPos(posRef.current);
    }
    startFloatingPanelDrag(e, posRef, setPos, el);
  };

  // --- Resize (same [data-panel-resize] pattern as the live player popup) ---
  const handleResize = useCallback((e: ReactPointerEvent<HTMLDivElement>, edge: ResizeEdge) => {
    const startSize = { ...sizeRef.current };
    // The mount layout effect always seeds posRef before any pointer event.
    const startPos = posRef.current ?? { x: 0, y: 0 };
    const viewport = { w: window.innerWidth, h: window.innerHeight };
    const applyPos = (next: PanelSize) => {
      const p = panelPosAfterResize(edge, startPos, startSize, next, viewport);
      posRef.current = p;
      setPos(p);
    };
    startPanelResizeDrag(e, edge, sizeRef, setSize, {
      panelEl: containerRef.current,
      maxW: viewport.w - RESIZE_MARGIN,
      maxH: viewport.h - RESIZE_MARGIN,
      clampSize: (s) => ({
        w: Math.min(viewport.w - RESIZE_MARGIN, Math.max(LOCAL_PANEL_MIN_W, s.w)),
        h: Math.min(viewport.h - RESIZE_MARGIN, Math.max(LOCAL_PANEL_MIN_H, s.h)),
      }),
      onResizeMove: (next) => applyPos(next),
      onResizeEnd: () => applyPos(sizeRef.current),
    });
  }, []);

  // left/top come from state once a drag/resize has run (mount layout and
  // mid-drag writes set them imperatively); before that, keep them out of
  // the style prop so React never overwrites the imperative values.
  return (
    <div
      ref={containerRef}
      className={`fixed flex flex-col gap-2 bg-zinc-950 border-2 border-white p-3 select-none ${platformCardShadow(platform)}`}
      style={{ zIndex, width: size.w, height: size.h, ...(pos ? { left: pos.x, top: pos.y } : {}) }}
      onPointerDownCapture={onBringToFront}
    >
      <PanelResizeHandles onPointerDown={handleResize} />

      <div
        className="flex items-center gap-2 cursor-grab active:cursor-grabbing min-w-0"
        onPointerDown={onDrag}
      >
        <PlatformVodIcon platform={item.platform} className="w-4 h-4 shrink-0" />
        <span className="flex-1 min-w-0 text-[11px] font-mono text-zinc-200 truncate">
          {item.title || item.filePath.split(/[/\\]/).pop()}
        </span>
        <button
          type="button"
          title={t('Search the local archive (transcripts + chat)')}
          onClick={() => setSearchOpen((o) => !o)}
          aria-pressed={searchOpen}
          className={`shrink-0 p-0.5 ${searchOpen ? 'text-white' : 'text-zinc-500 hover:text-white'}`}
        >
          <Search size={14} />
        </button>
        <button
          type="button"
          title={t('Close')}
          onClick={onClose}
          className="shrink-0 text-zinc-500 hover:text-white p-0.5"
        >
          <X size={14} />
        </button>
      </div>
      <div className="relative flex-1 min-h-0">
        <video
          key={src}
          src={src}
          controls
          autoPlay
          playsInline
          className="w-full h-full bg-black border border-zinc-800"
        />
        {searchOpen && (
          <div className="absolute inset-0 z-10 bg-zinc-950 border border-zinc-800 flex flex-col">
            <ArchiveSearchPopup
              embedded
              zIndex={0}
              onClose={() => setSearchOpen(false)}
              onOpenHit={onOpenHit}
              savedChannels={savedChannels}
            />
          </div>
        )}
      </div>
    </div>
  );
}
