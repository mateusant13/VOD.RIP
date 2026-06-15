/**
 * EditableHmsTime — extracted from App.tsx
 *
 * ponytail: inline helper extracted during App.tsx decomposition.
 * Renders an HH:MM:SS time that becomes editable on click.
 */

import { useCallback, useLayoutEffect, useRef, useState, type ReactNode } from 'react';
/** Parse an HH:MM:SS string to seconds (extracted from App.tsx helpers). */
function parseHms(t: string): number {
  const parts = t.split(':').map((s) => parseInt(s, 10) || 0);
  if (parts.length === 3) return parts[0] * 3600 + parts[1] * 60 + parts[2];
  if (parts.length === 2) return parts[0] * 60 + parts[1];
  return parts[0] || 0;
}

interface EditableHmsTimeProps {
  valueSec: number;
  minSec: number;
  maxSec: number;
  onChange: (sec: number) => void;
  className?: string;
}

function clamp(v: number, min: number, max: number): number {
  return Math.max(min, Math.min(max, v));
}

export default function EditableHmsTime({
  valueSec,
  minSec,
  maxSec,
  onChange,
  className = '',
}: EditableHmsTimeProps): ReactNode {
  const [editing, setEditing] = useState(false);
  const ref = useRef<HTMLSpanElement>(null);

  const commit = useCallback(() => {
    const el = ref.current;
    if (!el) return;
    const parsed = parseHms((el.textContent || '').trim());
    onChange(clamp(parsed, minSec, maxSec));
    setEditing(false);
  }, [onChange, minSec, maxSec]);

  useLayoutEffect(() => {
    if (editing && ref.current) {
      ref.current.focus();
      // Select all text in the span
      const sel = window.getSelection();
      if (sel) {
        const range = document.createRange();
        range.selectNodeContents(ref.current);
        sel.removeAllRanges();
        sel.addRange(range);
      }
    }
  }, [editing]);

  const h = Math.floor(valueSec / 3600);
  const m = Math.floor((valueSec % 3600) / 60);
  const s = Math.floor(valueSec % 60);
  const pad = (n: number) => n.toString().padStart(2, '0');

  if (editing) {
    return (
      <span
        ref={ref}
        contentEditable
        suppressContentEditableWarning
        className={`bg-zinc-800 text-zinc-200 px-0.5 outline-none focus:ring-1 focus:ring-white/30 ${className}`}
        onBlur={commit}
        onKeyDown={(e) => {
          if (e.key === 'Enter') {
            e.preventDefault();
            commit();
          }
          if (e.key === 'Escape') {
            setEditing(false);
          }
        }}
      >
        {`${pad(h)}:${pad(m)}:${pad(s)}`}
      </span>
    );
  }

  return (
    <span
      className={`cursor-pointer hover:text-white ${className}`}
      tabIndex={0}
      role="button"
      onClick={() => setEditing(true)}
      onKeyDown={(e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          setEditing(true);
        }
      }}
    >
      {`${pad(h)}:${pad(m)}:${pad(s)}`}
    </span>
  );
}
