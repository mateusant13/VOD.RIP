#!/usr/bin/env node
/** Start FastAPI (7897) + Vite dev server (5173) together. */
import { execSync, spawn } from "node:child_process";
import http from "node:http";
import { fileURLToPath } from "node:url";
import path from "node:path";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const pyDir = path.join(root, "backend");
const apiPort = process.env.PORT || "7897";

const children = [];

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
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

function releasePort(port) {
  try {
    execSync(
      `python -c "from services.server_lifecycle import release_api_port; release_api_port(${port})"`,
      { cwd: pyDir, stdio: "inherit", env: { ...process.env, PORT: String(port) } },
    );
  } catch (err) {
    console.warn(`[api] port :${port} release failed:`, err?.message || err);
  }
}

function start(label, command, args, cwd) {
  const child = spawn(command, args, {
    cwd,
    stdio: "inherit",
    shell: false,
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
        spawn("taskkill", ["/PID", String(child.pid), "/T", "/F"], {
          stdio: "ignore",
          windowsHide: true,
        });
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

  releasePort(port);

  console.log("Starting API  -> http://localhost:" + port + "  (/api only)");
  start("api", "python", ["run.py"], pyDir);

  for (let i = 0; i < 30; i++) {
    await sleep(500);
    if (await apiHealthy(port)) break;
    if (i === 29) {
      console.error(`[api] did not become ready on :${port} within 15s`);
      shutdown(1);
      return;
    }
  }

  console.log("Open UI at    -> http://localhost:5173");
  console.log("(Ctrl+C stops both)\n");

  start("web", process.platform === "win32" ? "npx.cmd" : "npx", ["vite"], root);
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
