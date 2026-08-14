// Kick Overlay — service worker.
// Resolves a YouTube channel URL (@handle / channel/UC... / bare UC...) to
// its UC... channel id by fetching the channel page (host_permission grants
// the cross-origin fetch the content script does not have). The live embed
// (youtube.com/embed/live_stream) only accepts the UC... id, not handles.
'use strict';

const YT_ID_RE = /UC[0-9A-Za-z_-]{22}/;

function normalizeYtUrl(value) {
  let v = (value || '').trim();
  if (!v) return null;
  if (YT_ID_RE.test(v) && v.startsWith('UC')) return `https://www.youtube.com/channel/${v}`;
  const m = v.match(/youtube\.com\/(@[^/?#]+|channel\/UC[0-9A-Za-z_-]{22}|c\/[^/?#]+|user\/[^/?#]+)/i);
  if (m) return `https://www.youtube.com/${m[1]}`;
  if (v.startsWith('@')) return `https://www.youtube.com/${v}`;
  // Bare handle without @ (e.g. "JBSniperPRIME") — the popup hint says
  // "URL, @handle or UC…", but users type the plain name; resolve it anyway.
  if (/^[0-9A-Za-z._-]{2,64}$/.test(v)) return `https://www.youtube.com/@${v}`;
  return null;
}

// ---- YouTube live HLS bypass ------------------------------------------------
// The live_stream embed (and the IFrame API) cannot initialize on bot-gated /
// cookieless sessions: the server bakes an error config ("Erro 153") into the
// embed page and the player never issues an innertube player request — no pot
// in the URL can fix that. Instead we replicate what yt-dlp does for live
// streams: an anonymous MWEB client player API call (full device context +
// the page's signature timestamp) returns streamingData.hlsManifestUrl, which
// the extension's own player page (player.html?m=hls) plays with hls.js.
// Verified 2026-08-14 on this bot-gated IP: MWEB player call from Chrome
// transport → hlsManifestUrl → manifest 200 #EXTM3U → ffmpeg pulls real
// segments. The pot (bgutil, content-bound to the video) is NOT needed on the
// player call — it rides the GVS manifest fetch, which works bare here.
const KO_MWEB_TPL = {
  context: {
    client: {
      hl: 'pt',
      gl: 'BR',
      deviceMake: 'Apple',
      deviceModel: 'iPad',
      userAgent: 'Mozilla/5.0 (iPad; CPU OS 16_7_10 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1,gzip(gfe),gzip(gfe)',
      clientName: 'MWEB',
      osName: 'iPad',
      osVersion: '16_7_10',
      originalUrl: 'https://m.youtube.com/',
      playerType: 'UNIPLAYER',
      screenPixelDensity: 2,
      platform: 'TABLET',
      clientFormFactor: 'LARGE_FORM_FACTOR',
      configInfo: {
        appInstallData: 'CIDN_tMGEIbhgBMQmpXSHBCDh7giEPfEgBMQt_iAExC0kdAcEK6R0hwQvoqwBRAAELnw0RwQ8bTQHBCu1s8cEMn2gBMQ8N7RHBCVrNAcEJ_PgBMQ0Yi4IhDNiLgiEIHNzhwQvbauBRCkqdEcEKynsQUQofjQHBCkodIcEMGP0BwQndCwBRCgr9IcEPb30RwQgo_PHBDA4K4FEIfUrwUQyvuAExCAg9IcELjkzhwQ6fjRHBCL988cEMqIuCIQ_LLOHBDTotEcELTB0BwQ9quwBRDxnLAFEMzfrgUQ3NLQHBC8pNAcEOKHuCIQ-qrSHBCHrM4cEIiQ0RwQ3rzOHBCJsM4cEL6E0RwQ7KzSHBDKk9IcEJmNsQUQvZmwBRDH_IATEOfr0RwQ4LfRHBCZn9IcENr3zhwQ1faAExCynf8SEPTp0RwQk4iBExCI5NEcEOzN0BwqiAFDQU1TWUJWYi1acS1ETWVVRXBVQ25BNzVGWjBGNFJhTTBBX2ZEZHdHNUFiVEFnRDF4T2NMZ2EwS2gwd3lvS3dFQTVDdUFfc210dVFHZ0JYd3J3VHBFNWg4LV9NRzBNVU8zWmdHeDhZRmlGeloxUVhkTE9WR2xOZ0d1MUtHOHVJZTJ3WWRCdz09MAA%3D'
      },
      screenDensityFloat: 2.0,
      userInterfaceTheme: 'USER_INTERFACE_THEME_LIGHT',
      timeZone: 'UTC',
      browserName: 'Safari Mobile',
      browserVersion: '16.6.15E148',
      acceptHeader: 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
      deviceExperimentId: 'ChxOelkzTkRBek5UTTFPVEkyTURNeU5qZzRNUT09EIDN_tMGGIDN_tMG',
      rolloutToken: 'CMbK7-OY9KSqXxCSnrzSpKGWAxjLo5TTpKGWAw%3D%3D',
      utcOffsetMinutes: 0
    },
    user: { lockedSafetyMode: false },
    request: { useSsl: true },
    clickTracking: {}
  },
  racyCheckOk: true,
  contentCheckOk: true
};
// The MWEB player API rejects the call without the page's signature timestamp
// (sts) — observed UNPLAYABLE "A página precisa ser atualizada" without it.
const KO_STS_RE = /"sts":(\d+)/;
const KO_VD_RE = /"visitorData":"([^"]+)"/;
const KO_VER_RE = /"INNERTUBE_CONTEXT_CLIENT_VERSION":"([^"]+)"/;
const KO_VID_RE = /[?&]v=([0-9A-Za-z_-]{11})/;

