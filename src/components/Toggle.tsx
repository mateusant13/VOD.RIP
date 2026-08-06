/**
 * Switch-style toggle wrapping a real checkbox (keyboard/screen-reader safe).
 * Track = bordered box, knob slides; checked = emerald, unchecked = zinc.
 * `info` renders a compact (i) hover affordance instead of a text line below.
 */
import { useId } from 'react';
import InfoHint from './InfoHint';

type Props = {
  checked: boolean;
  onChange: (checked: boolean) => void;
  label: string;
  info?: string;
  ariaLabel: string;
};

export default function Toggle({ checked, onChange, label, info, ariaLabel }: Props) {
  const id = useId();
  return (
    <div className="flex items-center justify-between gap-2 select-none group">
      <span className="flex items-center gap-1.5 min-w-0">
        <label
          htmlFor={id}
          className="text-xs font-bold uppercase tracking-wider text-zinc-300 cursor-pointer"
        >
          {label}
        </label>
        {info ? <InfoHint text={info} /> : null}
      </span>
      <label
        htmlFor={id}
        className={`relative w-9 h-[18px] shrink-0 border-2 transition-colors cursor-pointer focus-within:border-white ${
          checked ? 'bg-emerald-950 border-emerald-600' : 'bg-zinc-900 border-zinc-700 group-hover:border-zinc-500'
        }`}
      >
        <input
          id={id}
          type="checkbox"
          className="sr-only"
          checked={checked}
          onChange={(e) => onChange(e.target.checked)}
          aria-label={ariaLabel}
        />
        <span
          className={`absolute top-[1px] h-3 w-3 transition-all ${
            checked ? 'left-[18px] bg-emerald-400' : 'left-[1px] bg-zinc-500'
          }`}
        />
      </label>
    </div>
  );
}
