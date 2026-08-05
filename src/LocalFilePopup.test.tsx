import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import LocalFilePopup, { type LocalFilePopupItem } from './LocalFilePopup';

const ITEM: LocalFilePopupItem = {
  id: 'l1',
  filePath: 'C:\\VODs\\clip.mp4',
  title: 'Clip A',
  platform: 'twitch',
};

function renderPopup() {
  const onClose = vi.fn();
  const view = render(
    <LocalFilePopup
      item={ITEM}
      zIndex={10}
      stackIndex={0}
      onClose={onClose}
      onBringToFront={vi.fn()}
      onOpenHit={vi.fn()}
      savedChannels={[]}
    />,
  );
  return { ...view, onClose };
}

describe('LocalFilePopup', () => {
  it('renders exactly 8 resize handles ([data-panel-resize])', () => {
    renderPopup();
    expect(document.querySelectorAll('[data-panel-resize]')).toHaveLength(8);
  });

  it('renders the local video element and the close button fires onClose', () => {
    const { container, onClose } = renderPopup();
    expect(container.querySelector('video')).not.toBeNull();
    fireEvent.click(screen.getByTitle('Close'));
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});
