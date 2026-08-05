/**
 * Switch-style toggle wrapping a real checkbox (keyboard/screen-reader safe).
 * Track = bordered box, knob slides; checked = emerald, unchecked = zinc.
 */
type Props = {
  checked: boolean;
  onChange: (checked: boolean) => void;
  label: string;
  hint?: string;
  ariaLabel: string;
};

export default function Toggle({ checked, onChange, label, hint, ariaLabel }: Props) {
  return (
    <label className="flex items-center justify-between gap-2 cursor-pointer select-none group">
      <span className="flex flex-col gap-1 min-w-0">
        <span className="text-xs font-bold uppercase tracking-wider text-zinc-300">{label}</span>
        {hint ? <span className="text-xs text-zinc-500 font-mono leading-relaxed">{hint}</span> : null}
      </span>
      <span
        className={`relative w-9 h-[18px] shrink-0 border-2 transition-colors focus-within:border-white ${
          checked ? 'bg-emerald-950 border-emerald-600' : 'bg-zinc-900 border-zinc-700 group-hover:border-zinc-500'
        }`}
      >
        <input
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
      </span>
    </label>
  );
}
