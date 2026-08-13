/**
 * Chatterino-style custom chat emotes (BetterTTV + FrankerFaceZ + 7TV) —
 * render-only. The original message text is NEVER rewritten: the stored /
 * searchable `row.text` stays verbatim (text/semantic search indexes
 * messages.text), the split only happens at render time.
 *
 * Matching mirrors Chatterino: exact CASE-SENSITIVE whole-word lookup — the
 * message splits on whitespace and a token must equal an emote name. A
 * token that matches renders as an inline <img>; every other token stays
 * verbatim text (original whitespace preserved).
 *
 * Emote source: GET /api/chat/emotes?platform=twitch&slug={login} →
 * {"emotes": [{name, provider ("bttv"|"ffz"|"7tv"), url, global}]}, names
 * already deduped server-side by Chatterino priority (FFZ channel > BTTV
 * channel > 7TV channel > FFZ global > BTTV global > 7TV global). slug is
 * REQUIRED (missing → HTTP 400; there is no global-only mode), so the hook
 * skips the call entirely without a login.
 */
import { useEffect, useState } from 'react';

/** Emote name (verbatim token) → emote image URL. */
export type EmoteMap = Map<string, string>;

/** One renderable piece of a chat message: a verbatim text run (including
 *  whitespace separators) or a matched emote token. */
export type EmoteSegment = { text: string } | { emote: string; url: string };

const EMPTY_EMOTES: EmoteMap = new Map();
/** Module-level cache + in-flight dedupe keyed by twitch login. */
const emoteCache = new Map<string, EmoteMap>();
const emoteInflight = new Map<string, Promise<EmoteMap>>();

/**
 * Tokenize a chat message for emote rendering: split on whitespace (keeping
 * separators), exact case-sensitive whole-word match against `emotes`.
 * Non-matching tokens (and all whitespace) stay verbatim; concatenating the
 * segments reproduces the original text exactly.
 */
export function splitChatEmotes(text: string, emotes: EmoteMap): EmoteSegment[] {
  const out: EmoteSegment[] = [];
  for (const part of text.split(/(\s+)/)) {
    if (part === '') continue; // split's leading/trailing empties — nothing to render
    const url = emotes.get(part);
    if (url) out.push({ emote: part, url });
    else out.push({ text: part });
  }
  return out;
}

interface EmotesResponse {
  emotes?: Array<{ name?: string; provider?: string; url?: string; global?: boolean }>;
}

async function loadEmotes(slug: string): Promise<EmoteMap> {
  const q = new URLSearchParams({ platform: 'twitch', slug });
  const res = await fetch(`/api/chat/emotes?${q.toString()}`);
  if (!res.ok) throw new Error(`emote fetch failed: HTTP ${res.status}`);
  const data = (await res.json()) as EmotesResponse;
  const map: EmoteMap = new Map();
  for (const e of data.emotes ?? []) {
    if (e.name && e.url) map.set(e.name, e.url);
  }
  return map;
}

/**
 * Channel emotes for a chat surface. Twitch + login → fetches once per slug
 * (module-level cache; concurrent calls dedupe on one in-flight request).
 * Kick / youtube / unknown platform / missing login → empty map WITHOUT
 * fetching (the API requires a slug and has no global-only mode). Never
 * throws: errors/offline yield the empty map so chat never breaks.
 * ponytail: no TTL — one fetch per login per app run; upgrade path: a
 * short-lived cache or re-fetch on panel open if emote sets ever rotate
 * mid-session.
 */
export function useChatEmotes(
  platform: string | null | undefined,
  slug?: string | null,
): EmoteMap {
  const cacheKey = platform === 'twitch' && slug?.trim() ? slug.trim() : null;
  const [emotes, setEmotes] = useState<EmoteMap>(() =>
    cacheKey ? emoteCache.get(cacheKey) ?? EMPTY_EMOTES : EMPTY_EMOTES,
  );
  useEffect(() => {
    if (!cacheKey) {
      // Platform flipped away from twitch (or login dropped) — drop the map.
      setEmotes(EMPTY_EMOTES);
      return;
    }
    const cached = emoteCache.get(cacheKey);
    if (cached) {
      setEmotes(cached);
      return;
    }
    let cancelled = false;
    let p = emoteInflight.get(cacheKey);
    if (!p) {
      p = loadEmotes(cacheKey)
        .then((m) => {
          emoteCache.set(cacheKey, m);
          return m;
        })
        .catch(() => EMPTY_EMOTES) // offline/error → plain text, chat never breaks
        .finally(() => {
          emoteInflight.delete(cacheKey);
        });
      emoteInflight.set(cacheKey, p);
    }
    void p.then((m) => {
      if (!cancelled) setEmotes(m);
    });
    return () => {
      cancelled = true;
    };
  }, [cacheKey]);
  return emotes;
}

/** Renders a chat message with emotes: matched tokens become inline <img>s,
 *  everything else stays verbatim text. The message text is never mutated. */
export function ChatEmoteText({ text, emotes }: { text: string; emotes: EmoteMap }) {
  if (emotes.size === 0) {
    // ponytail: single span when there are no emotes — keeps rows cheap and
    // the DOM text contiguous (tests/copy see the whole message).
    return <span>{text}</span>;
  }
  return (
    <>
      {splitChatEmotes(text, emotes).map((seg, i) =>
        'emote' in seg ? (
          <img
            key={i}
            src={seg.url}
            alt={seg.emote}
            title={seg.emote}
            className="inline-block h-[18px] align-[-4px]"
            loading="lazy"
          />
        ) : (
          <span key={i}>{seg.text}</span>
        ),
      )}
    </>
  );
}
