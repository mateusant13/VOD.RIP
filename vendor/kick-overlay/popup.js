// Kick Overlay popup — toggle + per-channel Twitch→Kick mapping + live status.
'use strict';

const KEY = 'ko.v2';
const $ = (id) => document.getElementById(id);

async function readState() {
  const o = await chrome.storage.local.get(KEY);
  return o[KEY] || {};
}

async function writeState(s) {
  await chrome.storage.local.set({ [KEY]: s });
}

function twitchSlugFromUrl(url) {
  if (!url) return null;
  const m = url.match(/twitch\.tv\/([^/?#]+)/i);
  return m ? m[1].toLowerCase() : null;
}

async function kickStatus(slug) {
  // Same v2→v1 fallback as the content script; both endpoints are
  // anonymous and reflect any Origin.
  const enc = encodeURIComponent(slug);
  for (const [url, v2] of [
    [`https://kick.com/api/v2/channels/${enc}`, true],
    [`https://kick.com/api/v1/channels/${enc}`, false],
  ]) {
    try {
      const r = await fetch(url, { credentials: 'omit' });
      if (!r.ok) continue;
      const d = await r.json();
      if (v2) {
        const ls = d && d.livestream;
        if (ls && ls.playback_url) {
          return {
            live: true,
            viewers: ls.viewer_count ?? null,
            title: ls.session_title ?? '',
          };
        }
        if (!ls) return { live: false };
      } else if (d && d.playback_url) {
        return { live: true, viewers: d.viewer_count ?? null, title: (d.livestream && d.livestream.session_title) || '' };
      } else {
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
  $('player').value = st.player === 'twitch' ? 'twitch' : 'kick';
  if (slug) {
    $('kick').value = (st.mappings && st.mappings[slug]) || '';
  }
  $('kick').disabled = !slug;

  if (slug && st.mappings && st.mappings[slug]) {
    const s = await kickStatus(st.mappings[slug]);
    if (s.live === true) {
      $('status').innerHTML =
        `<b class="ok">Kick: LIVE</b>${s.viewers ? ` · ${s.viewers} viewers` : ''}${s.title ? ` · ${s.title}` : ''}`;
    } else if (s.live === false) {
      $('status').innerHTML = '<b class="off">Kick: offline</b>';
    } else {
      $('status').textContent = 'Kick: unreachable';
    }
  }

  // Storage writes hot-apply in the content script via storage.onChanged —
  // no page reload needed.
  $('enabled').addEventListener('change', async (e) => {
    st.enabled = e.target.checked;
    await writeState(st);
  });

  $('player').addEventListener('change', async (e) => {
    st.player = e.target.value === 'twitch' ? 'twitch' : 'kick';
    await writeState(st);
  });

  $('save').addEventListener('click', async () => {
    const kickSlug = $('kick').value.trim().toLowerCase();
    if (!slug) return;
    if (!st.mappings) st.mappings = {};
    if (kickSlug) {
      st.mappings[slug] = kickSlug;
    } else {
      delete st.mappings[slug];
    }
    await writeState(st);
    window.close();
  });
})();
