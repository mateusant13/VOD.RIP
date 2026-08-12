// Self-check for the clip-editor range logic (background.js helper).
// clipOffsets are relative to the ~90s raw media chunk. Native default is
// "1:00 to 1:30" (last 30s). VOD-absolute seconds must be converted before
// onLeftDrag/onRightDrag or the render job hangs at 99%.
import assert from 'node:assert/strict';

const pc = (s) => {
  const h = /^(\d+):(\d{1,2}):(\d{2})$/.exec(s);
  if (h) return +h[1] * 3600 + +h[2] * 60 + +h[3];
  const m = /^(\d+):(\d{2})$/.exec(s);
  if (m) return +m[1] * 60 + +m[2];
  if (/^\d{1,3}$/.test(String(s).trim())) return +s;
  return null;
};
const TOL = 3;
const LEN_TOL = 1;

const confirm = (w, s, e) =>
  w &&
  Math.abs(w.a - s) <= TOL && Math.abs(w.b - e) <= TOL &&
  Math.abs((w.b - w.a) - (e - s)) <= LEN_TOL &&
  w.b - w.a >= 5 && w.b - w.a <= 60;

const toRelative = (start, end, native, urlOffset) => {
  const requestedDur = end - start;
  const uiEnd = (native && native.b > 0 && native.b <= 93) ? native.b : 90;
  const anchor = (urlOffset > 0) ? urlOffset : end;
  const origin = Math.max(0, anchor - uiEnd);
  let relEnd = end - origin;
  let relStart = start - origin;
  if (relEnd > uiEnd) {
    relStart -= (relEnd - uiEnd);
    relEnd = uiEnd;
  }
  if (relStart < 0) relStart = 0;
  return {
    relStart: Math.round(relStart),
    relEnd: Math.round(relEnd),
    requestedDur,
    rawEnd: uiEnd,
    origin,
  };
};

const vodFromEditor = (requested, editor) => {
  if (editor.end <= 93 && requested.end > editor.end + 2) {
    const origin = Math.max(0, requested.end - editor.end);
    return { start: origin + editor.start, end: origin + editor.end };
  }
  return { start: editor.start, end: editor.end };
};

assert.equal(pc('17:00'), 1020);
assert.equal(pc('3:20'), 200);
assert.equal(pc('1:02:05'), 3725);
assert.equal(pc('0:05'), 5);
assert.equal(pc('17:34'), 1054);
assert.equal(pc('17:46'), 1066);
assert.equal(pc('1:00'), 60);
assert.equal(pc('1:30'), 90);

// Live bug (2026-08-12): target 17:34-17:46 (12s) with native "1:00 to 1:30".
// Absolute drag showed "17:34 to 17:46" and published a broken clip.
const live = toRelative(1054, 1066, { a: 60, b: 90 }, 1066);
assert.equal(live.origin, 976);
assert.equal(live.relStart, 78);
assert.equal(live.relEnd, 90);
assert.ok(confirm({ a: 78, b: 90 }, 78, 90), '12s relative window confirms');
assert.ok(!confirm({ a: 1054, b: 1066 }, 78, 90), 'VOD-absolute valuetext is not a confirmed relative window');

const late = toRelative(871, 890, { a: 60, b: 90 }, 890);
assert.equal(late.origin, 800);
assert.equal(late.relStart, 71);
assert.equal(late.relEnd, 90);
assert.deepEqual(vodFromEditor({ start: 871, end: 890 }, { start: 71, end: 90 }), { start: 871, end: 890 });
assert.notEqual(vodFromEditor({ start: 871, end: 890 }, { start: 71, end: 90 }).end, 90);

const early = toRelative(10, 29, { a: 0, b: 90 }, 29);
assert.equal(early.origin, 0);
assert.equal(early.relStart, 10);
assert.equal(early.relEnd, 29);

// Live bug (2026-08-12): 14s at VOD start. Twitch loads ~16.5s of HLS so the
// player clock reads 0:17. Mapping must stay 0-14; 16s pad must not confirm.
const startPad = toRelative(0, 14, { a: 0, b: 17 }, 14);
assert.equal(startPad.origin, 0);
assert.equal(startPad.relStart, 0);
assert.equal(startPad.relEnd, 14);
assert.ok(confirm({ a: 0, b: 14 }, 0, 14), '14s window confirms');
assert.ok(!confirm({ a: 0, b: 16 }, 0, 14), '16s HLS pad must not confirm as 14s');

assert.ok(confirm({ a: 60, b: 90 }, 60, 90), 'native 30s default confirms');
assert.ok(!confirm({ a: 0, b: 91 }, 0, 91), 'over-long local window rejected');
assert.ok(!confirm({ a: 0, b: 4 }, 0, 4), 'under-long local window rejected');
assert.ok(!confirm({ a: 500, b: 520 }, 78, 90), 'wrong position rejected');


const mid = toRelative(80, 93, { a: 60, b: 90 }, 93);
assert.equal(mid.origin, 3);
assert.equal(mid.relStart, 77);
assert.equal(mid.relEnd, 90);
assert.deepEqual(vodFromEditor({ start: 80, end: 93 }, { start: 77, end: 90 }), { start: 80, end: 93 });

console.log('clip-range logic OK (relative 0-90 offsets)');
