/**
 * Keyboard event utility functions.
 */
import type { KeyboardEvent } from 'react';

/** Let text fields, modifiers (Ctrl+A, etc.), and contenteditable keep native behavior. */
export function shouldIgnorePlayerKeyEvent(e: KeyboardEvent): boolean {
  if (e.ctrlKey || e.metaKey || e.altKey) return true;
  const el = e.target as HTMLElement;
  if (el.isContentEditable) return true;
  const tag = el.tagName;
  if (tag === 'TEXTAREA' || tag === 'SELECT') return true;
  if (tag === 'INPUT') {
    const type = (el as HTMLInputElement).type;
    return type !== 'range' && type !== 'checkbox' && type !== 'radio';
  }
  return false;
}
