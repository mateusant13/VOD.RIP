/** ponytail: extracted from App.tsx inline helper. Settings/section captions — not <label> so clicks never focus nearby inputs. */
import type { ReactNode } from 'react';

export default function FieldCaption({ children, noWrap }: { children: ReactNode; noWrap?: boolean }) {
  return (
    <span
      className={`text-xs font-bold uppercase tracking-wider text-zinc-400 block min-w-0 ${
        noWrap ? 'whitespace-nowrap overflow-hidden text-ellipsis' : ''
      }`}
    >
      {children}
    </span>
  );
}
