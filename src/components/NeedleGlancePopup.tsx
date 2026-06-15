/** ponytail: extracted from App.tsx inline helper. Needle glance popup for trim start/end preview. */

import { createPortal } from 'react-dom';
import type { ReactNode } from 'react';

export type NeedleGlanceState = {
  which: 'in' | 'out';
  x: number;
  y: number;
  sec: number;
  rangeStart: number;
  rangeEnd: number;
  deltaSec: number;
  dragging: boolean;
};

function formatHmsFull(sec: number): string {
  sec = Math.max(0, Math.floor(sec));
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = sec % 60;
  const pad = (n: number) => n.toString().padStart(2, '0');
  return `${pad(h)}:${pad(m)}:${pad(s)}`;
}

export default function NeedleGlancePopup({
  glance,
  vodDurationSec,
}: {
  glance: NeedleGlanceState | null;
  vodDurationSec: number;
}): ReactNode {
  if (!glance || vodDurationSec <= 0) return null;

  const clipLen = Math.max(1, glance.rangeEnd - glance.rangeStart);
  const winDur = Math.max(1, vodDurationSec);
  const needlePct = (glance.sec / winDur) * 100;
  const selStartPct = (glance.rangeStart / winDur) * 100;
  const selEndPct = (glance.rangeEnd / winDur) * 100;

  const deltaLabel = glance.deltaSec === 0
    ? null
    : `${glance.deltaSec > 0 ? '+' : '−'}${Math.abs(glance.deltaSec)}s`;

  const popupLeft = Math.min(glance.x + 14, window.innerWidth - 200);
  const popupTop = Math.max(12, glance.y - 96);

  return createPortal(
    <div
      className={`needle-glance-popup fixed z-[500] pointer-events-none select-none ${
        glance.dragging ? 'needle-glance-popup--drag' : 'needle-glance-popup--idle'
      }`}
      style={{ left: popupLeft, top: popupTop }}
    >
      <div className="border-2 border-zinc-500 bg-zinc-950/95 px-3 py-2 shadow-[4px_4px_0px_0px_rgba(113,113,122,0.5)] min-w-[168px]">
        <div className="text-[9px] font-mono uppercase tracking-widest text-zinc-500 mb-1">
          {glance.which === 'in' ? 'Trim start' : 'Trim end'}
        </div>
        <div className="text-2xl font-mono font-bold text-white tabular-nums leading-none">
          {formatHmsFull(glance.sec)}
        </div>
        <div className="mt-1.5 flex items-center justify-between gap-2 text-[10px] font-mono text-zinc-400">
          <span>Clip length</span>
          <span className="text-zinc-200">{formatHmsFull(clipLen)}</span>
        </div>
        {deltaLabel && (
          <div className="text-[10px] font-mono text-zinc-400 mt-0.5">
            <span className="text-white">{deltaLabel}</span>
            {' '}from drag start
          </div>
        )}
        <div className="needle-glance-zoom-rail relative h-5 mt-2 rounded-sm bg-zinc-800 overflow-hidden">
          <div
            className="absolute top-1 bottom-1 bg-zinc-500/35 border-y border-zinc-400/50"
            style={{ left: `${selStartPct}%`, width: `${Math.max(2, selEndPct - selStartPct)}%` }}
          />
          <div
            className={`absolute top-0 bottom-0 w-0.5 -translate-x-1/2 ${
              glance.which === 'in' ? 'bg-white' : 'bg-zinc-400'
            }`}
            style={{ left: `${needlePct}%` }}
          />
        </div>
        <div className="flex justify-between text-[8px] font-mono text-zinc-600 mt-0.5 tabular-nums">
          <span>0:00</span>
          <span>{formatHmsFull(vodDurationSec)}</span>
        </div>
      </div>
    </div>,
    document.body,
  );
}
