/**
 * EditableHmsTime — extracted from App.tsx
 *
 * ponytail: inline helper extracted during App.tsx decomposition.
 * Renders an HH:MM:SS time that becomes editable on click.
 */

import { useCallback, useLayoutEffect, useRef, useState, type ReactNode } from 'react';
import { parseHms, clamp } from '../utils';

interface EditableHmsTimeProps {
  valueSec: number;
  minSec: number;
  maxSec: number;
  onChange: (sec: number) => void;
  className?: string;
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
