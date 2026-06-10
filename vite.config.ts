import path from "path";
import { fileURLToPath } from "url";
import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";
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
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: `http://127.0.0.1:${API_PORT}`,
        changeOrigin: true,
        configure(proxy) {
          proxy.on("error", (err, req) => {
            const code = "code" in err ? (err as NodeJS.ErrnoException).code : "";
            console.error(`[api proxy] ${code || err.message} — ${req.url}`);
            console.error(
              "  → FastAPI on :7897 may have crashed. Restart: npm run dev:api",
            );
          });
        },
      },
    },
  },
});
