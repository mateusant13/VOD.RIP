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
#EXTINF:2.000,live
${LIVE}
#EXT-X-DISCONTINUITY
#EXT-X-DATERANGE:ID="stitched-ad-1",CLASS="twitch-stitched-ad",START-DATE="2026-08-02T05:00:02.000Z",DURATION=30.0,X-TV-TWITCH-AD-URL="https://track.adserver.example/click?x=1",X-TV-TWITCH-AD-CLICK-TRACKING-URL="https://track.adserver.example/click2?y=2"
#EXTINF:6.000,ad
${AD}
#EXTINF:6.000,ad
https://ad-segments.ttvnw.net/stitched/segment_ad_2.ts
#EXT-X-DISCONTINUITY
#EXT-X-PROGRAM-DATE-TIME:2026-08-02T05:00:32.000Z
#EXTINF:2.000,live
https://video-edge-abc123.ams01.abs.hls.ttvnw.net/v1/segment/def_live.ts`;

// The app's preview proxy rewrites every segment URI to an opaque resource
// URL, so the ONLY remaining ad marker is the CLASS="twitch-stitched-ad"
// DATERANGE tag — captured verbatim from a live Twitch session during a real
// preroll (segments with an ad-source title like 'Amazon|...').
const REWRITTEN_LIVE = '/api/preview/hls/sid/resource?id=abcd1234';
const REWRITTEN_AD = '/api/preview/hls/sid/resource?id=ad000000';

const rewrittenTwitchLivePlaylist = `#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:5
#EXT-X-MEDIA-SEQUENCE:0
#EXT-X-DATERANGE:ID="playlist-creation-1785715881",CLASS="timestamp",START-DATE="2026-08-03T00:11:21.228Z",END-ON-NEXT=YES
#EXT-X-DATERANGE:ID="stitched-ad-1785715876-30235000000",CLASS="twitch-stitched-ad",START-DATE="2026-08-03T00:11:16.185Z",DURATION=30.235,X-TV-TWITCH-AD-RADS-TOKEN="eyJhbGciOiJIUzI1NiJ9.abc"
#EXT-X-DATERANGE:ID="source-1785715876",CLASS="twitch-stream-source",START-DATE="2026-08-03T00:11:16.185Z",END-ON-NEXT=YES,X-TV-TWITCH-STREAM-SOURCE="Amazon|2474283100494"
#EXT-X-DATERANGE:ID="quartile-1785715876-0",CLASS="twitch-ad-quartile",START-DATE="2026-08-03T00:11:16.185Z",DURATION=2.000,X-TV-TWITCH-AD-QUARTILE="0"
#EXT-X-DISCONTINUITY
#EXT-X-PROGRAM-DATE-TIME:2026-08-03T00:11:16.185Z
#EXTINF:2.000,Amazon|2474283100494
${REWRITTEN_AD}
#EXTINF:2.000,Amazon|2474283100494
/api/preview/hls/sid/resource?id=ad000001
#EXT-X-DATERANGE:ID="source-1785715906",CLASS="twitch-stream-source",START-DATE="2026-08-03T00:11:46.420Z",END-ON-NEXT=YES,X-TV-TWITCH-STREAM-SOURCE="live"
#EXT-X-DISCONTINUITY
#EXT-X-TWITCH-LIVE-SEQUENCE:37551
#EXT-X-PROGRAM-DATE-TIME:2026-08-03T00:11:46.420Z
#EXTINF:2.000,live
${REWRITTEN_LIVE}
#EXTINF:2.000,live
/api/preview/hls/sid/resource?id=abcd1235`;

