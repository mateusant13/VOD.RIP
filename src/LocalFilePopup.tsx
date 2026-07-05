import {
  useLayoutEffect,
  useRef,
  useState,
  type PointerEvent as ReactPointerEvent,
} from 'react';
import { X } from 'lucide-react';
import {
  EXPLORE_PANEL_DEFAULT_W,
  EXPLORE_PANEL_CHROME_H_EST,
  EXPLORE_VIDEO_ASPECT_DEFAULT,
  layoutExplorePopupWindow,
  startFloatingPanelDrag,
  type PanelPos,
} from './explorePopupUtils';
import { platformCardShadow, type PlatformStyleKey } from './platformStyles';
import PlatformVodIcon from './components/PlatformVodIcon';

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
};

export default function LocalFilePopup({
  item,
  zIndex,
  stackIndex,
  onClose,
  onBringToFront,
}: Props) {
  const containerRef = useRef<HTMLDivElement>(null);
  const posRef = useRef<PanelPos | null>(null);
  const [, setPos] = useState<PanelPos | null>(null);
  const platform = platformKey(item.platform);
  const panelWidth = EXPLORE_PANEL_DEFAULT_W;
  const videoH = Math.round((panelWidth - 24) / EXPLORE_VIDEO_ASPECT_DEFAULT);
  const panelH = videoH + EXPLORE_PANEL_CHROME_H_EST;
  const src = `/api/local/media?path=${encodeURIComponent(item.filePath)}`;

  useLayoutEffect(() => {
    if (!containerRef.current) return;
    layoutExplorePopupWindow(containerRef.current, panelWidth, posRef, stackIndex);
  }, [panelWidth, stackIndex]);

  const onDrag = (e: ReactPointerEvent<HTMLDivElement>) => {
    const el = containerRef.current;
    if (!el) return;
    if (!posRef.current) {
      posRef.current = layoutExplorePopupWindow(el, panelWidth, posRef, stackIndex);
      setPos(posRef.current);
    }
    startFloatingPanelDrag(e, posRef, setPos, el);
  };

  return (
    <div
      ref={containerRef}
      className={`fixed flex flex-col gap-2 bg-zinc-950 border-2 border-white p-3 select-none ${platformCardShadow(platform)}`}
      style={{ zIndex, width: panelWidth }}
      onPointerDownCapture={onBringToFront}
    >
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
          title="Close"
          onClick={onClose}
          className="shrink-0 text-zinc-500 hover:text-white p-0.5"
        >
          <X size={14} />
        </button>
      </div>
      <video
        key={src}
        src={src}
        controls
        autoPlay
        playsInline
        className="w-full bg-black border border-zinc-800"
        style={{ height: videoH, maxHeight: `calc(100vh - ${panelH}px)` }}
      />
    </div>
  );
}
