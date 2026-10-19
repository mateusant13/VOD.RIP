import { describe, expect, it } from 'vitest';
import { render } from '@testing-library/react';
import ChannelPlatformLabel from './ChannelPlatformLabel';

/**
 * The channel row label.
 *
 * What these pin, in the order the owner meets it:
 *
 *  1. A CREATOR ON SEVERAL PLATFORMS IS ONE ROW, NOT SEVERAL. titiltei is on
 *     Kick+Twitch+YouTube; the row must read "titiltei" once with three logos,
 *     never "titiltei / titiltei / titiltei" — the repetition reads as a
 *     duplicate entry and is the thing the owner asked to have removed.
 *  2. DISTINCT SLUGS STAY DISTINCT. lubu (Twitch) and lubumr (YouTube) are
 *     two different names and both must survive, separated by "/".
 *  3. THE LOGOS CARRY THE UNLINK AFFORDANCE. Each platform logo keeps its
 *     own hover-to-remove control, so collapsing the name must not cost the
 *     ability to unlink one platform.
 */

type Props = Partial<Parameters<typeof ChannelPlatformLabel>[0]>;

function setup(props: Props = {}) {
  const utils = render(
    <ChannelPlatformLabel
      kickSlug=""
      twitchSlug=""
      youtubeSlug=""
      {...props}
    />,
  );
  const root = utils.container.firstElementChild as HTMLElement;
  const logos = root.querySelectorAll('svg[aria-label]');
  return { root, logos, ...utils };
}

describe('ChannelPlatformLabel', () => {
  it('collapses platforms that share an identical slug into one name token', () => {
    const { root, logos } = setup({
      kickSlug: 'titiltei',
      twitchSlug: 'titiltei',
      youtubeSlug: 'titiltei',
    });
    expect(root.textContent).toBe('titiltei');
    expect(logos.length).toBe(3);
  });

  it('collapses case-insensitively so "Lubu" and "lubu" are one name', () => {
    const { root, logos } = setup({ twitchSlug: 'Lubu', youtubeSlug: 'lubu' });
    expect(root.textContent).toBe('Lubu');
    expect(logos.length).toBe(2);
  });

  it('keeps distinct slugs separate with a / separator', () => {
    const { root, logos } = setup({ twitchSlug: 'lubu', youtubeSlug: 'lubumr' });
    expect(root.textContent).toBe('lubu/lubumr');
    expect(logos.length).toBe(2);
  });

  it('renders a single platform as one name and one logo', () => {
    const { root, logos } = setup({ youtubeSlug: 'gaveta' });
    expect(root.textContent).toBe('gaveta');
    expect(logos.length).toBe(1);
  });

  it('renders an explicit empty state when no platform is set', () => {
    const { root } = setup();
    expect(root.textContent).toBe('empty');
  });
});