// Whole window is ads (fresh-session preroll, MEDIA-SEQUENCE 0) — nothing to
// delete safely: emptying the playlist makes hls.js fail with a fatal
// levelEmptyError, so the strip serves it unchanged and the loader counts it
// as ad-tainted (rotation switch).
const rewrittenFullTakeover = `#EXTM3U
#EXT-X-VERSION:3
#EXT-X-DATERANGE:ID="stitched-ad-1785715876-30235000000",CLASS="twitch-stitched-ad",START-DATE="2026-08-03T00:11:16.185Z",DURATION=30.235,X-TV-TWITCH-AD-RADS-TOKEN="eyJhbGciOiJIUzI1NiJ9.abc"
#EXT-X-DISCONTINUITY
#EXTINF:2.000,Amazon|2474283100494
${REWRITTEN_AD}
#EXTINF:2.000,Amazon|2474283100494
/api/preview/hls/sid/resource?id=ad000001
#EXTINF:2.000,Amazon|2474283100494
/api/preview/hls/sid/resource?id=ad000002`;

// LL-HLS: ad content can arrive as #EXT-X-PART tags inside the ad window.
const rewrittenLlhlsWithAd = `#EXTM3U
#EXT-X-VERSION:9
#EXT-X-TARGETDURATION:2
#EXT-X-PART-INF:PART-TARGET=0.33334
#EXT-X-DATERANGE:ID="stitched-ad-1785715876-30235000000",CLASS="twitch-stitched-ad",START-DATE="2026-08-03T00:11:16.185Z",DURATION=8.0
#EXT-X-DISCONTINUITY
#EXT-X-PART:DURATION=2.000,URI="${REWRITTEN_AD}"
#EXT-X-PART:DURATION=2.000,URI="/api/preview/hls/sid/resource?id=ad000001"
#EXT-X-DATERANGE:ID="source-1785715906",CLASS="twitch-stream-source",START-DATE="2026-08-03T00:11:22.420Z",END-ON-NEXT=YES,X-TV-TWITCH-STREAM-SOURCE="live"
#EXT-X-DISCONTINUITY
#EXTINF:2.000,live
${REWRITTEN_LIVE}`;

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

  it('keeps the proxied live playlist intact (detection-only — deleting live fragments stalls hls.js)', () => {
    const out = stripTwitchAdSegments(rewrittenTwitchLivePlaylist);
    // Ad segments are NOT deleted: their URIs were rewritten by the proxy so
    // the only marker is the DATERANGE tag, and removing a live window's
    // fragments leaves hls.js waiting on a gap that never closes.
    expect(out).toContain('resource?id=ad000000');
    expect(out).toContain('resource?id=ad000001');
    expect(out).toContain(REWRITTEN_LIVE);
    expect(out).toContain('resource?id=abcd1235');
    expect(out.match(/#EXTINF/g)?.length).toBe(4);
    // The stitched-ad DATERANGE survives as the detection marker the loader
    // uses to count ad-tainted refreshes and rotate player types.
    expect(out).toContain('twitch-stitched-ad');
  });

  it('blanks TWITCH-PREFETCH lines when ads are detected even without stitched URIs', () => {
    const withPrefetch = rewrittenTwitchLivePlaylist.replace(
      '#EXT-X-MEDIA-SEQUENCE:0',
      '#EXT-X-MEDIA-SEQUENCE:0\n#EXT-X-TWITCH-PREFETCH:https://video-edge.ttvnw.net/raw/upstream.ts',
    );
    const out = stripTwitchAdSegments(withPrefetch);
    // The prefetch line carries a RAW upstream URL (never rewritten by the
    // proxy) — blanked so LL-HLS prefetch can't fetch ad segments directly.
    expect(out).not.toContain('#EXT-X-TWITCH-PREFETCH');
    expect(out).toContain(REWRITTEN_LIVE);
  });

  it('keeps LL-HLS PART tags inside the ad window (deletion would gap the timeline)', () => {
    const out = stripTwitchAdSegments(rewrittenLlhlsWithAd);
    expect(out).toContain('EXT-X-PART:');
    expect(out).toContain(REWRITTEN_LIVE);
    expect(out).toContain('resource?id=ad000000');
  });

  it('serves a rewritten full-takeover playlist unchanged (empty playlists kill hls.js)', () => {
    const out = stripTwitchAdSegments(rewrittenFullTakeover);
    expect(out).toBe(rewrittenFullTakeover);
    expect(out).toContain('resource?id=ad000000');
    expect(out).toContain('#EXTINF');
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

  it('leaves an all-ad direct playlist untouched (full-takeover guard — empty playlists break the player)', () => {
    const takeover = `#EXTM3U\n#EXTINF:6.000,ad\n${AD}\n#EXTINF:6.000,ad\nhttps://ad-segments.ttvnw.net/stitched/segment_ad_2.ts`;
    const out = stripTwitchAdSegments(takeover);
    expect(out).toBe(takeover);
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

describe('TwitchAdBlockLoader with rewritten/proxied playlists', () => {
  // ponytail: Promise.withResolvers is ES2024 but tsconfig targets ES2020.
  const loadOnce = (loader: TwitchAdBlockLoader, url = '/api/preview/hls/sid/master.m3u8'): Promise<string> =>
    new Promise((resolve, reject) => {
      loader.load({ url, type: 'manifest' }, {}, {
        onSuccess: ({ data }) => resolve(data),
        onError: () => reject(new Error('loader onError called')),
      });
    });

  it('serves a proxied ad playlist intact (detection-only — rotation handles live ads)', async () => {
    const originalFetch = window.fetch;
    window.fetch = (() => Promise.resolve({
      ok: true,
      text: () => Promise.resolve(rewrittenTwitchLivePlaylist),
    })) as unknown as typeof fetch;
    try {
      const loader = new TwitchAdBlockLoader(twitchAdBlockHlsConfig());
      const out = await loadOnce(loader);
      // Live window left intact — hls.js must keep a continuous timeline.
      expect(out).toContain('resource?id=ad000000');
      expect(out).toContain(REWRITTEN_LIVE);
    } finally {
      window.fetch = originalFetch;
    }
  });

  it('fires rotation after repeated ad-tainted refreshes (rewritten playlists are detection-only but still count)', async () => {
    const originalFetch = window.fetch;
    const rotated: string[] = [];
    const cfg = twitchAdBlockHlsConfig({
      onAdRotation: ({ url }) => rotated.push(url),
      rotationThreshold: 2,
      rotationCooldownMs: 60_000,
      maxRotations: 4,
    });
    const loader = new TwitchAdBlockLoader(cfg);
    window.fetch = (() => Promise.resolve({
      ok: true,
      text: () => Promise.resolve(rewrittenFullTakeover),
    })) as unknown as typeof fetch;
    try {
      await loadOnce(loader);
      expect(rotated).toEqual([]); // threshold 2 — first refresh only counts
      await loadOnce(loader);
      expect(rotated).toEqual(['/api/preview/hls/sid/master.m3u8']);
      await loadOnce(loader); // inside cooldown — no second rotation
      expect(rotated).toEqual(['/api/preview/hls/sid/master.m3u8']);
    } finally {
      window.fetch = originalFetch;
    }
  });

  it('holds the last known-good playlist during a full ad takeover, then recovers to clean content', async () => {
    const originalFetch = window.fetch;
    const cfg = twitchAdBlockHlsConfig({ onAdRotation: () => {}, rotationThreshold: 99 });
    const loader = new TwitchAdBlockLoader(cfg);
    const clean = `#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-MEDIA-SEQUENCE:0\n#EXTINF:2.000,live\n${REWRITTEN_LIVE}`;
    const responses = [clean, rewrittenFullTakeover, clean];
    let i = 0;
    window.fetch = (() => Promise.resolve({
      ok: true,
      text: () => Promise.resolve(responses[i++]),
    })) as unknown as typeof fetch;
    try {
      expect(await loadOnce(loader)).toBe(clean);
      // Full takeover: the all-ad playlist is never served — the last clean
      // window is held so hls.js keeps playing real segments (no levelEmpty).
      const held = await loadOnce(loader);
      expect(held).toBe(clean);
      expect(held).not.toContain('resource?id=ad000000');
      // Takeover ends: the next clean fetch is served and becomes the new hold.
      expect(await loadOnce(loader)).toBe(clean);
    } finally {
      window.fetch = originalFetch;
    }
  });
});
