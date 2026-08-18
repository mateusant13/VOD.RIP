import { useCallback, useEffect, useState } from 'react';

const STORAGE_KEY = 'vodrip.ui.frameMode';

function readFrameMode(): boolean {
  try {
    return localStorage.getItem(STORAGE_KEY) === '1' || localStorage.getItem(STORAGE_KEY) === 'true';
  } catch {
    return false;
  }
}

function writeFrameMode(v: boolean) {
  try {
    localStorage.setItem(STORAGE_KEY, v ? '1' : '0');
  } catch {}
}

export function useFrameMode() {
  const [frameMode, setFrameModeState] = useState<boolean>(() => readFrameMode());

  const setFrameMode = useCallback((next: boolean | ((prev: boolean) => boolean)) => {
    setFrameModeState((prev) => {
      const v = typeof next === 'function' ? (next as (p: boolean) => boolean)(prev) : next;
      writeFrameMode(v);
      return v;
    });
  }, []);

  // restore on reload: already handled by initializer; keep storage in sync for external writes
  useEffect(() => {
    const onStorage = (e: StorageEvent) => {
      if (e.key === STORAGE_KEY) setFrameModeState(e.newValue === '1' || e.newValue === 'true');
    };
    window.addEventListener('storage', onStorage);
    return () => window.removeEventListener('storage', onStorage);
  }, []);

  return { frameMode, setFrameMode } as const;
}

export default function FrameToggle({
  frameMode,
  setFrameMode,
}: {
  frameMode: boolean;
  setFrameMode: (v: boolean | ((prev: boolean) => boolean)) => void;
}) {
  return (
    <label
      data-frame-toggle
      style={{
        position: 'fixed',
        right: 12,
        bottom: 12,
        zIndex: 50,
        display: 'flex',
        alignItems: 'center',
        gap: 6,
        padding: '6px 10px',
        background: 'rgba(24,24,27,0.9)',
        border: '1px solid #3f3f46',
        borderRadius: 6,
        fontSize: 11,
        fontFamily: 'monospace',
        color: '#fafafa',
        cursor: 'pointer',
        userSelect: 'none',
      }}
    >
      <input
        type="checkbox"
        checked={frameMode}
        onChange={(e) => setFrameMode(e.target.checked)}
        aria-label="Frame mode"
      />
      Frame
    </label>
  );
}

export { STORAGE_KEY as FRAME_MODE_STORAGE_KEY, readFrameMode, writeFrameMode };
