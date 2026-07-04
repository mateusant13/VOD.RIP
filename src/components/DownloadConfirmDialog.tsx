/**
 * DownloadConfirmDialog — extracted from App.tsx
 *
 * ponytail: extracted during App.tsx decomposition. All props-driven,
 * no direct dependency on App state. Accepts filename placeholder for
 * custom download naming.
 */

import { type ReactNode } from 'react';
import { createPortal } from 'react-dom';


interface DownloadConfirmDialogProps {
  open: boolean;
  title: string;
  message: string;
  accentColor?: string;
  filenamePlaceholder?: string;
  filename?: string;
  onFilenameChange?: (value: string) => void;
  onConfirm: () => void;
  onCancel: () => void;
}

export default function DownloadConfirmDialog({
  open,
  title,
  message,
  accentColor = '#53fc18',
  filenamePlaceholder,
  filename,
  onFilenameChange,
  onConfirm,
  onCancel,
}: DownloadConfirmDialogProps): ReactNode {
  if (!open) return null;
  return createPortal(
    <div
      className="fixed inset-0 z-[400] flex items-center justify-center bg-black/75 p-4"
      onClick={onCancel}
    >
      <div
        className="bg-zinc-950 border-2 border-white p-5 font-mono text-sm flex flex-col gap-3 min-w-[22rem] max-w-[28rem]"
        style={{ boxShadow: `4px 4px 0px 0px ${accentColor}` }}
        onClick={(e) => e.stopPropagation()}
      >
        <p className="text-zinc-200 text-[10px] font-bold uppercase tracking-widest">
          {title}
        </p>
        <p className="text-zinc-400 text-xs leading-relaxed whitespace-pre-wrap">
          {message}
        </p>
        {filenamePlaceholder != null && (
          <input
            type="text"
            value={filename ?? ''}
            placeholder={filenamePlaceholder}
            onChange={(e) => onFilenameChange?.(e.target.value)}
            className="bg-transparent border-2 border-zinc-600 text-zinc-200 px-2 py-1.5 text-xs font-mono outline-none focus:border-white"
          />
        )}
        <div className="flex items-center gap-2 justify-end mt-1">
          <button
            type="button"
            onClick={onCancel}
            className="border-2 border-zinc-600 text-zinc-300 hover:border-white hover:text-white px-3 py-1.5 text-[10px] font-bold uppercase tracking-wider"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={onConfirm}
            className="border-2 border-white bg-white text-black hover:bg-zinc-200 px-3 py-1.5 text-[10px] font-bold uppercase tracking-wider"
            style={{ boxShadow: `2px 2px 0px 0px ${accentColor}` }}
          >
            Yes, download
          </button>
        </div>
      </div>
    </div>,
    document.body,
  );
}
