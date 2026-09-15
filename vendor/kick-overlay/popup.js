// Kick Overlay popup — toggle + per-channel Kick/YouTube mapping + live status.
'use strict';

const KEY = 'ko.v2';
const $ = (id) => document.getElementById(id);

async function readState() {
  try {
    const o = await chrome.storage.local.get(KEY);
    return o[KEY] || {};
  } catch {
    return {};
  }
}

async function writeState(s) {
  try {
    await chrome.storage.local.set({ [KEY]: s });
  } catch {
    /* storage busy — popup state is best-effort */
  }
}

function twitchSlugFromUrl(url) {
  if (!url) return null;
  const m = url.match(/twitch\.tv\/([^/?#]+)/i);
  return m ? m[1].toLowerCase() : null;
}

async function kickStatus(slug) {
  // Same v2→v1 fallback and liveness rule as the content script (kickPlaybackUrl):
  // LIVE only when the livestream object exists with a playback_url (nested or
  // top-level). The top-level playback_url alone is NOT live — kick's v2 API
  // returns a stale one for OFFLINE channels (proven with nyro), and the popup
  // must not report "Kick: LIVE" while the content script shows the channel off.
  const enc = encodeURIComponent(slug);
  for (const url of [
    `https://kick.com/api/v2/channels/${enc}`,
    `https://kick.com/api/v1/channels/${enc}`,
  ]) {
    try {
      const r = await fetch(url, { credentials: 'omit' });
      if (!r.ok) continue;
      const d = await r.json();
      const ls = d && d.livestream;
      if (ls) {
        if (ls.playback_url || d.playback_url) {
          return {
            live: true,
            viewers: d.viewer_count ?? null,
            title: (ls.session_title) || '',
          };
        }
        return { live: false };
      }
    } catch {
      /* try next */
    }
  }
  return { live: null };
}

(async function init() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  const slug = twitchSlugFromUrl(tab && tab.url);
  const st = await readState();

  $('channel').textContent = slug ? `twitch.tv/${slug}` : 'Not on a Twitch channel';
  $('enabled').checked = st.enabled === undefined ? true : !!st.enabled;
  $('player').value = st.player === 'twitch' ? 'twitch' : st.player === 'youtube' ? 'youtube' : 'kick';
  // Popup is INERT off Twitch: no state writes, no player switches, no delete.
  const offTwitch = !slug;
  $('enabled').disabled = offTwitch;
  $('player').disabled = offTwitch;
  if (offTwitch) $('status').textContent = 'Abra uma página da Twitch para controlar o overlay.';
  const del = $('del');
  if (slug && tab && tab.id) {
    del.style.display = 'block';
    del.addEventListener('click', async () => {
      try {
        await chrome.tabs.sendMessage(tab.id, { type: 'ko-delete-twitch' });
        $('status').textContent = 'Player da Twitch removido — recarregue a página para restaurar.';
      } catch {
        $('status').textContent = 'Recarregue a página da Twitch primeiro (extensão não injetada).';
      }
    });
  }
  const m = slug ? (st.mappings && st.mappings[slug]) : undefined;
  const kickSlug = typeof m === 'string' ? m : m ? m.kick || '' : '';
  const ytVal = m && typeof m === 'object' ? m.yt || '' : '';
  if (slug) $('kick').value = kickSlug;
  $('yt').value = ytVal;
  $('kick').disabled = !slug;
  $('yt').disabled = !slug;

  if (slug && kickSlug) {
    const s = await kickStatus(kickSlug);
    if (s.live === true) {
      // session_title is set by the kick streamer — attacker-controlled;
      // build the status with text nodes, never innerHTML.
      const el = $('status');
      el.innerHTML = '';
      const b = document.createElement('b');
      b.className = 'ok';
      b.textContent = 'Kick: LIVE';
      el.appendChild(b);
      if (s.viewers) el.append(` · ${s.viewers} viewers`);
      if (s.title) el.append(` · ${s.title}`);
    } else if (s.live === false) {
      $('status').innerHTML = '<b class="off">Kick: offline</b>';
    } else {
      $('status').textContent = 'Kick: unreachable';
    }
  } else if (slug && ytVal) {
    $('status').textContent = 'YouTube: mapped (status shown on the page)';
  }

  // Storage writes hot-apply in the content script via storage.onChanged —
  // no page reload needed.
  $('enabled').addEventListener('change', async (e) => {
    const fresh = await readState();
    fresh.enabled = e.target.checked;
    await writeState(fresh);
  });

  $('player').addEventListener('change', async (e) => {
    const v = e.target.value;
    // Re-read immediately before writing: the popup-open st snapshot goes
    // stale the moment another tab or the overlay-side saveState() writes
    // (mute/progress), and writing the whole stale object would clobber that
    // back (P5) — and can manufacture a player === prev.player echo that the
    // content script's retry gate must then survive. Merge onto the freshest.
    const fresh = await readState();
    fresh.player = v === 'twitch' ? 'twitch' : v === 'youtube' ? 'youtube' : 'kick';
    // 0.8.6 pain-4: an explicit popup re-pick must read as retry intent even
    // when the value is unchanged since the last write (a same-value write
    // after a session-only yt->kick fallback must re-mint). fresh.playerAt
    // is the intent marker the content script keys on; nothing else (volume/
    // mapping echoes, the overlay's own saveState) ever stamps it. A user
    // who re-picks YouTube immediately — the natural reaction to a failed
    // hand-back — is no longer swallowed by a 30s backoff.
    fresh.playerAt = Date.now();
    await writeState(fresh);
    if (v === 'youtube' && !$('yt').value.trim()) {
      $('status').textContent = 'YouTube needs a channel: paste URL, @handle or UC… above.';
    } else {
      $('status').textContent = 'Applied — switch anytime from the player.';
    }
  });

  $('save').addEventListener('click', async () => {
    if (!slug) return;
    const kick = $('kick').value.trim().toLowerCase();
    const yt = $('yt').value.trim();
    // Re-read AFTER any pending write (the player select above, another tab's
    // saveState) — the popup-open `st` snapshot (which carries st.player) goes
    // stale the moment anything writes; mutating and writing it would clobber
    // the fresh player back AND manufacture the same-value echo the retry gate
    // exists to survive (0.8.6 pain-4 / P5). Mutate ONLY the mapping fields.
    const fresh = await readState();
    if (!fresh.mappings) fresh.mappings = {};
    const prev = fresh.mappings[slug];
    const base = prev && typeof prev === 'object' ? prev : {};
    if (kick || yt) {
      // Clearing a field must clear it (the old `kick || base.kick` kept a
      // stale mapping the user deleted); empty kick = default to the Twitch
      // slug, exactly like the content script's `m.kick || slug` fallback.
      const next = { ...base, kick: kick || slug, yt: yt || '' };
      // Resolve the YouTube handle/URL to its UC... id now, so the content
      // script never has to wait for a fetch when switching to YouTube.
      if (next.yt) {
        try {
          const r = await chrome.runtime.sendMessage({ type: 'ko-resolve-yt', value: next.yt });
          if (r && r.id) next.ytId = r.id;
          else delete next.ytId;
        } catch {
          delete next.ytId;
        }
      } else {
        delete next.ytId;
      }
      fresh.mappings[slug] = next;
    } else {
      delete fresh.mappings[slug];
    }
    await writeState(fresh);
    window.close();
  });
})();
