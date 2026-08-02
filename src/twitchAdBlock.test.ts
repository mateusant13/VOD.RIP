import { describe, it, expect } from 'vitest';
import { stripTwitchAdSegments } from './twitchAdBlock';

const LIVE = 'https://video-edge-abc123.ams01.abs.hls.ttvnw.net/v1/segment/abc_live.ts';
const AD = 'https://ad-segments.ttvnw.net/stitched/segment_ad_1.ts';

const twitchLivePlaylist = `#EXTM3U
#EXT-X-VERSION:6
#EXT-X-TARGETDURATION:4
#EXT-X-MEDIA-SEQUENCE:12583
#EXT-X-TWITCH-PREFETCH:https://video-edge-abc123.ams01.abs.hls.ttvnw.net/v1/segment/abc_live.ts
#EXT-X-PROGRAM-DATE-TIME:2026-08-02T05:00:00.000Z
#EXTINF:2.000,foo
${LIVE}
#EXT-X-DISCONTINUITY
#EXT-X-DATERANGE:ID="stitched-ad-1",CLASS="twitch-stitched-ad",START-DATE="2026-08-02T05:00:02.000Z",DURATION=30.0,X-TV-TWITCH-AD-URL="https://track.adserver.example/click?x=1",X-TV-TWITCH-AD-CLICK-TRACKING-URL="https://track.adserver.example/click2?y=2"
#EXTINF:6.000,ad
${AD}
#EXTINF:6.000,ad
https://ad-segments.ttvnw.net/stitched/segment_ad_2.ts
#EXT-X-DISCONTINUITY
#EXT-X-PROGRAM-DATE-TIME:2026-08-02T05:00:32.000Z
#EXTINF:2.000,foo
https://video-edge-abc123.ams01.abs.hls.ttvnw.net/v1/segment/def_live.ts`;

describe('stripTwitchAdSegments', () => {
  it('removes stitched ad segments and prefetch lines from a Twitch live playlist', () => {
    const out = stripTwitchAdSegments(twitchLivePlaylist);
    expect(out).not.toContain('stitched/segment_ad');
    expect(out).not.toContain('EXT-X-TWITCH-PREFETCH');
    // Both live segments survive.
    expect(out).toContain(LIVE);
    expect(out).toContain('segment/def_live.ts');
    // The ad's EXTINF lines are gone too.
    expect(out).not.toContain('#EXTINF:6.000');
    // Still a valid playlist: 2 EXTINF + live segment URIs remain.
    expect(out.match(/#EXTINF/g)?.length).toBe(2);
  });

  it('neutralizes Twitch ad tracking URLs', () => {
    const out = stripTwitchAdSegments(twitchLivePlaylist);
    expect(out).not.toContain('track.adserver.example');
    expect(out).toContain('X-TV-TWITCH-AD-URL="https://twitch.tv"');
    expect(out).toContain('X-TV-TWITCH-AD-CLICK-TRACKING-URL="https://twitch.tv"');
  });

  it('keeps non-Twitch playlists untouched', () => {
    const plain = '#EXTM3U\n#EXTINF:2.000,\nseg1.ts\n#EXTINF:2.000,\nseg2.ts';
    expect(stripTwitchAdSegments(plain)).toBe(plain);
  });

  it('keeps master playlists untouched (no EXTINF segments)', () => {
    const master = '#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1280000,RESOLUTION=1280x720\nhttps://x/variant1.m3u8';
    expect(stripTwitchAdSegments(master)).toBe(master);
  });

  it('does not strip when every segment is an ad (full takeover)', () => {
    const takeover = `#EXTM3U\n#EXTINF:6.000,ad\n${AD}\n#EXTINF:6.000,ad\nhttps://ad-segments.ttvnw.net/stitched/segment_ad_2.ts`;
    expect(stripTwitchAdSegments(takeover)).toBe(takeover);
  });

  it('returns the input unchanged for non-HLS text', () => {
    const json = '{"hello":"world"}';
    expect(stripTwitchAdSegments(json)).toBe(json);
  });
});
