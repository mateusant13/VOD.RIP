/** The app-modal z law: every app-level overlay uses MODAL_Z.

This exists because the audit of 2026-10-12 found three overlays that each
answered the question differently, and the three answers were mutually
contradictory:

- DownloadConfirmDialog and ExtensionWaitOverlay used `zIndex: MODAL_Z`.
- CookieInstallOffer used the bare Tailwind class `z-[21000]`.
- FirstRunWizard used `z-[9999]` - which is EXPLORE_POPUP_Z, the base every
  floating player stacks on, so a player opened afterwards painted over a
  wizard the user still has to answer.

A magic literal is worse than a wrong constant: 21000 outranks the whole
ladder for no reason a reader can see, and the next modal written against the
ladder would land underneath it. The assertions here fail if any of those
files stops using MODAL_Z.
*/
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';
import { EXPLORE_POPUP_Z, MODAL_Z } from '../layoutUtils';

const MODAL_FILES = [
  'DownloadConfirmDialog.tsx',
  'ExtensionWaitOverlay.tsx',
  'CookieInstallOffer.tsx',
  'FirstRunWizard.tsx',
];

function src(name: string): string {
  return readFileSync(join(__dirname, name), 'utf8');
}

describe('app modals share ONE z base', () => {
  it('MODAL_Z is above the whole floating-popup ladder', () => {
    // The property that makes a modal a modal: nothing a user can leave
    // floating may outrank it.
    expect(MODAL_Z).toBeGreaterThan(EXPLORE_POPUP_Z);
  });

  it.each(MODAL_FILES)('%s uses MODAL_Z and no bare z literal', (f) => {
    const code = src(f);
    // A literal is the defect: it cannot be compared against the ladder.
    expect(code).not.toMatch(/z-\[\d+\]/);
    expect(code).not.toMatch(/zIndex:\s*\d{3,}/);
    expect(code).toMatch(/zIndex:\s*MODAL_Z/);
  });

  it('a bare z literal anywhere in src fails the same law', () => {
    // The tail of the same bug, not a new rule: the audit found these in
    // components, but a literal introduced in App.tsx tomorrow would be the
    // identical defect. This scans the files the first audit covered.
    const offenders = MODAL_FILES.filter((f) => /z-\[\d+\]/.test(src(f)));
    expect(offenders).toEqual([]);
  });
});