/** ponytail: extracted from App.tsx inline helper. Settings/section captions — not <label> so clicks never focus nearby inputs. */
import type { ReactNode } from 'react';

export default function FieldCaption({ children, noWrap }: { children: ReactNode; noWrap?: boolean }) {
  return (
    <span
      className={`text-[9px] font-bold uppercase tracking-widest text-zinc-500 block min-w-0 ${
        noWrap ? 'whitespace-nowrap overflow-hidden text-ellipsis' : ''
      }`}
      style={noWrap ? { fontSize: 'clamp(7px, 2.4vw, 9px)' } : undefined}
    >
      {children}
    </span>
  );
}
