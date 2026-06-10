#!/usr/bin/env node
/** Start FastAPI (7897) + Vite dev server (5173) together. */
import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";
import path from "node:path";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const pyDir = path.join(root, "KickDownloader", "KickDownloaderPy");
const shell = process.platform === "win32";
const apiPort = process.env.PORT || "7897";

const children = [];

function start(label, command, args, cwd, { restartOnCrash = false } = {}) {
  const child = spawn(command, args, {
    cwd,
    stdio: "inherit",
    shell,
    env: { ...process.env, PORT: apiPort },
  });
  child.on("exit", (code, signal) => {
    if (signal) return;
    if (code !== 0 && code !== null) {
      console.error(`[${label}] exited with code ${code}`);
      if (restartOnCrash) {
        console.error(`[${label}] restarting in 2s…`);
        setTimeout(() => start(label, command, args, cwd, { restartOnCrash: true }), 2000);
        return;
      }
      shutdown(1);
    }
  });
  children.push(child);
  return child;
}

function shutdown(code = 0) {
  for (const child of children) {
    try {
      if (process.platform === "win32") {
        spawn("taskkill", ["/PID", String(child.pid), "/T", "/F"], { shell: true });
      } else {
        child.kill("SIGTERM");
      }
    } catch {
      /* ignore */
    }
  }
  setTimeout(() => process.exit(code), 300);
}

process.on("SIGINT", () => shutdown(0));
process.on("SIGTERM", () => shutdown(0));

console.log("Starting API  -> http://localhost:" + apiPort);
console.log("Starting Vite -> http://localhost:5173");
console.log("(Ctrl+C stops both)\n");

start("api", "python", ["run.py"], pyDir, { restartOnCrash: true });

// Give uvicorn a moment to bind before Vite proxies /api.
setTimeout(() => {
  start("web", shell ? "npm.cmd" : "npm", ["run", "dev"], root);
}, 2000);
