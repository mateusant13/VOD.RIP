/**
 * Channel content filter vocabulary - one place that decides what "Shorts"
 * means and which rows belong to it.
 *
 * WHY THIS EXISTS: the channel UI used to present ONE `clips` state under two
 * different labels - "Shorts" when YouTube was the only platform, "Clips" in
 * multi-platform mode - and returned the whole `clipVideos` pile unfiltered in
 * both cases. YouTube shorts and Kick/Twitch clips therefore landed in one
 * list with no way to separate them. The fix is a fourth filter, `shorts`, and
 * this module is where its two rules live.
 *
 * RULE 1 - THE URL IS THE ONLY DISCRIMINATOR, AND IT WINS.
 * `ChannelVideo.content_kind` is typed `'vod' | 'clip' | 'stream'` (types.ts):
 * there is no `'short'` member. The backend's clips payload tags EVERY row
 * `content_kind: "clip"` - see backend/routers/channels.py, which builds the
 * merged payload from `_fetch_youtube_shorts` (playlist="shorts") PLUS the
 * Twitch and Kick clip crawls, and stamps `"content_kind": "clip"` on each.
 * So for a YouTube short and a Twitch clip the stored content_kind is
 * BYTE-IDENTICAL, and no content_kind rule can ever separate them. A Shorts
 * filter keyed on content_kind would show every Twitch clip in the app.
 *
 * That matches the backend's own ranking: services/youtube_service.py
 * classifies with the comment "URL pattern (strongest signal for shorts)" and
 * tests `"/shorts/" in url` FIRST, before live status, duration and playlist
 * source. This module uses the same signal, host-anchored the same way
 * archiveScope.ts:32 anchors it, so a row is a Short if and only if it is a
 * YouTube URL whose path starts with `/shorts/`.
 *
 * When the two signals disagree, the URL wins:
 *   - URL `/shorts/...` + content_kind 'clip'  -> SHORT (this is the normal
 *     case for every YouTube short; 'clip' here means "short clip listing",
 *     not "a clip from a clips tab").
 *   - URL without `/shorts/` + content_kind 'clip' -> CLIP (a real Twitch/Kick
 *     clip, or a YouTube clip from a clips tab).
 *   - URL without `/shorts/` + content_kind 'vod'  -> neither.
 *
 * RULE 2 - `shorts` IS A CLIENT-SIDE VIEW OVER THE `clips` PAYLOAD.
 * The backend normalises its `content` parameter and only knows three values
 * (backend/routers/channels.py: `is_clips = content_norm == "clips"`, then a
 * `streams` branch, then vods as the fall-through). Sending `content=shorts`
 * would therefore fetch VODS, not shorts. Shorts live in the shorts playlist
 * that the `clips` branch already crawls, so `shorts` asks the backend for
 * `clips` and splits the response here. `backendContentForFilter` is the one
 * place that translation happens, so the vocabulary difference is stated once
 * instead of being re-derived at each call site.
 */

/** Every value the channel content filter can hold. */
export const CHANNEL_CONTENT_FILTERS = ['vods', 'clips', 'streams', 'shorts'] as const;

export type ChannelContentFilter = (typeof CHANNEL_CONTENT_FILTERS)[number];

/** The filter the UI starts on when nothing valid is persisted. */
export const DEFAULT_CHANNEL_CONTENT_FILTER: ChannelContentFilter = 'vods';

/**
 * Type guard for a persisted/untrusted filter value.
 *
 * Both persistence paths feed this: the `channel_content_filter` setting and
 * the `vodrip_channel_ui` localStorage blob. Both previously enumerated their
 * accepted values inline, so adding `shorts` without editing them made the
 * value silently revert to `vods` on reload - a filter that looks like it
 * works and is not persisted at all.
 */
export function isChannelContentFilter(value: unknown): value is ChannelContentFilter {
  return (
    typeof value === 'string' &&
    (CHANNEL_CONTENT_FILTERS as readonly string[]).includes(value)
  );
}

/**
 * Coerce an untrusted persisted value to a real filter, falling back to
 * `vods`. This is what keeps `shorts` (and any future value) across a reload.
 */
export function normalizeChannelContentFilter(value: unknown): ChannelContentFilter {
  return isChannelContentFilter(value) ? value : DEFAULT_CHANNEL_CONTENT_FILTER;
}

/** Path segment that marks a YouTube Short. */
const SHORTS_PATH = '/shorts/';

/** Minimal shape needed to classify - keeps this testable without a full row.
 *
 * content_kind is carried (and deliberately NOT read) because that is the
 * shape production rows actually have: ChannelContentRow is this type
 * intersected with that field, and the classifier is called with rows. The
 * URL is the only discriminator, so a disagreeing content_kind must change
 * nothing - which is only testable if the parameter accepts the field at all.
 */
