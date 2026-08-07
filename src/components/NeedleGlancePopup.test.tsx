import { describe, expect, it } from 'vitest';
import { render } from '@testing-library/react';
import NeedleGlancePopup, { type NeedleGlanceState } from './NeedleGlancePopup';
import { LIVE_POPUP_ACTIVE_Z, SEARCH_POPUP_Z } from '../layoutUtils';

const GLANCE: NeedleGlanceState = {
  which: 'in',
  x: 120,
  y: 300,
  sec: 42,
  rangeStart: 10,
  rangeEnd: 90,
  deltaSec: 0,
  dragging: true,
};

describe('NeedleGlancePopup z-order', () => {
  it('floats the trim preview above the floating archive search while active', () => {
    const { unmount } = render(
      <NeedleGlancePopup glance={GLANCE} vodDurationSec={300} />,
    );
    const popup = document.querySelector('.needle-glance-popup') as HTMLElement;
    expect(popup).toBeTruthy();
    expect(Number(popup.style.zIndex)).toBe(LIVE_POPUP_ACTIVE_Z);
    expect(LIVE_POPUP_ACTIVE_Z).toBeGreaterThan(SEARCH_POPUP_Z);
    unmount();
  });

  it('renders nothing when no glance is active (no layer to restore)', () => {
    const { container } = render(
      <NeedleGlancePopup glance={null} vodDurationSec={300} />,
    );
    expect(container.querySelector('.needle-glance-popup')).toBeNull();
  });
});
