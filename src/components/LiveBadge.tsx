export interface LiveEntry {
  platform: string;
  is_live: boolean;
  title: string;
  url: string;
  headers: Record<string, string>;
  type: string;
}

export function LiveBadge({
  entries,
  invisible,
  onClick,
  ariaLabel,
  onMouseEnter,
}: {
  entries: LiveEntry[];
  invisible?: boolean;
  /** When provided the badge itself is the clickable live control (opens the live player). */
  onClick?: (e: React.MouseEvent) => void;
  ariaLabel?: string;
  onMouseEnter?: () => void;
}) {
  if (entries.length === 0 && !invisible) return null;
  const title = entries.map((e) => `${e.platform}: ${e.title}`).join('\n');
  const inner = (
    <>
      <span className="h-1.5 w-1.5 rounded-full bg-red-500 animate-pulse" />
      LIVE
    </>
  );
  const baseClass =
    "flex items-center gap-1 rounded bg-red-900/50 px-1 text-[10px] text-red-200 font-bold shrink-0";
  if (onClick) {
    return (
      <button
        type="button"
        title={title}
        aria-label={ariaLabel}
        onClick={onClick}
        onMouseEnter={onMouseEnter}
        className={`${baseClass} cursor-pointer hover:bg-red-800/70 active:bg-red-800`}
        style={invisible ? { visibility: 'hidden' } : undefined}
      >
        {inner}
      </button>
    );
  }
  return (
    <span
      title={title}
      className={baseClass}
      onMouseEnter={onMouseEnter}
      style={invisible ? { visibility: 'hidden' } : undefined}
    >
      {inner}
    </span>
  );
}
