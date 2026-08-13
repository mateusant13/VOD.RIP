/** Compact info affordance: hover shows the description as an immediate
 *  tooltip; click pins it as a small popover (closes on re-click, outside
 *  click, or Esc). No native title — the in-DOM box is the only tooltip, and
 *  it flips/clamps inside both the viewport and its paint-contained scroll
 *  container (.custom-scrollbar clips absolutely-positioned descendants), so
 *  it never overflows or gets cut off.
 *  ponytail: no tooltip lib — a positioned span; if anchored popovers are
 *  ever needed elsewhere, extract a shared useAnchoredPopover hook. */
import { useEffect, useRef, useState } from 'react';
import { Info } from 'lucide-react';

const TIP_W = 224; // w-56
const TIP_H = 64; // ~3 lines at text-[11px] leading-snug + py-1.5 — flip decision only
const MARGIN = 6;

type Pos = { above: boolean; alignRight: boolean };

export default function InfoHint({ text }: { text: string }) {
  const [open, setOpen] = useState(false);
  const [hovered, setHovered] = useState(false);
  const [pos, setPos] = useState<Pos>({ above: false, alignRight: false });
  const rootRef = useRef<HTMLSpanElement>(null);

  // Prefer below; flip above when there is no room; tie-break to the side
  // with more room. Horizontally, align right when a left-aligned tooltip
  // would stick out past the right edge (and a right-aligned one fits).
  const measure = () => {
    const root = rootRef.current;
    if (!root) return;
    const b = root.getBoundingClientRect();
    const scroller = root.closest('.custom-scrollbar');
    const s = scroller?.getBoundingClientRect();
    const minX = s ? Math.max(s.left, 0) : 0;
    const maxX = s ? Math.min(s.right, window.innerWidth) : window.innerWidth;
    const minY = s ? Math.max(s.top, 0) : 0;
    const maxY = s ? Math.min(s.bottom, window.innerHeight) : window.innerHeight;
    const fitsBelow = b.bottom + MARGIN + TIP_H <= maxY;
    const fitsAbove = b.top - MARGIN - TIP_H >= minY;
    const above = !fitsBelow && (fitsAbove || b.top - minY > maxY - b.bottom);
    // ponytail: degenerate containers narrower than the tooltip still overflow;
    // upgrade path is measuring actual tooltip width and clamping with max-w.
    const alignRight = b.left + TIP_W + MARGIN > maxX && b.right - TIP_W - MARGIN >= minX;
    setPos({ above, alignRight });
  };

  useEffect(() => {
    if (!open) return;
    const onPointerDown = (e: PointerEvent) => {
      if (!rootRef.current?.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false);
    };
    document.addEventListener('pointerdown', onPointerDown);
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('pointerdown', onPointerDown);
      document.removeEventListener('keydown', onKey);
    };
  }, [open]);

  const visible = hovered || open;

  return (
    <span ref={rootRef} className="relative inline-flex shrink-0">
      <button
        type="button"
        aria-label={text}
        aria-expanded={open}
        onMouseEnter={() => {
          measure();
          setHovered(true);
        }}
        onMouseLeave={() => setHovered(false)}
        onClick={() => {
          if (!open) measure();
          setOpen((o) => !o);
        }}
        className="w-4 h-4 inline-flex items-center justify-center rounded-full border border-zinc-600 text-zinc-400 hover:text-white hover:border-zinc-300 transition-colors"
      >
        <Info size={10} strokeWidth={3} />
      </button>
      {visible ? (
        <span
          role="tooltip"
          className={`pointer-events-none absolute z-50 w-56 border border-zinc-700 bg-zinc-900 px-2 py-1.5 text-[11px] leading-snug text-zinc-300 shadow-lg ${
            pos.above ? 'bottom-full mb-1.5' : 'top-full mt-1.5'
          } ${pos.alignRight ? 'right-0' : 'left-0'}`}
        >
          {text}
        </span>
      ) : null}
    </span>
  );
}
