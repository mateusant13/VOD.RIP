#!/usr/bin/env node
/** Start FastAPI (7897) + Vite dev server (5173) together. */
import { spawn } from "node:child_process";
import http from "node:http";
import net from "node:net";
import { fileURLToPath } from "node:url";
import path from "node:path";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const pyDir = path.join(root, "backend");
const shell = process.platform === "win32";
const apiPort = process.env.PORT || "7897";

const children = [];
let apiManaged = false;

function portFree(port) {
  return new Promise((resolve) => {
    const srv = net.createServer();
    srv.once("error", () => resolve(false));
    srv.once("listening", () => {
      srv.close(() => resolve(true));
    });
    srv.listen(Number(port), "127.0.0.1");
  });
}

function apiHealthy(port) {
  return new Promise((resolve) => {
    const req = http.get(`http://127.0.0.1:${port}/api/settings`, (res) => {
      resolve(res.statusCode === 200);
      res.resume();
    });
    req.on("error", () => resolve(false));
    req.setTimeout(2500, () => {
      req.destroy();
      resolve(false);
    });
  });
}

function start(label, command, args, cwd) {
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

async function main() {
  const port = Number(apiPort);
  const healthy = await apiHealthy(port);
  const free = await portFree(port);

  if (healthy) {
    console.log(`[api] already running on :${port} — reusing it`);
  } else if (!free) {
    console.error(
      `[api] port :${port} is in use but the API is not responding.`,
    );
    console.error(
      "  Kill the old process (Task Manager / netstat) then run npm run dev again.",
    );
    process.exit(1);
  } else {
    apiManaged = true;
    console.log("Starting API  -> http://localhost:" + port + "  (/api only)");
    start("api", "python", ["run.py"], pyDir);
    // Wait until API accepts connections before Vite proxies /api.
    for (let i = 0; i < 30; i++) {
      await new Promise((r) => setTimeout(r, 500));
      if (await apiHealthy(port)) break;
      if (i === 29) {
        console.error(`[api] did not become ready on :${port} within 15s`);
        shutdown(1);
        return;
      }
    }
  }

  console.log("Open UI at    -> http://localhost:5173");
  console.log("(Ctrl+C stops both)\n");

  start("web", shell ? "npx.cmd" : "npx", ["vite"], root);
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
