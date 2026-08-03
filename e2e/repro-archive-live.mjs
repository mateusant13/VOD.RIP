// Archive "local Google" e2e — asserts the REAL archived Titiltei/lubu data.
// Deterministic in asserts (presence/known values), not in DB state: the
// archive was populated by the real ingestion runs (YouTube adapter, Twitch
// backfill, Kick downloads). Re-runnable: only checks known-good rows.
//
// Usage: node e2e/repro-archive-live.mjs   (backend must be on :7897)
// Port override for isolated verification: ARCHIVE_BASE=http://localhost:7900 node e2e/repro-archive-live.mjs

const BASE = process.env.ARCHIVE_BASE || "http://localhost:7897";
let failures = 0;
const check = (name, cond, extra = "") => {
  if (cond) console.log(`  PASS ${name}`);
  else { failures++; console.log(`  FAIL ${name} ${extra}`); }
};

async function get(path) {
  const r = await fetch(BASE + path);
  if (!r.ok) throw new Error(`${path} -> HTTP ${r.status}`);
  return r.json();
}

console.log("[1] video index: >=3 per platform (youtube TiTiltei, twitch lubu, kick titiltei)");
const { videos } = await get("/api/archive/videos");
// Archive grows over time (more live/VOD rows per channel) — assert floor +
// known-good rows, never exact counts.
check("at least 9 videos", videos.length >= 9, `got ${videos.length}`);
const byPlatform = { youtube: [], twitch: [], kick: [] };
for (const v of videos) byPlatform[v.platform]?.push(v);
check(">=3 youtube TiTiltei", byPlatform.youtube.filter(v => v.channel === "TiTiltei").length >= 3);
check(">=3 twitch lubu", byPlatform.twitch.filter(v => v.channel === "lubu").length >= 3);
check(">=3 kick titiltei ready", byPlatform.kick.length >= 3 && byPlatform.kick.filter(v => v.channel === "titiltei" && v.status === "ready").length >= 3,
  JSON.stringify(byPlatform.kick.map(v => [v.status, v.archive_path])));
const yt = byPlatform.youtube.find(v => v.video_id === "3sCcLEsYw3M");
check("YT stream LOL CLASSICO CHEGOU HOJE present", !!yt);
check("YT canonical_key NFKD format", yt?.canonical_key === "lol-classico-chegou-hoje-pix|2026-07-30", yt?.canonical_key);
const tw = byPlatform.twitch.find(v => v.video_id === "2834554822");
check("Twitch world-cup VOD present", !!tw && /ULTIMO DIA DO MUNDIAL/i.test(tw.title || ""), tw?.title);

console.log("[2] transcript search across real Titiltei streams");
const bronzinhos = await get("/api/archive/search?q=bronzinhos");
check("'bronzinhos' → transcript hit on YT stream", bronzinhos.hits.some(h => h.kind === "transcript" && h.platform === "youtube"),
  JSON.stringify(bronzinhos.hits.slice(0, 2)));
const shaco = await get("/api/archive/search?q=shaco&limit=10");
const shacoHit = shaco.hits.find(h => h.video_id === "3sCcLEsYw3M" && h.kind === "message");
check("'shaco' → chat hits incl. 3sCcLEsYw3M @~8399s", !!shacoHit && Math.abs(shacoHit.offset_sec - 8399) < 30,
  JSON.stringify(shaco.hits.slice(0, 3).map(h => [h.video_id, Math.round(h.offset_sec), h.text])));

console.log("[3] chat window around a hit");
const win = await get(`/api/archive/videos/youtube/3sCcLEsYw3M/chat?offset=${Math.round(shacoHit.offset_sec)}&half=30`);
check("chat window returns rows incl. the hit text", win.messages.length >= 1 && win.messages.some(m => /shaco/i.test(m.text)));

console.log("[4] dedupe view + aliases");
const { groups } = await get("/api/archive/dedupe");
check("at least 9 canonical groups", groups.length >= 9, `got ${groups.length}`);
check("known canonical key present", groups.some(g => g.canonical_key === "lol-classico-chegou-hoje-pix|2026-07-30"),
  groups.map(g => g.canonical_key).join(","));

console.log("[5] jobs queue");
const { jobs } = await get("/api/archive/jobs?limit=50");
const kinds = [...new Set(jobs.map(j => j.kind))].sort();
check("ingest/backfill/transcribe job kinds present", kinds.length >= 2, kinds.join(","));

console.log("[6] cookie bridge surface");
const cs = await get("/api/session/cookies/status");
check("cookie status endpoint 200", cs && typeof cs.paired === "boolean", JSON.stringify(cs));

console.log(failures === 0 ? "\nALL PASS" : `\n${failures} FAILURES`);
// exitCode (not process.exit) so pending fetch handles drain — process.exit
// with live handles trips a libuv abort on Windows.
process.exitCode = failures === 0 ? 0 : 1;
