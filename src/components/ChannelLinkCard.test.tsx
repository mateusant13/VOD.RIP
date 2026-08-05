import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import ChannelLinkCard from './ChannelLinkCard';
import type { ChannelLinkDraft } from '../channelUtils';

const DRAFT: ChannelLinkDraft = {
  kickSlug: 'srdoglol',
  twitchSlug: 'srdogg',
  youtubeSlug: '',
  kickEnabled: true,
  twitchEnabled: true,
  youtubeEnabled: false,
  detectedFrom: 'twitch',
};

function renderCard(onCancel = vi.fn(), onConfirm = vi.fn()) {
  render(
    <ChannelLinkCard
      draft={DRAFT}
      onChange={() => {}}
      onConfirm={onConfirm}
      onCancel={onCancel}
    />,
  );
  return { onCancel, onConfirm };
}

describe('ChannelLinkCard', () => {
  it('renders the header + hint and a top-right close button wired to onCancel', () => {
    const { onCancel } = renderCard();
    expect(screen.getByText('Link channel')).toBeInTheDocument();
    expect(
      screen.getByText(/Usernames can be different on each platform/),
    ).toBeInTheDocument();
    const close = screen.getByRole('button', { name: 'Close' });
    expect(close).toHaveAttribute('title', 'Close');
    expect(close).toHaveAttribute('aria-label', 'Close');
    fireEvent.click(close);
    expect(onCancel).toHaveBeenCalledTimes(1);
  });

  it('the bottom Cancel button also closes the card', () => {
    const { onCancel } = renderCard();
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(onCancel).toHaveBeenCalledTimes(1);
  });

  it('Add channel is enabled for a valid draft and fires onConfirm', () => {
    const { onConfirm } = renderCard();
    const add = screen.getByRole('button', { name: 'Add channel' });
    expect(add).toBeEnabled();
    fireEvent.click(add);
    expect(onConfirm).toHaveBeenCalledTimes(1);
  });
});
