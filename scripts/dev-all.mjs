#!/usr/bin/env node
/** Start FastAPI (7897) + Vite dev server (5173) together. */
import { execSync, spawn, spawnSync } from "node:child_process";
import fs from "node:fs";
import http from "node:http";
import net from "node:net";
import { pathToFileURL, fileURLToPath } from "node:url";
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
let apiProcess = null; // current API child — health monitor + graceful exit target
let apiSpec = null; // last API launch spec — hot-reload respawn source
let reloading = false; // hot reload in flight: exit handler delegates to reloadApi
let healthMonitorStop = null; // anti-hang watchdog cleanup (paused during reload)
let watcherTimer = null; // backend poll interval
let pendingAfterReload = null; // change detected mid-reload — re-applied when the reload settles

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
    // Kill the whole tree first; killing the parent first loses the handle
    // needed to terminate orphaned Vite/API children.
    ["taskkill", ["/F", "/T", "/PID", String(pid)]],
    ["taskkill", ["/F", "/PID", String(pid)]],
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

function apiHealthy(port, timeoutMs = 2500) {
  return new Promise((resolve) => {
    // Probe the DEDICATED liveness endpoint, never app work. /api/settings is
    // app work: on 2026-10-11 a 9-way parallel multi-platform preview burst
    // starved it past this 2.5s timeout for three consecutive 15s probes and
    // this watchdog force-killed a LIVE API mid-request - six in-flight POST
    // /api/preview/session sockets died with ECONNRESET and the app came back
    // at 22:47:26. /api/health is answered by the 4-worker LIVENESS_EXECUTOR
    // and exists for exactly this reason (deps.py: a burst of app work must
    // "never queue the health probe behind app work and fake a dead app -
    // the old watchdog-restart failure class").
    const req = http.get(`http://127.0.0.1:${port}/api/health`, (res) => {
      resolve(res.statusCode === 200);
      res.resume();
    });
    req.on("error", () => resolve(false));
    req.setTimeout(timeoutMs, () => {
      req.destroy();
      resolve(false);
    });
  });
}

/** POST /api/exit — the app's own graceful shutdown (flush watchdog sinks,
 * cancel downloads cleanly). Resolves true when the app confirmed. */
function gracefulApiExit(timeoutMs = 5000) {
  return new Promise((resolve) => {
    const body = "";
    const req = http.request(
      {
        host: "127.0.0.1",
        port: apiPort,
        path: "/api/exit",
        method: "POST",
        timeout: timeoutMs,
      },
      (res) => {
        res.resume();
        res.on("end", () => resolve(res.statusCode === 200));
      },
    );
    req.on("error", () => resolve(false));
    req.on("timeout", () => {
      req.destroy();
      resolve(false);
    });
    req.end(body);
  });
}

/** Print the app's aggregate health (queue, detached workers) to the dev
 * terminal — boot and shutdown visibility into what keeps running. */
async function reportHealth(label) {
  try {
    const body = await new Promise((resolve, reject) => {
      const req = http.get(`http://127.0.0.1:${apiPort}/api/health`, (res) => {
        let data = "";
        res.on("data", (c) => (data += c));
        res.on("end", () => resolve(data));
      });
      req.on("error", reject);
      req.setTimeout(3000, () => {
        req.destroy();
        reject(new Error("timeout"));
      });
    });
    const h = JSON.parse(body);
    console.log(
      `[dev] ${label}: queue_pending=${h.queue_pending} worker_alive=${h.worker_alive} ` +
        `background_alive=${h.background_alive} app_activity_age=${h.app_activity_age_s}s`,
    );
  } catch {
    /* health endpoint is optional — boot/exit must not fail on it */
  }
}

/** Anti-hang watchdog: poll the API; N consecutive missed probes while the
 * process is alive = hung (deadlocked sqlite, stuck thread) → onUnhealthy.
 * Exported so scripts/_health_monitor_test.mjs can exercise the real code
 * against a fake server. */
