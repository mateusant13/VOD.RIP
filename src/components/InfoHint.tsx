/** Compact info affordance: hover shows the description as an immediate
 *  tooltip; click pins it as a small popover (closes on re-click, outside
 *  click, or Esc). Native title is kept for browsers that render it, but the
 *  in-DOM box is what actually shows in the packaged app's webview.
 *  ponytail: no tooltip lib — a positioned span; if anchored popovers are
 *  ever needed elsewhere, extract a shared useAnchoredPopover hook. */
import { useEffect, useRef, useState } from 'react';
import { Info } from 'lucide-react';

export default function InfoHint({ text }: { text: string }) {
  const [open, setOpen] = useState(false);
  const [hovered, setHovered] = useState(false);
  const [above, setAbove] = useState(false);
  const rootRef = useRef<HTMLSpanElement>(null);

  // Flip above when the button sits in the bottom half of its scroll
  // container — the settings scroll area carries .custom-scrollbar with
  // `contain: layout paint`, so a below-popover near the bottom edge would
  // be clipped instead of extending the scrollable area.
  const measure = () => {
    const btn = rootRef.current;
    if (!btn) return;
    const scroller = btn.closest('.custom-scrollbar') ?? document.scrollingElement;
    if (!scroller) return;
    const b = btn.getBoundingClientRect();
    const s = scroller.getBoundingClientRect();
    setAbove(b.top - s.top + b.height / 2 > s.height / 2);
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
        title={text}
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
          className={`absolute left-0 z-10 w-56 border border-zinc-700 bg-zinc-900 px-2 py-1.5 text-[11px] leading-snug text-zinc-300 shadow-lg ${
            above ? 'bottom-full mb-1.5' : 'top-full mt-1.5'
          }`}
        >
          {text}
        </span>
      ) : null}
    </span>
  );
}
