/** ponytail: extracted from App.tsx inline helper. Settings/section captions — not <label> so clicks never focus nearby inputs.
 *  `info` renders a compact (i) hover affordance so rows stay label + control. */
import type { ReactNode } from 'react';
import InfoHint from './InfoHint';

export default function FieldCaption({
  children,
  noWrap,
  info,
}: {
  children: ReactNode;
  noWrap?: boolean;
  info?: string;
}) {
  return (
    <div className={`flex items-center gap-1.5 min-w-0 ${noWrap ? 'whitespace-nowrap' : ''}`}>
      <span
        className={`text-xs font-bold uppercase tracking-wider text-zinc-400 min-w-0 ${
          noWrap ? 'overflow-hidden text-ellipsis' : ''
        }`}
      >
        {children}
      </span>
      {info ? <InfoHint text={info} /> : null}
    </div>
  );
}
