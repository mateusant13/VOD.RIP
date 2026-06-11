#!/usr/bin/env node
/** Start FastAPI (7897) + Vite dev server (5173) together. */
import { execSync, spawn } from "node:child_process";
import http from "node:http";
import net from "node:net";
import { fileURLToPath } from "node:url";
import path from "node:path";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const pyDir = path.join(root, "backend");
const shell = process.platform === "win32";
const apiPort = process.env.PORT || "7897";

const children = [];

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

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

function listeningPids(port) {
  const pids = new Set();
  if (process.platform === "win32") {
    const out = execSync("netstat -ano", { encoding: "utf8" });
    for (const line of out.split(/\r?\n/)) {
      if (!line.includes("LISTENING")) continue;
      const cols = line.trim().split(/\s+/);
      const local = cols[1] || "";
      if (!local.endsWith(`:${port}`)) continue;
      const pid = cols[cols.length - 1];
      if (/^\d+$/.test(pid)) pids.add(Number(pid));
    }
    return [...pids];
  }
  try {
    const out = execSync(`lsof -ti :${port} -sTCP:LISTEN`, { encoding: "utf8" });
    return out
      .split(/\s+/)
      .filter((s) => /^\d+$/.test(s))
      .map(Number);
  } catch {
    return [];
  }
}

async function releasePort(port) {
  const mine = process.pid;
  const pids = listeningPids(port).filter((pid) => pid !== mine);
  if (!pids.length) return;

  console.log(
    `[api] port :${port} in use (pid ${pids.join(", ")}) — stopping old listener and starting fresh`,
  );

  for (const pid of pids) {
    try {
      if (process.platform === "win32") {
        execSync(`taskkill /F /T /PID ${pid}`, { stdio: "ignore" });
      } else {
        process.kill(pid, "SIGKILL");
      }
    } catch {
      /* already gone */
    }
  }

  for (let i = 0; i < 24; i++) {
    await sleep(250);
    if (await portFree(port)) return;
  }

  console.warn(`[api] port :${port} may still be busy after kill attempt`);
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

  await releasePort(port);

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

  start("web", shell ? "npx.cmd" : "npx", ["vite"], root);
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
