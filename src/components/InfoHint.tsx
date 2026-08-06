/** Compact info affordance: the description lives in the native hover
 *  tooltip (title) + aria-label, so settings option rows stay label + control.
 *  Native title tooltip is the codebase-wide pattern (ArchiveSearchPopup etc.). */
import { Info } from 'lucide-react';

export default function InfoHint({ text }: { text: string }) {
  return (
    <button
      type="button"
      title={text}
      aria-label={text}
      className="w-4 h-4 shrink-0 inline-flex items-center justify-center rounded-full border border-zinc-600 text-zinc-400 hover:text-white hover:border-zinc-300 transition-colors"
    >
      <Info size={10} strokeWidth={3} />
    </button>
  );
}
