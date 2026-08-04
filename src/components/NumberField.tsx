/**
 * Numeric input with − / + stepper buttons (used in settings).
 * Native spinners hidden; clamping to [min, max] on every change.
 */
type Props = {
  value: number;
  min: number;
  max: number;
  step?: number;
  onChange: (v: number) => void;
  ariaLabel: string;
};

export default function NumberField({ value, min, max, step = 1, onChange, ariaLabel }: Props) {
  const clamp = (v: number) => Math.max(min, Math.min(max, v));
  return (
    <div className="flex items-stretch border-2 border-zinc-800 focus-within:border-white transition-colors">
      <button
        type="button"
        aria-label={`${ariaLabel} minus`}
        disabled={value <= min}
        onClick={() => onChange(clamp(value - step))}
        className="w-6 flex items-center justify-center text-sm leading-none text-zinc-400 hover:text-white hover:bg-zinc-800 disabled:opacity-30 disabled:hover:bg-transparent disabled:hover:text-zinc-400"
      >
        −
      </button>
      <input
        type="number"
        aria-label={ariaLabel}
        min={min}
        max={max}
        step={step}
        value={value}
        onChange={(e) => onChange(clamp(parseInt(e.target.value) || min))}
        className="w-full min-w-0 bg-zinc-950 text-white font-mono text-xs py-1.5 px-1 text-center focus:outline-none [appearance:textfield] [&::-webkit-outer-spin-button]:appearance-none [&::-webkit-inner-spin-button]:appearance-none"
      />
      <button
        type="button"
        aria-label={`${ariaLabel} plus`}
        disabled={value >= max}
        onClick={() => onChange(clamp(value + step))}
        className="w-6 flex items-center justify-center text-sm leading-none text-zinc-400 hover:text-white hover:bg-zinc-800 disabled:opacity-30 disabled:hover:bg-transparent disabled:hover:text-zinc-400"
      >
        +
      </button>
    </div>
  );
}