export type ShortsCandidate = { url?: string | null; content_kind?: string | null };

/**
 * True when the row is a YouTube Short.
 *
 * Host-anchored, like archiveScope.ts:32: a `youtu.be/<id>` link is the short
 * form of a WATCH url, never a shorts url, and a non-YouTube host that merely
 * contains "/shorts/" in its path is not a YouTube Short. A row whose url is
 * missing or unparseable is NOT a short - it falls back to the Clips list,
 * which is where such a row already appeared, so an odd url can never make the
 * Shorts filter quietly drop content.
 */
export function isShortsVideo(v: ShortsCandidate): boolean {
  const raw = (v.url ?? '').trim();
  if (!raw) return false;
  let u: URL;
  try {
    u = new URL(raw);
  } catch {
    return false;
  }
  const host = u.hostname.toLowerCase();
  const isYouTube = host === 'youtube.com' || host.endsWith('.youtube.com');
  if (!isYouTube) return false;
  return u.pathname.startsWith(SHORTS_PATH);
}

/**
 * Split one cached list into its Shorts and its genuine Clips.
 * The two outputs are disjoint and together cover the input, which is what
 * keeps a Short out of the Clips filter instead of showing it twice.
 */
export function splitShortsAndClips<T extends ShortsCandidate>(list: readonly T[]): {
  shorts: T[];
  clips: T[];
} {
  const shorts: T[] = [];
  const clips: T[] = [];
  for (const item of list) {
    if (isShortsVideo(item)) shorts.push(item);
    else clips.push(item);
  }
  return { shorts, clips };
}

/** The `content` value to send the backend for a given UI filter. */
export type BackendContentFilter = 'vods' | 'clips' | 'streams';

export function backendContentForFilter(filter: ChannelContentFilter): BackendContentFilter {
  // Shorts are served by the clips payload; see RULE 2 above.
  if (filter === 'clips' || filter === 'shorts') return 'clips';
  return filter;
}

/**
 * True for the filters whose rows come from the clips payload and therefore
 * share its fetch, paging, missing-cache and has-more bookkeeping.
 * `streams` is the opposite: its own tab, its own page counter.
 */
export function isClipLikeFilter(filter: ChannelContentFilter): boolean {
  return filter === 'clips' || filter === 'shorts';
}

/** Structural minimum the selector needs. `ChannelVideo` satisfies it. */
export type ChannelContentRow = ShortsCandidate & { content_kind?: string | null };

/**
 * The one place that turns a filter into a row list.
 *
 * This is the body of App's `allChannelVideos` memo, extracted so the Shorts /
 * Clips split is a real unit under test rather than something only reachable by
 * mounting the whole app. The caller passes lists that are ALREADY filtered for
 * public visibility (members-only rows are dropped at the source, see
 * `allChannelVideos`), so this function never re-applies that policy.
 *
 * `keyOf` supplies the dedupe identity for the Shorts union.
 */
export function selectChannelContent<T extends ChannelContentRow>(
  filter: ChannelContentFilter,
  lists: { clipVideos: T[]; vodVideos: T[] },
  opts: { youtubePlatformOnly: boolean; keyOf: (v: T) => string },
): T[] {
  const { clipVideos, vodVideos } = lists;
  if (filter === 'shorts') {
    // Also consider vodVideos: a Short cached by an older /videos fetch would
    // otherwise be invisible on the Shorts tab, which reads as data loss.
    const { shorts: fromClips } = splitShortsAndClips(clipVideos);
    const { shorts: fromVods } = splitShortsAndClips(vodVideos);
    const seen = new Set(fromClips.map(opts.keyOf));
    return [...fromClips, ...fromVods.filter((v) => !seen.has(opts.keyOf(v)))];
  }
  if (filter === 'clips') {
    // Shorts are excluded here, not duplicated: one cached list, two filters.
    return splitShortsAndClips(clipVideos).clips;
  }
  if (filter === 'streams') {
    return vodVideos.filter((v) => v.content_kind === 'stream');
  }
  // Multi-platform UI: recorded YouTube broadcasts (kind 'stream') belong
  // in the channel's VOD list — the /streams tab content is now merged
  // into the vods fetch. YouTube-only mode keeps them out of "Videos"
  // because its dedicated "VODs" tab shows them.
  return vodVideos.filter((v) =>
    opts.youtubePlatformOnly
      ? v.content_kind !== 'stream' && v.content_kind !== 'clip'
      : v.content_kind !== 'clip',
  );
}

