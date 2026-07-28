// Self-check: mergeVodLists / mergeClipLists subscriber_only filtering and prunePlatforms
// Run: cd worktree && npx tsx src/channelUtils.selfcheck.ts

import { mergeVodLists, mergeClipLists, isMembersOnlyVideo, isPublicVideo } from './channelUtils';
import type { ChannelVideo } from './types';

function test(name: string, ok: boolean) {
  if (!ok) { console.error(`FAIL: ${name}`); process.exit(1); }
  console.log(`  ok  ${name}`);
}

function v(id: string, platform: string, extra: Partial<ChannelVideo> = {}): ChannelVideo {
  return {
    id,
    title: `Video ${id}`,
    platform,
    slug: `slug-${id}`,
    created_at: '2026-01-01T00:00:00Z',
    url: `https://example.com/${id}`,
    views: '100',
    length_seconds: 300,
    content_kind: 'vod',
    availability: undefined,
    ...extra,
  } as ChannelVideo;
}

// --- mergeVodLists ---

console.log('=== mergeVodLists ===');

// 1) subscriber_only cached entry is dropped on merge
{
  const existing = [v('1', 'YouTube', { availability: 'subscriber_only' })];
  const incoming = [v('2', 'YouTube')];
  const result = mergeVodLists(existing, incoming);
  test('subscriber_only filtered out', result.length === 1 && result[0].id === '2');
}

// 2) subscriber_only incoming entry is still merged (merge overrides cached entry)
{
  const existing = [v('1', 'YouTube')];
  const incoming = [v('1', 'YouTube', { availability: 'subscriber_only' })];
  const result = mergeVodLists(existing, incoming);
  test('subscriber_only incoming merged in', result[0].availability === 'subscriber_only');
}

// 3) cached YouTube entry absent from authoritative YouTube fetch is pruned
{
  const existing = [
    v('keep', 'Twitch'),
    v('prune', 'YouTube'),
    v('keep2', 'YouTube', { availability: 'subscriber_only' }), // filtered by subscriber_only check
  ];
  const incoming = [v('new', 'YouTube')];
  const result = mergeVodLists(existing, incoming, { prunePlatforms: ['YouTube'] });
  test('prune removes YouTube entry absent from incoming',
    result.every(r => r.id !== 'prune') && result.some(r => r.id === 'keep'));
  test('prune keeps Twitch entries',
    result.some(r => r.id === 'keep'));
  test('prune adds new incoming entries',
    result.some(r => r.id === 'new'));
}

// 4) cached Twitch entry survives YouTube-authoritative prune
{
  const existing = [v('twitch-vid', 'Twitch')];
  const incoming = [v('yt-vid', 'YouTube')];
  const result = mergeVodLists(existing, incoming, { prunePlatforms: ['YouTube'] });
  test('Twitch survives YouTube-only prune',
    result.some(r => r.id === 'twitch-vid') && result.some(r => r.id === 'yt-vid'));
}

// 5) cached old YouTube entry survives incremental (no prune) merge
{
  const existing = [v('old-yt', 'YouTube'), v('old-twitch', 'Twitch')];
  const incoming = [v('new-yt', 'YouTube')];
  const result = mergeVodLists(existing, incoming);
  test('old YouTube survives non-prune merge',
    result.some(r => r.id === 'old-yt') && result.some(r => r.id === 'new-yt'));
}

// 6) prunePlatforms empty array — no filtering
{
  const existing = [v('survive', 'YouTube')];
  const incoming = [v('incoming', 'YouTube')];
  const result = mergeVodLists(existing, incoming, { prunePlatforms: [] });
  test('empty prunePlatforms = no filter',
    result.some(r => r.id === 'survive') && result.some(r => r.id === 'incoming'));
}

// 7) subscriber_only + prune together
{
  const existing = [
    v('sub', 'YouTube', { availability: 'subscriber_only' }),
    v('gone', 'YouTube'),
    v('stay', 'Kick'),
  ];
  const incoming = [v('fresh', 'YouTube')];
  const result = mergeVodLists(existing, incoming, { prunePlatforms: ['YouTube'] });
  test('subscriber_only + prune combined',
    result.every(r => r.id !== 'sub' && r.id !== 'gone') && result.some(r => r.id === 'stay') && result.some(r => r.id === 'fresh'));
}

// 7b) empty incoming + prune must NOT wipe cache (regression: soft-failed YouTube fetch returned 0 videos and erased the list)
{
  const existing = [v('cached-yt', 'YouTube'), v('cached-twitch', 'Twitch')];
  const incoming: ChannelVideo[] = [];
  const result = mergeVodLists(existing, incoming, { prunePlatforms: ['YouTube'] });
  test('empty incoming + prune preserves cache',
    result.some(r => r.id === 'cached-yt') && result.some(r => r.id === 'cached-twitch'));
}

// --- mergeClipLists ---

console.log('=== mergeClipLists ===');

// 8) clip subscriber_only is dropped
{
  const existing = [v('c1', 'YouTube', { content_kind: 'clip', availability: 'subscriber_only' })];
  const incoming: ChannelVideo[] = [];
  const result = mergeClipLists(existing, incoming);
  test('clip subscriber_only filtered out', result.length === 0);
}

// 9) non-clip existing items are filtered by isLikelyClip but subscriber_only still checked
{
  const existing = [
    v('clip1', 'YouTube', { content_kind: 'clip' }),
    v('subclip', 'YouTube', { content_kind: 'clip', availability: 'subscriber_only' }),
  ];
  const incoming: ChannelVideo[] = [];
  const result = mergeClipLists(existing, incoming);
  test('non-subscriber_only clip survives, subscriber_only clip dropped',
    result.length === 1 && result[0].id === 'clip1');
}

// 10) clip prune works
{
  const existing = [
    v('ytclip', 'YouTube', { content_kind: 'clip' }),
    v('twclip', 'Twitch', { content_kind: 'clip' }),
  ];
  const incoming = [v('newclip', 'YouTube', { content_kind: 'clip' })];
  const result = mergeClipLists(existing, incoming, { prunePlatforms: ['YouTube'] });
  test('clip prune removes YouTube, keeps Twitch',
    result.every(r => r.id !== 'ytclip') && result.some(r => r.id === 'twclip') && result.some(r => r.id === 'newclip'));
}

// 11) isMembersOnlyVideo returns true for subscriber_only
{
  const result = isMembersOnlyVideo({ availability: 'subscriber_only' });
  test('isMembersOnlyVideo(subscriber_only) === true', result === true);
}

// 12) isPublicVideo returns false for subscriber_only, true for public
{
  const r1 = isPublicVideo({ availability: 'subscriber_only' });
  const r2 = isPublicVideo({ availability: 'public' });
  test('isPublicVideo(subscriber_only) === false', r1 === false);
  test('isPublicVideo(public) === true', r2 === true);
}

console.log('\n✅ All checks passed');
