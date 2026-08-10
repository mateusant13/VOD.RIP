#!/usr/bin/env node
/** Start FastAPI (7897) + Vite dev server (5173) together. */
import { execSync, spawn, spawnSync } from "node:child_process";
import fs from "node:fs";
import http from "node:http";
import net from "node:net";
import { fileURLToPath } from "node:url";
import path from "node:path";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const pyDir = path.join(root, "backend");
const apiPort = Number(process.env.PORT || "7897");
const vitePort = Number(process.env.VITE_PORT || "5173");
const builtUi = path.join(pyDir, "static", "index.html");
const serveBuiltUi = fs.existsSync(builtUi);
/** Windows: prefer py -3.11 so a stale 3.10 on PATH does not run the API. */
const pyCmd = process.env.VODRIP_PYTHON || (process.platform === "win32" ? "py" : "python");
const pyArgsPrefix = process.env.VODRIP_PYTHON ? [] : process.platform === "win32" ? ["-3.11"] : [];

const fastPreview =
  process.argv.includes("--fast-preview") ||
  process.argv.includes("2") ||
  process.env.VODRIP_PREVIEW_FAST_ONLY === "1";
const previewFastEnv = fastPreview ? { VODRIP_PREVIEW_FAST_ONLY: "1" } : {};

const children = [];

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function isPortListeningOn(host, port) {
  return new Promise((resolve) => {
    const socket = net.createConnection({ port, host });
    const done = (listening) => {
      socket.removeAllListeners();
      try { socket.destroy(); } catch { /* ignore */ }
      resolve(listening);
    };
    socket.setTimeout(600);
    socket.once("connect", () => done(true));
    socket.once("timeout", () => done(false));
    socket.once("error", () => done(false));
  });
}

async function isPortListening(port) {
  // Vite may bind [::1] only on Windows while API checks used 127.0.0.1
  for (const host of ["127.0.0.1", "::1"]) {
    if (await isPortListeningOn(host, port)) return true;
  }
  return false;
}

async function waitPortFree(port, timeoutMs = 12000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (!(await isPortListening(port))) return true;
    await sleep(200);
  }
  return false;
}

function getWinPortPids(port) {
  const result = spawnSync(
    "powershell",
    [
      "-NoProfile",
      "-Command",
      `(Get-NetTCPConnection -LocalPort ${port} -State Listen -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique) -join ' '`,
    ],
    { encoding: "utf8", windowsHide: true, timeout: 8000 },
  );
  const out = (result.stdout || "").trim();
  return [...new Set(out.split(/\s+/).filter((x) => /^\d+$/.test(x)).map(Number))];
}

function killWinPid(pid) {
  if (!pid || pid === process.pid) return;
  for (const [cmd, args] of [
    ["taskkill", ["/F", "/PID", String(pid)]],
    ["taskkill", ["/F", "/T", "/PID", String(pid)]],
    ["powershell", ["-NoProfile", "-Command", `Stop-Process -Id ${pid} -Force -ErrorAction SilentlyContinue`]],
  ]) {
    spawnSync(cmd, args, { stdio: "ignore", windowsHide: true, timeout: 5000 });
  }
}

function releasePort(port) {
  // Force-kill only. We deliberately do NOT call `release_api_port` here —
  // that helper POSTs `/api/exit` which can race-kill a freshly-spawned VOD.RIP
  // API if a sibling dev-all session is starting. taskkill on the PIDs we
  // observed is sufficient for the dev workflow.
  if (process.platform === "win32") {
    for (const pid of getWinPortPids(port)) {
      killWinPid(pid);
    }
  }
}

