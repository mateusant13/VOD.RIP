/** Compact info affordance: hover shows the description as an immediate
 *  tooltip; click pins it as a small popover (closes on re-click, outside
 *  click, or Esc). No native title — the in-DOM box is the only tooltip.
 *
 *  The box is absolutely positioned inside the tab scroll container, which
 *  carries .custom-scrollbar { contain: layout paint } and therefore clips
 *  absolutely-positioned descendants at its padding box. Placement is
 *  measured against that scroller (falling back to the viewport): the box is
 *  capped to the space actually available — maxWidth shrinks a narrow
 *  container, maxHeight + overflow-y-auto scroll long text internally — and
 *  pinned inside the bounds with an inline left. It flips above when there
 *  is no room below, re-measures with the real box size once mounted, and
 *  re-positions on scroll/resize while pinned, so it never overflows or gets
 *  cut off.
 *  ponytail: no tooltip lib — a positioned span; if anchored popovers are
 *  ever needed elsewhere, extract a shared useAnchoredPopover hook. */
import { useEffect, useId, useLayoutEffect, useRef, useState } from 'react';
import { Info } from 'lucide-react';

const TIP_W = 224; // preferred width (w-56)
const TIP_H = 64; // conservative height floor for the pre-mount flip decision
const MARGIN = 6;
const PAD_H = 14; // py-1.5 + 2px borders — vertical size estimate
const LINE_H = 15; // ~11px text at leading-snug
const CHARS_PER_LINE = 33; // w-56 minus px-2 padding, ~11px font
const MIN_H = 24; // never collapse the box below a readable sliver

type Pos = { above: boolean; left: number; maxWidth: number; maxHeight: number };

export default function InfoHint({ text }: { text: string }) {
  const [open, setOpen] = useState(false);
  const [hovered, setHovered] = useState(false);
  const [pos, setPos] = useState<Pos>({ above: false, left: 0, maxWidth: TIP_W, maxHeight: 0 });
  const rootRef = useRef<HTMLSpanElement>(null);
  const tipRef = useRef<HTMLSpanElement>(null);
  // Stable id wiring the button's aria-describedby to the tooltip box.
  const tipId = useId();
  const visible = hovered || open;

  /** Height estimate used before the box is mounted (first pass, jsdom). */
  const estimateH = () =>
    Math.max(TIP_H, PAD_H + Math.ceil(text.length / CHARS_PER_LINE) * LINE_H);

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

    // Never wider than the room: shrink w-56 when the container is narrow so
    // the box always fits horizontally.
    const maxWidth = Math.min(TIP_W, Math.max(1, maxX - minX - 2 * MARGIN));
    // Real content height once mounted (scrollHeight ignores the maxHeight
    // cap); estimate otherwise.
    const tip = tipRef.current;
    const h = tip && tip.scrollHeight ? tip.scrollHeight : estimateH();

    const roomBelow = maxY - b.bottom - MARGIN;
    const roomAbove = b.top - minY - MARGIN;
    // Prefer below; flip above when there is no room; tie-break to the side
    // with more room. The maxHeight cap makes the choice safe even when
    // neither side fits: the box scrolls internally instead of clipping.
    const above = h > roomBelow && (roomAbove >= h || roomAbove > roomBelow);
    const room = above ? roomAbove : roomBelow;

    // Pin horizontally: left-aligned by default; right-align when that would
    // stick out past the right edge; clamp when neither fits (narrow box).
    let left = 0;
    if (b.left + maxWidth + MARGIN > maxX) {
      left = b.right - maxWidth - b.left;
    }
    left = Math.max(minX + MARGIN - b.left, Math.min(maxX - MARGIN - maxWidth - b.left, left));

    setPos((prev) =>
      prev.above === above && prev.left === left &&
        prev.maxWidth === maxWidth && prev.maxHeight === Math.max(MIN_H, room - 2)
        ? prev
        : { above, left, maxWidth, maxHeight: Math.max(MIN_H, room - 2) }
    );
  };

  useEffect(() => {
    if (!open) return;
    const onPointerDown = (e: PointerEvent) => {
      if (!rootRef.current?.contains(e.target as Node)) setOpen(false);
    };
    const scroller: EventTarget = rootRef.current?.closest('.custom-scrollbar') ?? window;
    scroller.addEventListener('scroll', measure, { passive: true });
    window.addEventListener('resize', measure);
    document.addEventListener('pointerdown', onPointerDown);
    return () => {
      scroller.removeEventListener('scroll', measure);
      window.removeEventListener('resize', measure);
      document.removeEventListener('pointerdown', onPointerDown);
    };
  }, [open]);

  // Escape dismisses BOTH states — the hover tooltip included: a keyboard
  // user cannot mouse-leave to close it (the pinned popover already closed
  // on Esc via the same handler).
  useEffect(() => {
    if (!visible) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        setOpen(false);
        setHovered(false);
      }
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [visible]);

  // Re-measure with the real box size once it is mounted — runs before paint,
  // so the position is final by the time the user sees it.
  useLayoutEffect(() => {
    if (tipRef.current) measure();
  });

  return (
    <span ref={rootRef} className="relative inline-flex shrink-0">
      <button
        type="button"
        aria-label={text}
        aria-expanded={open}
        aria-describedby={visible ? tipId : undefined}
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
          ref={tipRef}
          id={tipId}
          role="tooltip"
          className={`absolute z-50 overflow-y-auto border border-zinc-700 bg-zinc-900 px-2 py-1.5 text-[11px] leading-snug text-zinc-300 shadow-lg ${
            pos.above ? 'bottom-full mb-1.5' : 'top-full mt-1.5'
          } ${open ? 'pointer-events-auto' : 'pointer-events-none'}`}
          style={{ left: pos.left, width: pos.maxWidth, maxWidth: pos.maxWidth, maxHeight: pos.maxHeight }}
        >
          {text}
        </span>
      ) : null}
    </span>
  );
}
