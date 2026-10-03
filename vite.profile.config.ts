// WS-9 profiling-only config: no @vitejs/plugin-react (its @babel/core dep is
// missing from the shared node_modules). esbuild transforms TSX natively;
// only HMR/fast-refresh is lost — irrelevant for headless profiling.
import path from "path";
import { fileURLToPath } from "url";
import tailwindcss from "@tailwindcss/vite";
import { defineConfig } from "vite";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const API_PORT = process.env.PORT || "7899";

export default defineConfig({
  plugins: [tailwindcss()],
  resolve: {
    alias: { "@": path.resolve(__dirname, "src") },
  },
  esbuild: { jsx: "automatic" },
  server: {
    // Bind loopback only. `host: true` means 0.0.0.0, which exposes this dev
    // server (and its /api proxy) to every host on the LAN — and that is the
    // exact precondition the vite `server.fs.deny` bypass advisories
    // (GHSA-356w-63v5-8wf4, GHSA-4r4m-qw57-chr8, GHSA-859w-5945-r5v3) require.
    // This config exists for headless local profiling; nothing needs to reach
    // it from another machine, so loopback is the whole requirement.
    host: "127.0.0.1",
    port: 5175,
    strictPort: true,
    proxy: {
      "/api": { target: `http://127.0.0.1:${API_PORT}`, changeOrigin: true },
    },
  },
});