async function koFetchText(url, opts) {
  const r = await fetch(url, opts);
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  return r.text();
}

// The channel's /live page redirects to the running stream's watch URL; its
// canonical link carries the live video id.
async function koLiveVideoId(channelRef) {
  const base = channelRef.startsWith('UC')
    ? `https://www.youtube.com/channel/${channelRef}/live`
    : `https://www.youtube.com/${channelRef}/live`;
  const r = await fetch(base, { credentials: 'omit', redirect: 'follow' });
  if (!r.ok) return null;
  const m = r.url.match(KO_VID_RE);
  if (m) return m[1];
  const html = await r.text();
  const c = html.match(/<link rel="canonical" href="[^"]*watch\?v=([0-9A-Za-z_-]{11})/);
  return c ? c[1] : null;
}

// Cache the resolved manifest per video id — the URL is valid for hours and
// re-resolving on every probe would hammer innertube.
const KO_HLS_CACHE = new Map(); // videoId -> { url, at }
const KO_HLS_TTL_MS = 5 * 60 * 1000;

async function koMintManifest(videoId) {
  const cached = KO_HLS_CACHE.get(videoId);
  if (cached && Date.now() - cached.at < KO_HLS_TTL_MS) return cached.url;
  // 1) MWEB config: fresh visitorData + clientVersion (rotates daily).
  const cfg = await koFetchText('https://m.youtube.com/?hl=pt&gl=BR');
  const vd = (cfg.match(KO_VD_RE) || [])[1];
  const ver = (cfg.match(KO_VER_RE) || [])[1];
  if (!vd || !ver) throw new Error('mweb config parse fail');
  // 2) Signature timestamp from the watch page ytcfg.
  const watch = await koFetchText(`https://www.youtube.com/watch?v=${videoId}`);
  const sts = (watch.match(KO_STS_RE) || [])[1];
  if (!sts) throw new Error('sts not found');
  // 3) MWEB player API call (anonymous, Chrome transport).
  const body = JSON.parse(JSON.stringify(KO_MWEB_TPL));
  body.context.client.visitorData = vd;
  body.context.client.clientVersion = ver;
  body.playbackContext = { contentPlaybackContext: { html5Preference: 'HTML5_PREF_WANTS', signatureTimestamp: Number(sts) } };
  body.videoId = videoId;
  const pr = await fetch('https://www.youtube.com/youtubei/v1/player?prettyPrint=false', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Youtube-Client-Name': '2', 'X-Youtube-Client-Version': ver },
    body: JSON.stringify(body)
  });
  const j = await pr.json();
  const url = j && j.streamingData && j.streamingData.hlsManifestUrl;
  if (!url) throw new Error(`player ${(j && j.playabilityStatus && j.playabilityStatus.status) || pr.status}`);
  KO_HLS_CACHE.set(videoId, { url, at: Date.now() });
  return url;
}

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg && msg.type === 'ko-yt-play') {
    // channelRef: UC… id or @handle (the mapped yt value or resolved id).
    koLiveVideoId(msg.channelRef || '')
      .then((videoId) => (videoId ? koMintManifest(videoId) : Promise.reject(new Error('no live video'))))
      .then((url) => sendResponse({ url }))
      .catch((e) => sendResponse({ error: String((e && e.message) || e).slice(0, 120) }));
    return true; // async sendResponse
  }
  if (msg && msg.__koDiag) {
    // Diagnostics relay: the content script's [ko] state mirrored to the
    // local diag listener (http://127.0.0.1:9234). An extension-origin
    // no-cors fetch is neither CORS- nor CSP-blocked, so no host_permission
    // is needed. The server answers with an opaque 204; failures are
    // swallowed on purpose.
    // ponytail: debug-only channel; remove once the kick black-screen is
    // root-caused (2026-08-13).
    try {
      const e = encodeURIComponent(String(msg.__koDiag.ev || 'ev'));
      const d = encodeURIComponent(JSON.stringify(msg.__koDiag.data || {}));
      fetch(`http://127.0.0.1:9234/d?e=${e}&d=${d}`, { mode: 'no-cors' }).catch(() => {});
    } catch {
      /* relay must never break the extension */
    }
    sendResponse({ ok: true });
    return false;
  }
  if (!msg || msg.type !== 'ko-resolve-yt') return;
  const url = normalizeYtUrl(msg.value);
  if (!url) {
    sendResponse({ id: null, error: 'bad-url' });
    return;
  }
  // credentials 'omit': the anonymous page carries the UC id (curl-verified);
  // the logged-in variant can redirect to the consent wall and break the match.
  fetch(url, { credentials: 'omit' })
    .then((r) => (r.ok ? r.text() : Promise.reject(new Error(`HTTP ${r.status}`))))
    .then((html) => {
      const m =
        html.match(/"externalId":"(UC[0-9A-Za-z_-]{22})"/) ||
        html.match(/"channelId":"(UC[0-9A-Za-z_-]{22})"/) ||
        html.match(/"browseId":"(UC[0-9A-Za-z_-]{22})"/);
      sendResponse({ id: m ? m[1] : null, error: m ? null : 'not-found' });
    })
    .catch((e) => sendResponse({ id: null, error: String(e).slice(0, 80) }));
  return true; // async sendResponse
});
