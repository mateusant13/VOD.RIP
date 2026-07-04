/** ponytail: extracted from App.tsx inline helper. Trim duration adjustment button group (‑5s, ‑1s, +1s, +5s). */

import type { ReactNode } from 'react';

export default function ClipDurationAdjustButtons({
  onAdjust,
  disabled,
  compact,
  activeEndpoint,
}: {
  onAdjust: (deltaSec: number) => void;
  disabled?: boolean;
  compact?: boolean;
  activeEndpoint: 'in' | 'out';
}): ReactNode {
  const btnClass = compact
    ? 'px-1 py-0 text-[7px] font-mono font-bold border border-zinc-700 text-zinc-400 hover:border-white hover:text-white disabled:opacity-30 disabled:pointer-events-none'
    : 'px-1.5 py-0.5 text-[8px] font-mono font-bold border border-zinc-700 text-zinc-400 hover:border-white hover:text-white disabled:opacity-30 disabled:pointer-events-none';
  const titles = activeEndpoint === 'in'
    ? { m5: 'Extend clip 5s at start', m1: 'Extend clip 1s at start', p1: 'Trim 1s from start', p5: 'Trim 5s from start' }
    : { m5: 'Trim 5s from end', m1: 'Trim 1s from end', p1: 'Extend clip 1s at end', p5: 'Extend clip 5s at end' };
  return (
    <div className={`flex items-center gap-0.5 shrink-0 ${compact ? '' : 'justify-end'}`}>
      <button type="button" disabled={disabled} onClick={() => onAdjust(-5)} className={btnClass} title={titles.m5}>-5s</button>
      <button type="button" disabled={disabled} onClick={() => onAdjust(-1)} className={btnClass} title={titles.m1}>-1s</button>
      <button type="button" disabled={disabled} onClick={() => onAdjust(1)} className={btnClass} title={titles.p1}>+1s</button>
      <button type="button" disabled={disabled} onClick={() => onAdjust(5)} className={btnClass} title={titles.p5}>+5s</button>
    </div>
  );
}
