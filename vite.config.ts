import path from "path";
import { fileURLToPath } from "url";
import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
/// <reference types="vitest" />
import type { ProxyOptions } from "vite";
import { defineConfig } from "vitest/config";
import { viteSingleFile } from "vite-plugin-singlefile";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

const API_PORT = process.env.PORT || "7897";

// https://vite.dev/config/
export default defineConfig({
  plugins: [react(), tailwindcss(), viteSingleFile()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "src"),
    },
  },
  test: {
    globals: true,
    environment: "jsdom",
    setupFiles: ["./src/test-setup.ts"],
    include: ["src/**/*.{test,spec}.{ts,tsx}"],
    // The 5s vitest default is a deadline, not an assertion. On this box a
    // 67-file run in parallel makes correct tests cross it (a 600-row render, a
    // sweep that walks two full passes), and the failure then names a different
    // file on every run: measured across three full runs, the failing set was
    // {ArchiveSearchPopupAgeParked, PreviewChatPanel, PreviewChatPanelParked},
    // then {PreviewChatPanel x2, PreviewChatPanelParked}, then
    // {LivePlayerPopup, SettingsTab} - every one of them "Test timed out in
    // 5000ms" or a waitFor that ran out of its 1s budget. 20s keeps a genuinely
    // hung test a failure while removing the load-sensitivity.
    testTimeout: 20_000,
    hookTimeout: 20_000,
    // This box has 20 logical processors and is SHARED: the owner plays League
    // of Legends while the dev server runs. vitest's default is one worker per
    // core, so a full run oversubscribes a machine already at the governor's
    // limit - measured: three back-to-back full runs took 161-193s with
    // environment time ballooning to 1493s (vs 403s in the gate run), and the
    // pool gave up on its own worker ("[vitest-pool]: Timeout terminating forks
    // worker"). Four workers keep every test running while removing the
    // self-inflicted starvation that turned correct lookups into failures.
    maxWorkers: 4,
  },
  server: {
    host: "localhost",
    port: 5173,
    strictPort: true,
    // No server.warmup here: Vite warms clientFiles SEQUENTIALLY (measured
    // ~10s/file under AV scan → 10-15min for this graph), blocking first
    // requests the whole time. dev-all.mjs prewarms source files with
    // parallel first-touch reads instead — transforms then cost 1-24ms.
    proxy: (() => {
      const base: ProxyOptions = {
        target: `http://127.0.0.1:${API_PORT}`,
        changeOrigin: true,
        configure(proxy) {
          // The API binds in ~2s warm (~5-10s on a cold page cache / first
          // boot of the day); the UI polls /api immediately on load, so a
          // transient ECONNREFUSED during that window is expected — not
          // "not running". Stay quiet for the first 15s, then report real
          // failures (deduped — the UI polls every few seconds).
          const bootGraceMs = 15_000;
          const startedAt = Date.now();
          let graceLogged = false;
          let lastWarnedAt = 0;
          proxy.on("error", (err, req) => {
            const code = "code" in err ? (err as NodeJS.ErrnoException).code : "";
            if (Date.now() - startedAt < bootGraceMs) {
              if (!graceLogged) {
                graceLogged = true;
                console.error(
                  `[api proxy] ${code || err.message} — ${req.url} (API ainda iniciando — aguarde)`,
                );
              }
              return;
            }
            const now = Date.now();
            if (now - lastWarnedAt < 5_000) return;
            lastWarnedAt = now;
            console.error(`[api proxy] ${code || err.message} — ${req.url}`);
            console.error(
              "  → FastAPI on :7897 is not running. Start both with: npm run dev",
            );
          });
        },
      };
      return {
        // Fail-fast lane — the endpoints first paint and idle pollers await.
        // A wedged backend (accepts but never answers — 2026-09-13 incident:
        // ~70 CLOSE_WAIT pile-up on :7897) left these proxied fetches pending
        // FOREVER, and the UI never painted. proxyTimeout bounds it: http-proxy
        // destroys the silent upstream req, vite's proxy "error" handler then
        // sends 500 (vite never writes headers before the response event).
        // 8s is ~160x the measured direct latency (~50ms) and far above any
        // legitimate settings/info poll. Matched BEFORE "/api" (insertion
        // order) so it wins for these URLs.
        "^/api/(?:settings(?:/(?:features|recommended|youtube-auth))?|features|info|health|presence|app/version|errors/latest)([?].*)?$": {
          ...base,
          proxyTimeout: 8_000,
        },
        // Preview/player control lane — the requests that OPEN a preview.
        //
        // Measured 2026-10-15: POST /api/preview/session answers in
        // 0.12-0.44s (6/6 direct, max 0.44s). The same POST through this
        // proxy was hanging until the 45s harness limit, reporting
        // session=null, because it fell into the no-timeout "/api" lane
        // below — the 2026-09-13 failure mode: an upstream that accepts but
        // never answers leaves these pending FOREVER and the UI never
        // paints.
        //
        // 2026-10-19: the 8s cap was measured to be INSIDE the normal cold
        // path, not above it. A cold YouTube resolve (no warm snapshot yet —
        // the state right after a restart, or for any video the warm batch
        // has not touched) took 9.75s on the server, which logged
        // session_created and answered 200 AFTER this proxy had already
        // destroyed the socket: the page saw 500 + `socket hang up` and a
        // console error for a preview the backend had successfully built.
        // Measured cold/warm the same day, direct against :7897:
        //   cold YouTube 5.26s · warm 12ms · warm (other vid) 0.21s
        //   Twitch 0.85s · Kick 1.59s
        // So cold resolves legitimately take seconds while the app's own
        // create budget is 45s (routers/preview.py `_PREVIEW_CREATE_HARD_TIMEOUT_SEC`).
        // 30s stays bounded (a wedged upstream still costs a 500, not a dead
        // tab) and sits above every measured cold resolve, so slowness is no
        // longer converted into an error. It must stay below the backend's
        // 45s budget so the client never outlives the server's own timeout.
        // Matched BEFORE "/api" (insertion order) so it wins for these URLs.
        "^/api/preview/(?:session|invalidate|warm(?:/batch)?|seek|quality)([?].*)?$": {
          ...base,
          proxyTimeout: 30_000,
        },
        // Everything else (SSE streams with 15s backend keepalives, HLS/MP4
        // proxies, yt-dlp extraction, folder pickers, scans) keeps the old
        // no-timeout behavior — a fixed cap would sever legitimate long calls.
        "/api": base,
      };
    })(),
  },
});
