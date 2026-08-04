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
    host: true,
    port: 5175,
    strictPort: true,
    proxy: {
      "/api": { target: `http://127.0.0.1:${API_PORT}`, changeOrigin: true },
    },
  },
});
