// Kick Overlay popup — toggle + per-channel Kick/YouTube mapping + live status.
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
  // anonymous and reflect any Origin. v2 exposes playback_url TOP-LEVEL.
  const enc = encodeURIComponent(slug);
  for (const url of [
    `https://kick.com/api/v2/channels/${enc}`,
    `https://kick.com/api/v1/channels/${enc}`,
  ]) {
    try {
      const r = await fetch(url, { credentials: 'omit' });
      if (!r.ok) continue;
      const d = await r.json();
      if (d && d.playback_url) {
        return {
          live: true,
          viewers: d.viewer_count ?? null,
          title: (d.livestream && d.livestream.session_title) || '',
        };
      }
      if (d && d.livestream) return { live: false };
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
      $('status').innerHTML =
        `<b class="ok">Kick: LIVE</b>${s.viewers ? ` · ${s.viewers} viewers` : ''}${s.title ? ` · ${s.title}` : ''}`;
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
    st.enabled = e.target.checked;
    await writeState(st);
  });

  $('player').addEventListener('change', async (e) => {
    const v = e.target.value;
    st.player = v === 'twitch' ? 'twitch' : v === 'youtube' ? 'youtube' : 'kick';
    await writeState(st);
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
    if (!st.mappings) st.mappings = {};
    const prev = st.mappings[slug];
    const base = prev && typeof prev === 'object' ? prev : {};
    if (kick || yt) {
      const next = { ...base, kick: kick || base.kick || slug, yt: yt || base.yt || '' };
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
      st.mappings[slug] = next;
    } else {
      delete st.mappings[slug];
    }
    await writeState(st);
    window.close();
  });
})();
