import { Settings } from 'lucide-react';
import type { PreviewLevelOption } from './previewPlayerUtils';

interface PreviewQualityMenuProps {
  levels: PreviewLevelOption[];
  currentLevel: number;
  menuOpen: boolean;
  setMenuOpen: (open: boolean | ((prev: boolean) => boolean)) => void;
  onSelect: (levelIndex: number) => void;
  disabled: boolean;
  buttonClassName: string;
  onMenuOpen?: () => void;
  popoverClassName?: string;
  /** Fullscreen controls sit above a trim rail — open downward to avoid clipping. */
  popoverPlacement?: 'up' | 'down';
}

export default function PreviewQualityMenu({
  levels,
  currentLevel,
  menuOpen,
  setMenuOpen,
  onSelect,
  disabled,
  buttonClassName,
  onMenuOpen,
  popoverClassName = 'border-2 border-zinc-600 bg-zinc-950',
  popoverPlacement = 'up',
}: PreviewQualityMenuProps) {
  if (!levels.length) return null;

  const popoverPos = popoverPlacement === 'down'
    ? 'absolute top-full left-0 mt-1'
    : 'absolute bottom-full left-0 mb-1';

  return (
    <div className="relative" data-player-menu>
      <button
        type="button"
        onClick={(e) => {
          e.stopPropagation();
          onMenuOpen?.();
          setMenuOpen((o) => !o);
        }}
        disabled={disabled}
        className={buttonClassName}
        title="Video quality"
      >
        <Settings size={15} />
      </button>
      {menuOpen && (
        <div className={`${popoverPos} z-[100] min-w-[7rem] shadow-lg py-1 ${popoverClassName}`}>
          {levels.map((l) => (
            <button
              key={l.index}
              type="button"
              onClick={() => onSelect(l.index)}
              className={`block w-full text-left px-2 py-1 text-[10px] font-mono hover:bg-zinc-800 ${
                l.index === currentLevel ? 'text-white' : 'text-zinc-400'
              }`}
            >
              {l.label}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
