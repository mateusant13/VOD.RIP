import path from "path";
import { fileURLToPath } from "url";
import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
/// <reference types="vitest" />
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
  },
  server: {
    host: true,
    port: 5173,
    strictPort: true,
    // No server.warmup here: Vite warms clientFiles SEQUENTIALLY (measured
    // ~10s/file under AV scan → 10-15min for this graph), blocking first
    // requests the whole time. dev-all.mjs prewarms source files with
    // parallel first-touch reads instead — transforms then cost 1-24ms.
    proxy: {
      "/api": {
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
      },
    },
  },
});
