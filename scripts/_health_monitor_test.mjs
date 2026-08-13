// Self-check for the dev-all health monitor: a live-but-unresponsive HTTP
// listener must trip it exactly once; a healthy one must not trip it.
// Run: node scripts/_health_monitor_test.mjs
import http from "node:http";
import assert from "node:assert";
import { startHealthMonitor } from "./dev-all.mjs";

let respond = true;
const server = http.createServer((req, res) => {
  if (respond) {
    res.writeHead(200);
    res.end("{}");
  }
  // else: accept but never answer — simulates a deadlocked API process
});
await new Promise((resolve) => server.listen(0, resolve));
const port = server.address().port;

const trips = [];
const stop = startHealthMonitor({
  port,
  healthIntervalMs: 20,
  maxStrikes: 3,
  healthTimeoutMs: 30,
  onUnhealthy: (strikes) => trips.push(strikes),
});

// Healthy phase: several probes must all pass, zero trips.
await new Promise((r) => setTimeout(r, 150));
assert.strictEqual(trips.length, 0, "healthy server must not trip the monitor");

// Hang phase: server accepts but never responds; apiHealthy times out.
respond = false;
await new Promise((r) => setTimeout(r, 400));
assert.strictEqual(trips.length, 1, "hung server must trip the monitor exactly once");

// Recovery: server answers again; the monitor resets its strike counter.
respond = true;
await new Promise((r) => setTimeout(r, 150));
respond = false;
await new Promise((r) => setTimeout(r, 400));
assert.strictEqual(trips.length, 2, "a second hang episode must trip again (strikes reset)");

stop();
server.close();
console.log("health monitor self-check OK");
