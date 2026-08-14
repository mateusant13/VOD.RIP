// Kick Overlay — YouTube IFrame API bridge (PAGE WORLD).
// Injected as a <script> tag by content.js. The content script (isolated
// world) cannot touch window.YT, so this bridge owns the API instance and
// is driven by window.postMessage. It creates the player for the 'ko-yt'
// iframe (polling until the iframe exists, so injection order is free) and
// reports state back via postMessage.
'use strict';
(function () {
  if (window.__koYtBridge) return;
  window.__koYtBridge = true;

  var player = null;
  var apiTried = false;

  function post(msg) {
    try {
      window.postMessage(Object.assign({ __koYt: true }, msg), '*');
    } catch (e) {
      /* ignore */
    }
  }

  function status() {
    if (!player || !window.YT) return null;
    try {
      var d = player.getVideoData() || {};
      return {
        playing: player.getPlayerState() === 1,
        muted: player.isMuted(),
        live: !!d.video_id,
        dur: player.getDuration() || 0,
        ct: player.getCurrentTime() || 0,
        vq: player.getPlaybackQuality ? player.getPlaybackQuality() : '',
      };
    } catch (e) {
      return null;
    }
  }

  function makePlayer() {
    if (player || !window.YT || !window.YT.Player) return;
    var el = document.getElementById('ko-yt');
    if (!el) return;
    try {
      player = new window.YT.Player('ko-yt', {
        events: {
          onReady: function () {
            post({ t: 'ready' });
            var s = status();
            if (s) post(Object.assign({ t: 'status' }, s));
          },
          onStateChange: function () {
            var s = status();
            if (s) post(Object.assign({ t: 'status' }, s));
          },
          onError: function (e) {
            post({ t: 'error', c: e && e.data ? e.data : 0 });
          },
        },
      });
    } catch (e) {
      player = null;
    }
  }

  function loadApi() {
    if (apiTried) return;
    apiTried = true;
    if (window.YT && window.YT.Player) {
      makePlayer();
      return;
    }
    window.onYouTubeIframeAPIReady = makePlayer;
    var tag = document.createElement('script');
    tag.src = 'https://www.youtube.com/iframe_api';
    document.head.appendChild(tag);
  }

  window.addEventListener('message', function (ev) {
    var d = ev && ev.data;
    if (!d || !d.__koYtCmd) return;
    var cmd = d.__koYtCmd;
    if (cmd === 'destroy') {
      if (player) {
        try {
          player.destroy();
        } catch (e) {
          /* ignore */
        }
        player = null;
      }
      return;
    }
    if (!player) return;
    try {
      switch (cmd) {
        case 'play':
          player.playVideo();
          break;
        case 'pause':
          player.pauseVideo();
          break;
        case 'mute':
          player.mute();
          break;
        case 'unmute':
          player.unMute();
          break;
        case 'live':
          player.seekTo(player.getDuration(), true);
          break;
        case 'status':
          var s = status();
          if (s) post(Object.assign({ t: 'status' }, s));
          break;
      }
    } catch (e) {
      /* ignore */
    }
  });

  // Poll for the iframe (created by the content script) and the API.
  setInterval(function () {
    if (!player && document.getElementById('ko-yt')) loadApi();
  }, 300);

  // Periodic status so the content script can detect live<->offline
  // transitions and the behind-the-edge state.
  setInterval(function () {
    if (!player) return;
    var s = status();
    if (s) post(Object.assign({ t: 'status' }, s));
  }, 2000);

  loadApi();
})();
