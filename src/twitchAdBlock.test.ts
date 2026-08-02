import { describe, it, expect } from 'vitest';
import {
  TwitchAdBlockLoader,
  TwitchAdRotationTracker,
  createTwitchAdRotationHandler,
  stripTwitchAdSegments,
  twitchAdBlockHlsConfig,
} from './twitchAdBlock';

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

describe('TwitchAdRotationTracker', () => {
  const opts = { rotationThreshold: 2, rotationCooldownMs: 60_000, maxRotations: 4 };

  it('rotates only after the threshold of consecutive ad-tainted refreshes', () => {
    const t = new TwitchAdRotationTracker(opts);
    expect(t.onAds(1_000_000)).toBe(false); // first ad-tainted playlist
    expect(t.onAds(1_001_000)).toBe(true); // second — rotate
    expect(t.onAds(1_002_000)).toBe(false); // count restarted, cooldown active
  });

  it('a clean playlist resets the count (ads ended)', () => {
    const t = new TwitchAdRotationTracker(opts);
    t.onAds(1_000_000);
    t.onCleanPlaylist();
    t.onAds(2_000_000);
    expect(t.onAds(3_000_000)).toBe(true); // still needs two fresh ad-tainted refreshes
  });

  it('respects the cooldown window and resumes counting after it', () => {
    const t = new TwitchAdRotationTracker({ ...opts, rotationCooldownMs: 1_000 });
    t.onAds(1_000);
    t.onAds(1_001); // rotate at t=1_001
    t.onAds(1_002);
    expect(t.onAds(1_003)).toBe(false); // inside cooldown — no rotation, no count
    t.onAds(5_000);
    expect(t.onAds(5_001)).toBe(true); // cooldown passed, count reached again
  });

  it('caps the number of rotations per session', () => {
    const t = new TwitchAdRotationTracker({ ...opts, rotationCooldownMs: 0, maxRotations: 1 });
    t.onAds(0);
    expect(t.onAds(1)).toBe(true); // first rotation used
    t.onAds(2);
    expect(t.onAds(3)).toBe(false); // cap reached — strip-only from now on
  });
});

describe('createTwitchAdRotationHandler', () => {
  it('reloads the master URL on the hls instance and resumes playback', async () => {
    const loaded: string[] = [];
    let played = false;
    const rot = createTwitchAdRotationHandler({
      getSessionId: () => 'sid123',
      getHls: () => ({ loadSource: (u) => loaded.push(u) }),
      getVideo: () => ({ paused: false, play: () => { played = true; return Promise.resolve(); } }),
      requestRotation: async (sid) => {
        expect(sid).toBe('sid123');
        return { ok: true, master_url: '/api/preview/hls/sid123/master.m3u8' };
      },
    });
    rot();
    await Promise.resolve();
    await Promise.resolve();
    expect(loaded).toEqual(['/api/preview/hls/sid123/master.m3u8']);
    expect(played).toBe(true);
  });

  it('keeps stripping (no reload) when rotation fails or is rejected', async () => {
    const loaded: string[] = [];
    const rot = createTwitchAdRotationHandler({
      getSessionId: () => 'sid1',
      getHls: () => ({ loadSource: (u) => loaded.push(u) }),
      getVideo: () => ({ paused: true, play: () => Promise.resolve() }),
      requestRotation: async () => { throw new Error('backend down'); },
    });
    rot();
    await Promise.resolve();
    await Promise.resolve();
    expect(loaded).toEqual([]); // strip fallback — never a black screen
  });

  it('does nothing when there is no session or player yet', () => {
    const rot = createTwitchAdRotationHandler({
      getSessionId: () => null,
      getHls: () => null,
      getVideo: () => null,
      requestRotation: async () => { throw new Error('must not be called'); },
    });
    expect(() => rot()).not.toThrow();
  });
});

describe('twitchAdBlockHlsConfig + loader', () => {
  it('stays compatible: pLoader plus an inert rotation key', () => {
    const cfg = twitchAdBlockHlsConfig();
    expect(cfg.pLoader).toBe(TwitchAdBlockLoader);
    expect(cfg.twitchAdRotation).toBeNull();
  });

  it('passes the rotation options into the pLoader constructor (hls.js hands it the config)', () => {
    const fired: string[] = [];
    const cfg = twitchAdBlockHlsConfig({ onAdRotation: ({ url }) => fired.push(url) });
    const loader = new TwitchAdBlockLoader(cfg);
    expect(loader).toBeInstanceOf(TwitchAdBlockLoader);
    expect(fired).toEqual([]);
  });

  it('defaults to strip-only when no rotation callback is configured', () => {
    const loader = new TwitchAdBlockLoader(twitchAdBlockHlsConfig());
    expect(loader).toBeInstanceOf(TwitchAdBlockLoader);
  });
});
