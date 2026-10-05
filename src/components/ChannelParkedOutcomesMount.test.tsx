import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

/**
 * WHERE the parked-channels panel is mounted - the contract, not the pixels.
 *
 * Rendering the whole App is not feasible in jsdom (App.chatOverlay.test.tsx
 * documents why: the preview only opens through the real URL->session flow), so
 * this pins the mount site by reading the source, the same technique
 * src/i18n.test.ts uses for its locale-parity scan.
 *
 * It exists as its own file, importing NOTHING from the new surface, on
 * purpose. That is what lets it run against the unfixed tree: a version that
 * imported the component could only fail with a module-resolution error, which
 * says "the file is missing", not "a parked channel is invisible in the place
 * the owner looks". This file fails with the real defect instead:
 *
 *     expected App.tsx to render <ChannelParkedOutcomes> in the channels tab
 */

/**
 * Read App.tsx as text.
 *
 * Resolved with `fileURLToPath(import.meta.url)` + `path.join`, deliberately
 * NOT `new URL('../App.tsx', import.meta.url)`: under this vitest/jsdom setup
 * the global `URL` can be jsdom's, and jsdom resolves EVERY relative reference
 * against `http://localhost:3000/`, ignoring a `file:` base. That turns the
 * path into an http URL and `fileURLToPath` then throws "The URL must be of
 * scheme file" - a failure that depends on whether the module graph happened
 * to pull Node's `URL` in ahead of time. `import.meta.url` is already an
 * absolute file: string, so no URL construction is needed at all.
 */
function appSource(): string {
  return readFileSync(path.join(path.dirname(fileURLToPath(import.meta.url)), '..', 'App.tsx'), 'utf8');
}

describe('App mounts the parked-channels panel in the CHANNELS surface', () => {
  it('imports the panel', () => {
    // A missing import is the failure this file exists to catch, so the
    // assertion says what is broken rather than printing a regex diff.
    expect(appSource()).toMatch(
      /import ChannelParkedOutcomes from '\.\/components\/ChannelParkedOutcomes'/,
    );
  });

  it('renders it inside the channels tab, not on a hidden or unrelated surface', () => {
    const src = appSource();
    // The channels tab opens here and the channel list is the first thing after
    // it, so "between the two" is the channels tab and nothing else.
    const tabAt = src.indexOf("{tab === 'channels' && (");
    const firstRowAt = src.indexOf('<ChannelRow', tabAt);
    expect(tabAt, "App.tsx no longer has a {tab === 'channels' && ( block").toBeGreaterThan(-1);
    expect(firstRowAt, 'App.tsx no longer renders <ChannelRow>').toBeGreaterThan(tabAt);

    const mountAt = src.indexOf('<ChannelParkedOutcomes', tabAt);
    expect(
      mountAt,
      'expected App.tsx to render <ChannelParkedOutcomes> in the channels tab; a parked ' +
        'channel is skipped by the walk before any request, so without this the owner ' +
        'cannot see which channels are parked, why, or un-park one',
    ).toBeGreaterThan(-1);
    expect(
      mountAt,
      '<ChannelParkedOutcomes> must sit inside the channels tab, above the channel list',
    ).toBeLessThan(firstRowAt);
  });

  it('does not mount it behind a flag that is off by default', () => {
    // A panel gated behind `false &&` is the same invisible surface wearing a
    // component: the mount site must be a bare element in the channels tab.
    const src = appSource();
    const real = src.indexOf(
      '<ChannelParkedOutcomes',
      src.indexOf("{tab === 'channels' && ("),
    );
    const line = src.slice(src.lastIndexOf('\n', real) + 1, src.indexOf('\n', real));
    expect(line.trim(), 'the mount must be a bare element, not gated by a flag').toMatch(
      /^<ChannelParkedOutcomes\s*\/>$/,
    );
  });
});