async function ensurePortFree(port, label) {
  for (let attempt = 1; attempt <= 8; attempt++) {
    if (!(await isPortListening(port))) {
      if (attempt > 1) console.log(`[dev] ${label} :${port} is free`);
      return;
    }
    if (attempt === 1) {
      console.log(`[dev] ${label} :${port} busy — killing listener(s)...`);
    }
    releasePort(port);
    if (await waitPortFree(port, 4000)) {
      console.log(`[dev] ${label} :${port} is free`);
      return;
    }
  }
  const pids = process.platform === "win32" ? getWinPortPids(port) : [];
  console.error(
    `[dev] ${label} :${port} still busy after kill attempts${pids.length ? `: [${pids.join(", ")}]` : ""} — aborting`,
  );
  process.exit(1);
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

function viteHealthy(port) {
  return new Promise((resolve) => {
    const req = http.get(`http://127.0.0.1:${port}/`, (res) => {
      resolve(res.statusCode >= 200 && res.statusCode < 500);
      res.resume();
    });
    req.on("error", () => resolve(false));
    req.setTimeout(2500, () => {
      req.destroy();
      resolve(false);
    });
  });
}

function truncateIfLarge(filePath) {
  try {
    const stat = fs.statSync(filePath);
    if (stat.size > 10 * 1024 * 1024) {
      // ponytail: overwrite truncates; no fd juggling needed
      fs.writeFileSync(filePath, "");
    }
  } catch {
    // file missing or can't stat — first-run is fine
  }
}

/**
 * Touch every app source file once (parallel). A cold first-touch costs up to
 * ~13s/file (cold OS page cache + AV scan-on-open); reading each file here
 * while Vite/API boot warms the OS cache AND the AV scanner's per-file cache,
 * so the browser's first module requests hit warm files (measured 1-24ms
 * transforms). The reads are throwaway — Vite's transform pass is unchanged.
 * ponytail: parallelism is capped implicitly by Defender's scan queue; if a
 * future machine still shows cold first-touches, pre-transform via Vite's
 * `server.warmup` instead (already configured in vite.config.ts).
 */
async function prewarmSourceFiles() {
  const srcDir = path.join(root, "src");
  const files = [];
  const walk = (dir) => {
    let entries;
    try {
      entries = fs.readdirSync(dir, { withFileTypes: true });
    } catch {
      return;
    }
    for (const entry of entries) {
      const p = path.join(dir, entry.name);
      if (entry.isDirectory()) walk(p);
      else if (/\.(ts|tsx|css)$/.test(entry.name)) files.push(p);
    }
  };
  walk(srcDir);
  if (files.length === 0) return;
  const t0 = Date.now();
  await Promise.all(files.map((f) => fs.promises.readFile(f).catch(() => null)));
  console.log(`[dev] prewarmed ${files.length} source files in ${Date.now() - t0}ms`);
}

function attachChildLogger(label, child) {
  const logDir = path.join(root, "tmp");
  try {
    fs.mkdirSync(logDir, { recursive: true });
  } catch {
    // fall back to terminal-only
  }
  const logPath = path.join(logDir, `vodrip-devall-${label}.log`);
  truncateIfLarge(logPath);
  const stream = fs.createWriteStream(logPath, { flags: "a" });

  const tag = () => {
    const ts = new Date().toISOString().replace(/Z$/, ""); // YYYY-MM-DDTHH:MM:SS.fff
    return `${ts} [${label}] `;
  };

  const fwd = (source, dest) => {
    source.on("data", (data) => {
      const lines = data.toString().split(/\r?\n/);
      for (const line of lines) {
        if (!line) continue;
        const t = tag();
        stream.write(t + line + "\n");
        dest.write(line + "\n");
      }
    });
  };

  fwd(child.stdout, process.stdout);
  fwd(child.stderr, process.stderr);

  child.on("close", () => {
    stream.end();
  });
}

let restartAttempts = 0;

function start(label, command, args, cwd, extraEnv = {}) {
  const disableLog = process.env.VODRIP_DEVALL_DISABLE_LOG === "1";
  const stdio = disableLog ? "inherit" : ["ignore", "pipe", "pipe"];
  const child = spawn(command, args, {
    cwd,
    stdio,
    shell: false,
    env: { ...process.env, PORT: String(apiPort), ...extraEnv },
  });
  if (!disableLog) {
    attachChildLogger(label, child);
  }
  child.on("exit", (code, signal) => {
    if (signal) return;
    if (code !== 0 && code !== null) {
      console.error(`[${label}] exited with code ${code}`);
      if (label === "api" && restartAttempts < 3) {
        // Transient crash (observed twice: API exits 1 with no traceback
        // after ~30-60min — AV scan / memory pressure during archive ingest).
        // Restart instead of taking the whole dev-all down.
        restartAttempts += 1;
        const delaySec = restartAttempts * 3;
        console.error(
          `[dev] API crashed — restarting in ${delaySec}s (attempt ${restartAttempts}/3)`,
        );
        setTimeout(() => start(label, command, args, cwd, extraEnv), delaySec * 1000);
      } else {
        shutdown(1);
      }
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
try {
  // SIGHUP = terminal detached / tmux pane closed; clean up children
  process.on("SIGHUP", () => shutdown(0));
} catch {
  // unsupported on Windows — no-op
}

async function main() {
  /** --kill-logs: empty all devall log files and exit (no server start). */
  if (process.argv.includes("--kill-logs")) {
    const logDir = path.join(root, "tmp");
    try {
      for (const name of fs.readdirSync(logDir)) {
        if (name.startsWith("vodrip-devall-") && name.endsWith(".log")) {
          fs.writeFileSync(path.join(logDir, name), "");
          console.log(`[dev] cleared ${name}`);
        }
      }
    } catch {
      // directory missing or no files — nothing to clear
    }
    process.exit(0);
  }

  await ensurePortFree(apiPort, "API");

  if (fastPreview) {
    console.log(
      "[dev:2] VODRIP_PREVIEW_FAST_ONLY=1 — innertube race only (~8s), no cookies/POT/browser/slow fallback",
    );
    console.log("        Best for shorts/simple VODs; 6h titiltei streams need npm run dev\n");
  }

  // Vite's cold start is the long pole (~60s cold OS cache + AV scanning) —
  // start it FIRST so it gets uncontended CPU while the API boots. Prewarm
  // source files in parallel (see prewarmSourceFiles) and only announce the
  // URL once the first browser load would be fast.
  const prewarm = prewarmSourceFiles();
  if (await viteHealthy(vitePort)) {
    console.log(`[dev] Vite already running on :${vitePort} — reusing\n`);
    console.log("(Ctrl+C stops API only; existing Vite keeps running)\n");
  } else {
    await ensurePortFree(vitePort, "Vite");
    console.log("(Ctrl+C stops both)\n");

    const viteBin = path.join(root, "node_modules", "vite", "bin", "vite.js");
    if (!fs.existsSync(viteBin)) {
      console.error("[web] vite not installed — run npm install first");
      shutdown(1);
      return;
    }
    start("web", process.execPath, [viteBin, "--port", String(vitePort), "--strictPort"], root);
  }

  console.log(`Starting API  -> http://localhost:${apiPort}  (/api only)`);
  start("api", pyCmd, [...pyArgsPrefix, "run.py"], pyDir, {
    VODRIP_SKIP_PORT_RELEASE: "1",
    ...previewFastEnv,
    ...(serveBuiltUi ? { KICK_SERVE_UI: "1" } : {}),
  });

  await prewarm;

  // Warm the first-load HTTP paths (HTML pipeline + entry + the one-time
  // Tailwind CSS compile, ~10s+ cold) so the browser's first requests hit
  // Vite's in-memory transform cache instead of paying cold costs. Run
  // SEQUENTIALLY in browser order (html → entry → css): parallel first
  // requests can race Vite's import analysis and leave a module stuck
  // "loading" in the graph, hanging every later request for it. The rest of
  // the graph transforms fast because prewarmSourceFiles already touched
  // every source file (measured 1-24ms transforms once OS-warm).
  for (let i = 0; i < 240 && !(await viteHealthy(vitePort)); i++) {
    await sleep(500);
  }
  const warmTargets = [
    `http://127.0.0.1:${vitePort}/`,
    `http://127.0.0.1:${vitePort}/src/main.tsx`,
    `http://127.0.0.1:${vitePort}/src/index.css`,
  ];
  console.log("[dev] warming first-load modules\u2026");
  for (const u of warmTargets) {
    await new Promise((resolve) => {
      const req = http.get(u, (res) => {
        res.resume();
        res.on("end", resolve);
      });
      req.on("error", () => resolve());
      req.setTimeout(180000, () => {
        req.destroy();
        resolve();
      });
    });
  }
  console.log(`Open UI at    -> http://localhost:${vitePort}`);

  if (serveBuiltUi) {
    console.log(`Fast UI at   -> http://localhost:${apiPort}  (built bundle, instant — npm run build-copy to refresh)`);
  }

  // Safety net only: the lifespan now yields quickly (fast yield), so the API
  // usually responds within ~2-3s. 120s covers post-reboot / dirty-volume
  // worst cases without leaving a hung process when the warm itself hangs.
  for (let i = 0; i < 240; i++) {
    await sleep(500);
    if (await apiHealthy(apiPort)) break;
    if (i === 239) {
      console.error(`[api] did not become ready on :${apiPort} within 120s`);
      shutdown(1);
      return;
    }
  }
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