export function startHealthMonitor({
  port = apiPort,
  healthIntervalMs = 15000,
  maxStrikes = 3,
  healthTimeoutMs = 2500,
  graceMs = 0,
  onUnhealthy,
  onHealthy,
}) {
  let strikes = 0;
  const startedAt = Date.now();
  // Measured, not computed. The old call site printed the constant "~45s"
  // (maxStrikes x healthIntervalMs), so a monitor whose clock predated a
  // restart announced "unresponsive for ~45s" about an API that had been
  // alive for four seconds. The number was a claim the code never made.
  let lastOkAt = Date.now();
  const timer = setInterval(async () => {
    if (Date.now() - startedAt < graceMs) return;
    const ok = await apiHealthy(port, healthTimeoutMs);
    if (ok) {
      strikes = 0;
      lastOkAt = Date.now();
      try {
        onHealthy?.();
      } catch {
        /* the watchdog must never throw into the interval */
      }
      return;
    }
    strikes += 1;
    if (strikes === maxStrikes) {
      try {
        onUnhealthy(strikes, Date.now() - lastOkAt);
      } catch {
        /* watchdog must never throw into the interval */
      }
    }
  }, healthIntervalMs);
  return () => clearInterval(timer);
}

function viteHealthy(port) {
  return new Promise((resolve) => {
    // Vite binds the hostname configured in vite.config.ts (localhost may
    // resolve to IPv6 on Windows); probing 127.0.0.1 falsely reports a live
    // Vite as down and makes the next npm run dev take over unnecessarily.
    const req = http.get(`http://localhost:${port}/`, (res) => {
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
async function waitForDevAllHealthy(timeoutMs = 15000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const [apiOk, viteOk] = await Promise.all([apiHealthy(apiPort), viteHealthy(vitePort)]);
    if (apiOk && viteOk) return true;
    await sleep(1000);
  }
  return false;
}
function devAllStartupWaitMs(lock) {
  const startedAt = Date.parse(lock.startedAt || "");
  if (!Number.isFinite(startedAt)) return 15000;
  // A lock written during a cold API boot is valid even while one child is
  // still starting. Give recent owners the same 120s budget as main boot;
  // stale owners older than that are checked quickly and replaced.
  return Math.max(15000, 120000 - Math.max(0, Date.now() - startedAt));
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

// --- single-instance lock + attach mode ------------------------------------
// A second `npm run dev` must NOT kill the running dev-all (its
// ensurePortFree would taskkill the live API, the old supervisor counts that
// as a crash, and the loser of the bind race ends up with no API at all —
// exit-0 "won" is not retried). Instead the new session ATTACHES to the
// owner's log files (tmp/vodrip-devall-{api,web}.log — always written by
// attachChildLogger) and prints them until Ctrl+C. `--kill` forces a real
// takeover: stop the owner's whole tree, then boot everything fresh.
const lockPath = path.join(root, "tmp", "dev-all.lock");
let ownsLock = false;

function readLock() {
  try {
    const raw = fs.readFileSync(lockPath, "utf8");
    const lock = JSON.parse(raw);
    if (typeof lock?.pid !== "number") return null;
    try {
      process.kill(lock.pid, 0); // throws when the pid is gone
    } catch {
      return null; // stale lock — owner died
    }
    return lock;
  } catch {
    return null;
  }
}

function tryWriteLock() {
  try {
    fs.mkdirSync(path.join(root, "tmp"), { recursive: true });
    const fd = fs.openSync(lockPath, "wx"); // atomic — EEXIST when racing
    fs.writeSync(
      fd,
      JSON.stringify({ pid: process.pid, startedAt: new Date().toISOString(), apiPort }),
    );
    fs.closeSync(fd);
    return true;
  } catch {
    return false; // another dev-all won the race
  }
}

function removeLock() {
  try {
    fs.unlinkSync(lockPath);
  } catch {
    /* missing — nothing to remove */
  }
}

async function tailLog(name) {
  // Poll the owner's log file from the end and print new lines verbatim
  // (the lines already carry `ISO [api|web] ` tags from attachChildLogger).
  const p = path.join(root, "tmp", `vodrip-devall-${name}.log`);
  let pos = 0;
  try {
    pos = fs.statSync(p).size;
  } catch {
    /* file not created yet — start from 0 */
  }
  // eslint-disable-next-line no-constant-condition
  while (true) {
    await sleep(400);
    try {
      const size = fs.statSync(p).size;
      if (size < pos) pos = 0; // truncated by --kill-logs or rotation
      if (size > pos) {
        const fd = fs.openSync(p, "r");
        const buf = Buffer.alloc(size - pos);
        fs.readSync(fd, buf, 0, buf.length, pos);
        fs.closeSync(fd);
        pos = size;
        process.stdout.write(buf.toString("utf8"));
      }
    } catch {
      /* file vanished — keep polling */
    }
  }
}

async function attachToDevAll(lock) {
  if (await waitForDevAllHealthy(devAllStartupWaitMs(lock))) {
    console.log(`[dev] dev-all already running (pid ${lock.pid}) — attaching to its logs.`);
    console.log(`[dev] Ctrl+C detaches. Run \`npm run dev -- --kill\` to stop it and take over.\n`);
    // Attach has no children and no lock — a plain exit, never shutdown().
    process.on("SIGINT", () => process.exit(0));
    process.on("SIGTERM", () => process.exit(0));
    await Promise.all([tailLog("api"), tailLog("web")]);
    return true;
  }

  console.warn(
    `[dev] existing dev-all (pid ${lock.pid}) is unhealthy — replacing its stale lock and children`,
  );
  await gracefulApiExit(3000);
  if (process.platform === "win32") {
    killWinPid(lock.pid);
  } else {
    try { process.kill(lock.pid, "SIGTERM"); } catch { /* owner already gone */ }
  }
  removeLock();
  await waitPortFree(apiPort, 15000);
  await waitPortFree(vitePort, 15000);
  return false;
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

/* The API must never be left down.
 *
 * Measured 2026-10-12: restartAttempts was reset ONLY inside reloadApi(), so
 * after three crashes — or three hang-kills, which arrive as an exit CODE on
 * Windows rather than a signal — every branch fell through to shutdown(1) and
 * the whole dev supervisor exited. The API then stayed down until a human
 * noticed, which is the "o backend sempre cai, nunca ta on" the owner
 * reported. A supervisor that dies because its child crashed is strictly worse
 * than one that keeps retrying, so there is no give-up path here any more: the
 * backoff is capped and the retry continues for as long as the supervisor
 * lives. A crash after a minute of continuous health is a NEW incident, not a
 * continuation of the old one, and resets the budget (see onHealthy below).
 */
const API_RESTART_MAX_DELAY_S = 30;
const API_HEALTHY_RESET_MS = 60_000;
/* How long :apiPort must stay silent before "busy" becomes "hung".
 *
 * Measured 2026-10-13: the API was killed repeatedly while it was demonstrably
 * WORKING - the log shows extract-ok lines right up to each verdict. The probe
 * used a 2 s timeout, but the app sqlite busy_timeout is 10 s
 * (archive_db.py:515) and /api/health runs its DB probe on a 2-worker pool
 * (deps.py:88 HEALTH_EXECUTOR), so two WAL-busy probes queue the third for ~20 s.
 * A two-second probe therefore cannot distinguish a hung API from a busy one,
 * and treating "no answer in 2 s" as proof of a hang is what kept killing it.
 * Silence must be SUSTAINED before it counts. */
const API_HUNG_GRACE_MS = 60_000;
let apiHealthySince = 0;

/* Reclaim :apiPort before a restart — the missing half of the handshake.
 *
 * Measured 2026-10-12 21:06 in tmp/devall11.log. This is the whole of "o
 * backend sempre cai, nunca ta on":
 *
 *   run.py re-executes itself, so the process that actually HOLDS :7897 is a
 *   GRANDCHILD of this supervisor (observed: 604 -> 12548, 34436 -> 40040).
 *   The exit handler kills the child it spawned; the grandchild survives and
 *   keeps LISTENing with six sockets in CLOSE_WAIT — the signature of an event
 *   loop that stopped servicing its accept queue. Every respawn then dies with
 *   [Errno 10048] "only one usage of each socket address is permitted". With
 *   the old three-strikes budget that ended the supervisor; with the
 *   never-give-up budget it became an infinite loop ("attempt 25") because
 *   NOTHING in the restart path freed the port. ensurePortFree already did the
 *   work and was called from the startup path only (dev-all.mjs:919-993).
 *
 * run.py:84 asserts "dev-all.mjs releases the port before spawning us" and
 * dev-all.mjs:98 says "We deliberately do NOT call release_api_port here".
 * Each half waited for the other, and VODRIP_SKIP_PORT_RELEASE=1 (line 969)
 * made sure run.py would not do it either. This is that missing half.
 *
 * Two deliberate differences from ensurePortFree:
 *
 *  1. It NEVER calls process.exit. ensurePortFree aborts the whole supervisor
 *     when the port stays busy (dev-all.mjs:128), which is the opposite of
 *     always-on — a failure here must leave the watchdog free to try again.
 *  2. It refuses to kill a port that ANSWERS. A hung API holds the port and
 *     does not reply; a healthy sibling dev-all session does reply, and killing
 *     that is the murder loop the single-instance lock exists to prevent
 *     (dev-all.mjs:292-299).
 */
async function reclaimApiPort(port) {
  if (!(await isPortListening(port))) return true;
  if (await apiHealthy(port, 2000)) {
    console.log(`[dev] :${port} answers — leaving it alone`);
    return true;
  }
  for (let attempt = 1; attempt <= 8; attempt++) {
    releasePort(port);
    if (await waitPortFree(port, 4000)) {
      console.log(`[dev] reclaimed :${port} from an unresponsive listener`);
      return true;
    }
  }
  console.error(
    `[dev] could not reclaim :${port} — the next watchdog tick will try again`,
  );
  return false;
}
let apiRestartScheduled = false;

function scheduleApiRestart(reason) {
  if (shuttingDown || reloading || apiRestartScheduled) return;
  apiRestartScheduled = true;
  restartAttempts += 1;
  const delaySec = Math.min(API_RESTART_MAX_DELAY_S, restartAttempts * 3);
  console.error(
    `[dev] API ${reason} — restarting in ${delaySec}s (attempt ${restartAttempts}, never giving up)`,
  );
  setTimeout(async () => {
    if (shuttingDown || reloading || !apiSpec) {
      apiRestartScheduled = false;
      return;
    }
    // Reclaim the PORT, not just our child. Without this the respawn cannot
    // bind and the loop never ends — see reclaimApiPort above.
    await reclaimApiPort(apiPort);
    apiRestartScheduled = false;
    if (shuttingDown || reloading || !apiSpec) return;
    start("api", apiSpec.command, apiSpec.args, apiSpec.cwd, apiSpec.extraEnv);
    startApiHealthMonitor();
  }, delaySec * 1000);
}

/* The authoritative always-on watchdog.
 *
 * Measured 2026-10-12: after four forced kills the supervisor correctly
 * restarted every time, then the API died once more and NOTHING brought it
 * back - no restart line, no exit handler, an unserved port. The two existing
 * mechanisms both have holes: the crash/exit handler only fires for a child it
 * is watching, and the health monitor is armed with a 300 s grace that is
 * re-applied on every start, so it is blind for five minutes at a time and was
 * not re-armed at all after a crash restart.
 *
 * This loop does not care how the API went down. Every 10 s it asks the only
 * question that matters - does :apiPort answer? - and if not, and no restart is
 * already pending, it schedules one. It runs for the life of the supervisor and
 * has no grace period after boot, because a dead API is not a warm-up.
 */
let alwaysOnTimer = null;
let apiUnresponsiveSince = 0;
function startAlwaysOnWatchdog() {
  if (alwaysOnTimer) return;
  alwaysOnTimer = setInterval(async () => {
    if (shuttingDown || reloading) return;
    if (await apiHealthy(apiPort, 2000)) {
      apiHealthySince = apiHealthySince || Date.now();
      apiUnresponsiveSince = 0;
      return;
    }
    apiHealthySince = 0;
    // Silence is not a hang until it is SUSTAINED. A single missed 2 s probe is
    // what a 10 s sqlite busy_timeout looks like from the outside, and the old
    // code restarted on exactly that - killing the app mid-extract, over and
    // over, which the owner experienced as previews that never load. Retry
    // first; restart only after API_HUNG_GRACE_MS of continuous silence.
    if (!apiUnresponsiveSince) {
      apiUnresponsiveSince = Date.now();
      console.log(
        `[dev] :${apiPort} missed a probe — watching for ${API_HUNG_GRACE_MS / 1000}s of silence before treating it as hung`,
      );
      return;
    }
    const silentMs = Date.now() - apiUnresponsiveSince;
    if (silentMs < API_HUNG_GRACE_MS) return;
    scheduleApiRestart(`silent on :${apiPort} for ${Math.round(silentMs / 1000)}s`);
  }, 10_000);
}

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
  if (label === "api") {
    apiProcess = child;
    apiSpec = { command, args, cwd, extraEnv };
  }
  child.on("exit", async (code, signal) => {
    if (shuttingDown) return;
    if (label === "api" && reloading) {
      // Hot reload: reloadApi() owns the respawn (single point — avoids a
      // double-start race with the wait loop below). Just acknowledge.
      return;
    }
    if (signal) {
      // POSIX: the anti-hang watchdog kills with SIGKILL → exit event has
      // signal set (Windows taskkill produces a code instead). The API must
      // still respawn, never idle dead.
      if (label === "api") scheduleApiRestart(`killed by signal ${signal}`);
      return;
    }
    if (label === "api" && code === 0) {
      // Exit 0 is the "another healthy API won" race — but if nothing
      // answers :apiPort the API vanished cleanly (graceful /api/exit)
      // and the dev server must stay up. Re-spawn instead of idling.
      if (await apiHealthy(apiPort)) return;
      scheduleApiRestart(`exited with code 0 but :${apiPort} is unserved`);
      return;
    }
    if (code !== 0 && code !== null) {
      console.error(`[${label}] exited with code ${code}`);
      if (label === "api") {
        scheduleApiRestart(`crashed (code ${code})`);
      } else {
        shutdown(1);
      }
    }
  });
  children.push(child);
  return child;
}

let shuttingDown = false;

async function shutdown(code = 0) {
  if (shuttingDown) return; // SIGINT during a graceful exit
  shuttingDown = true;
  if (watcherTimer) {
    clearInterval(watcherTimer);
    watcherTimer = null;
  }
  if (healthMonitorStop) {
    healthMonitorStop();
    healthMonitorStop = null;
  }
  await reportHealth("stopping");
  // Graceful first: POST /api/exit lets the app flush watchdog sinks and
  // cancel downloads cleanly; force-kill is only the fallback.
  if (process.platform === "win32") {
    const ok = await gracefulApiExit();
    if (ok) {
      console.log("[dev] API graceful exit requested — waiting for it to stop");
      for (let i = 0; i < 16; i++) {
        await sleep(500);
        if (!(await isPortListening(apiPort))) break;
      }
    }
  }
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
  if (ownsLock) removeLock();
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

// --- backend hot reload ----------------------------------------------------
// Poll mtimes (NOT fs.watch): Windows fs.watch can drop events under bursts
// (AV scan, git merge/checkout touching many files at once), and polling is
// deterministic. A ~1s scan of the backend tree (only *.py, tests/tmp/
// __pycache__/venvs excluded) is negligible. Bursts collapse to ONE reload
// via the debounce; a minimum 1s gap between reloads rate-limits edit loops.
// ponytail: if sub-second reload latency is ever needed, upgrade to fs.watch
// recursive with a hash fallback — polling keeps this bomb-proof instead.
const reloadPollMs = Number(process.env.VODRIP_DEVALL_RELOAD_POLL_MS || "1000");
const reloadDebounceMs = Number(process.env.VODRIP_DEVALL_RELOAD_DEBOUNCE_MS || "400");
const backendRoot = path.join(root, "backend");

function backendWatchFiles() {
  const out = [];
  const walk = (dir) => {
    let entries;
    try {
      entries = fs.readdirSync(dir, { withFileTypes: true });
    } catch {
      return;
    }
    for (const entry of entries) {
      const p = path.join(dir, entry.name);
      if (entry.isDirectory()) {
        if (
          entry.name === "tmp" ||
          entry.name === "tests" ||
          entry.name === "__pycache__" ||
          entry.name === "venv" ||
          entry.name === ".venv"
        ) {
          continue;
        }
        walk(p);
      } else if (entry.name.endsWith(".py")) {
        try {
          const st = fs.statSync(p);
          out.push({ p, m: st.mtimeMs, s: st.size });
        } catch {
          /* vanished mid-scan */
        }
      }
    }
  };
  walk(backendRoot);
  return out;
}

function startBackendWatcher() {
  if (
    process.argv.includes("--no-reload") ||
    process.env.VODRIP_DEVALL_NO_RELOAD === "1"
  ) {
    console.log("[dev] backend hot reload disabled (--no-reload)");
    return;
  }
  let snapshot = backendWatchFiles();
  let pendingTimer = null;
  let lastTrigger = 0;
  watcherTimer = setInterval(() => {
    const next = backendWatchFiles();
    // Path-keyed diff — readdirSync ordering is not guaranteed, so index
    // pairing would occasionally spuriously reload or mask a real change.
    const snapshotByPath = new Map(snapshot.map((f) => [f.p, f]));
    const nextByPath = new Map(next.map((f) => [f.p, f]));
    let changed = null;
    for (const f of snapshot) {
      const nf = nextByPath.get(f.p);
      if (!nf || nf.m !== f.m || nf.s !== f.s) {
        changed = nf ?? f;
        break;
      }
    }
    if (!changed) {
      for (const f of next) {
        if (!snapshotByPath.has(f.p)) {
          changed = f;
          break;
        }
      }
    }
    snapshot = next;
    if (!changed) return;
    if (reloading) {
      // Change saved while a reload is in flight: stash and re-apply once
      // the reload settles — otherwise the edit is silently dropped.
      pendingAfterReload = changed;
      return;
    }
    if (shuttingDown) return;
    if (pendingTimer) return; // already debouncing a burst
    const fire = () => {
      pendingTimer = null;
      if (shuttingDown || reloading) return;
      const since = Date.now() - lastTrigger;
      if (since < 1000) {
        // Min-1s gap: re-arm with the REMAINDER instead of dropping the
        // change (a fast reload + quick save would lose the edit).
        pendingTimer = setTimeout(fire, 1000 - since);
        return;
      }
      lastTrigger = Date.now();
      void reloadApi(changed.p);
    };
    pendingTimer = setTimeout(fire, reloadDebounceMs);
  }, reloadPollMs);
  console.log(
    `[dev] watching backend/ for changes (poll ${reloadPollMs}ms) — edit a .py to hot-reload the API`,
  );
}

/** Restart the API with the new code. The exit handler delegates to this
 * function (single respawn point) while `reloading` is set. The anti-hang
 * watchdog is paused so the down-time during reload never counts strikes. */
async function reloadApi(changedFile) {
  if (shuttingDown || !apiProcess || apiProcess.exitCode !== null) return;
  if (!apiSpec) return;
  console.log(
    `[dev] backend changed: ${path.relative(root, changedFile)} — reloading API`,
  );
  reloading = true;
  restartAttempts = 0;
  const killTree = () => {
    if (process.platform === "win32") {
      spawn("taskkill", ["/PID", String(apiProcess.pid), "/T", "/F"], {
        stdio: "ignore",
        windowsHide: true,
      });
    } else {
      try {
        apiProcess.kill("SIGKILL");
      } catch {
        /* already gone */
      }
    }
  };
  let healthy = false;
  try {
    if (healthMonitorStop) {
      healthMonitorStop();
      healthMonitorStop = null;
    }
    const ok = await gracefulApiExit();
    if (!ok && apiProcess && apiProcess.exitCode === null) {
      // API unresponsive — force-kill its tree so the port frees.
      killTree();
    }
    let portFreed = await waitPortFree(apiPort, 15000);
    if (!portFreed && apiProcess && apiProcess.exitCode === null) {
      // Graceful teardown stalled past 15s (long flush/downloads): the old
      // process would keep serving OLD code. Force-kill and re-check.
      console.error("[dev] API teardown stalled — force-killing to free the port");
      killTree();
      portFreed = await waitPortFree(apiPort, 10000);
    }
    if (!portFreed) {
      console.error("[dev] API port did not free — aborting reload, scheduling crash restart");
      restartAttempts += 1;
      setTimeout(
        () => start("api", apiSpec.command, apiSpec.args, apiSpec.cwd, apiSpec.extraEnv),
        restartAttempts * 3000,
      );
      return;
    }
    start("api", apiSpec.command, apiSpec.args, apiSpec.cwd, apiSpec.extraEnv);
    for (let i = 0; i < 240 && !shuttingDown; i++) {
      await sleep(500);
      // Broken code (syntax error/bad import) exits within ~1s: detect the
      // death instead of probing a dead port for 120s.
      if (apiProcess.exitCode !== null) break;
      if (await apiHealthy(apiPort)) {
        healthy = true;
        break;
      }
    }
    if (!shuttingDown && !healthy && apiProcess.exitCode !== null) {
      // The fresh API died — standard crash restart (the exit handler
      // delegates while reloading, so this is the single respawn point).
      console.error(
        `[dev] API exited during reload (code ${apiProcess.exitCode}) — scheduling crash restart`,
      );
      restartAttempts += 1;
      setTimeout(
        () => start("api", apiSpec.command, apiSpec.args, apiSpec.cwd, apiSpec.extraEnv),
        restartAttempts * 3000,
      );
      return;
    }
    if (!shuttingDown && healthy) {
      startApiHealthMonitor();
      startAlwaysOnWatchdog();
      console.log(`[dev] API reloaded (${path.relative(root, changedFile)})`);
    }
  } finally {
    reloading = false;
  }
  if (!shuttingDown && pendingAfterReload) {
    // Edit saved mid-reload — apply it now that the reload settled.
    const p = pendingAfterReload.p;
    pendingAfterReload = null;
    setTimeout(() => void reloadApi(p), 100);
  }
}
function startApiHealthMonitor() {
  const graceMs = Number(process.env.VODRIP_DEVALL_HEALTH_GRACE_MS || "300000");
  // ONE monitor, always. This used to overwrite healthMonitorStop WITHOUT
  // calling it, so every restart leaked a live setInterval that nothing could
  // ever clear. Measured 2026-10-12 21:29: with four leaked monitors, four
  // "unresponsive" verdicts arrived per window and each one taskkilled the API
  // - an API that had been listening for four seconds and was mid-extract.
  // That is the murder loop behind "o backend sempre cai": the app was killed
  // by its own watchdog faster than it could serve a single preview, forever.
  if (healthMonitorStop) {
    healthMonitorStop();
    healthMonitorStop = null;
  }
  healthMonitorStop = startHealthMonitor({
    graceMs,
    onHealthy: () => {
      // A crash after a minute of continuous health is a NEW incident, not a
      // continuation of the old one. Without this the budget was consumed for
      // the whole life of the supervisor and the fourth crash was fatal.
      const now = Date.now();
      if (!apiHealthySince) apiHealthySince = now;
      if (now - apiHealthySince >= API_HEALTHY_RESET_MS && restartAttempts > 0) {
        console.error(
          `[dev] API healthy for ${API_HEALTHY_RESET_MS / 1000}s — restart budget reset`,
        );
        restartAttempts = 0;
      }
    },
    onUnhealthy: (_strikes, unresponsiveMs) => {
      if (shuttingDown || reloading) return;
      if (!apiProcess || apiProcess.exitCode !== null) {
        // The child is already dead and no exit handler is going to fire for
        // it again: this is the always-on guarantee, independent of every
        // crash counter. Without it a dead API stayed dead.
        scheduleApiRestart(`dead and :${apiPort} unserved`);
        return;
      }
      apiHealthySince = 0;
      console.error(
        `[dev] API on :${apiPort} unresponsive for ~${Math.round((unresponsiveMs || 0) / 1000)}s — treating as hung, restarting`,
      );
      if (process.platform === "win32") {
        spawn("taskkill", ["/PID", String(apiProcess.pid), "/T", "/F"], {
          stdio: "ignore",
          windowsHide: true,
        });
      } else {
        apiProcess.kill("SIGKILL");
      }
    },
  });
  if (graceMs > 0) {
    console.log(
      `[dev] health monitor armed in ${Math.round(graceMs / 1000)}s (startup warm grace)`,
    );
  }
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

  // Single-instance protocol: attach to a live dev-all by default; only
  // `--kill` takes over (stop the owner's tree, then boot everything fresh).
  const forceTakeover = process.argv.includes("--kill");
  let lock = readLock();
  if (forceTakeover) {
    if (lock) {
      console.log(`[dev] --kill: stopping existing dev-all (pid ${lock.pid}) and taking over`);
      // Graceful first: the owner's API flushes watchdog sinks / cancels
      // downloads; force-kill of the owner's tree is the fallback.
      await gracefulApiExit();
      await waitPortFree(apiPort, 8000);
      killWinPid(lock.pid); // tree-first kill prevents orphaned Vite/API children
      await waitPortFree(apiPort, 15000);
    }
    removeLock(); // stale or just-killed owner
    await ensurePortFree(apiPort, "API");
  } else if (lock) {
    const attached = await attachToDevAll(lock);
    if (attached) return;
    await ensurePortFree(apiPort, "API");
    await ensurePortFree(vitePort, "Vite");
  } else {
    removeLock(); // stale lock (owner died without cleanup)
    // A live, healthy API on apiPort with no lock is an orphaned server from
    // a crashed supervisor (lock lost, child survived). Do NOT raw-taskkill
    // it (defect A) — reuse it instead of spawning a duplicate daemon. Only
    // when nothing answers do we go on to ensure the port is free.
    if (await apiHealthy(apiPort)) {
      console.log(`[dev] live API already on :${apiPort} (orphan, no lock) — reusing its logs.`);
      console.log(`[dev] Ctrl+C detaches. Run \`npm run dev -- --kill\` to stop it and take over.`);
      process.on("SIGINT", () => process.exit(0));
      process.on("SIGTERM", () => process.exit(0));
      await Promise.all([tailLog("api"), tailLog("web")]);
      return;
    }
    await ensurePortFree(apiPort, "API");
  }
  if (!tryWriteLock()) {
    // Lost a boot race with another dev-all — attach to the winner instead
    // of fighting over the port.
    const winner = readLock();
    if (winner) {
      const attached = await attachToDevAll(winner);
      if (attached) return;
    }
    await ensurePortFree(apiPort, "API");
    await ensurePortFree(vitePort, "Vite");
  }
  ownsLock = true;

  if (fastPreview) {
    console.log(
      "[dev:2] VODRIP_PREVIEW_FAST_ONLY=1 — innertube race only (~8s), no cookies/POT/browser/slow fallback",
    );
    console.log("        Best for shorts/simple VODs; 6h titiltei streams need npm run dev\n");
  }
  // Vite's cold source reads overlap FastAPI imports, but the browser remains
  // gated until the API is healthy.
  const prewarm = prewarmSourceFiles();

  // Start the API before exposing Vite to a browser. The UI requests /api
  // immediately on load; starting both children concurrently creates a burst
  // of ECONNREFUSED responses while FastAPI imports and binds port 7897.
  console.log(`Starting API  -> http://localhost:${apiPort}  (/api only)`);
  start("api", pyCmd, [...pyArgsPrefix, "run.py"], pyDir, {
    VODRIP_SKIP_PORT_RELEASE: "1",
    ...previewFastEnv,
    ...(serveBuiltUi ? { KICK_SERVE_UI: "1" } : {}),
  });
  let apiReady = false;
  for (let i = 0; i < 240 && !shuttingDown; i++) {
    if (await apiHealthy(apiPort)) {
      apiReady = true;
      break;
    }
    if (apiProcess && apiProcess.exitCode !== null) break;
    await sleep(500);
  }
  if (!apiReady) {
    console.error(`[dev] API did not become ready on :${apiPort}; refusing to expose the UI`);
    await shutdown(1);
    return;
  }

  // Vite starts only after the API is healthy, preventing startup proxy races.
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

  await reportHealth("health");

  startApiHealthMonitor();
  startAlwaysOnWatchdog();
  startBackendWatcher();
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().catch((err) => {
    console.error(err);
    process.exit(1);
  });
}
