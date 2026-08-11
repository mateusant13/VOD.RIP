// Self-check for the clip-editor range logic (background.js helper):
// the ±1s-tight confirmation rejected a window clamped at the VOD edge
// (17:00-17:22 on a ~17:22 VOD never published); the fix widens tolerance
// and retries with the END pulled off the edge. Mirrors the shipped logic.
import assert from 'node:assert/strict';

const pc = (s) => {
  const h = /^(\d+):(\d{1,2}):(\d{2})$/.exec(s);
  if (h) return +h[1] * 3600 + +h[2] * 60 + +h[3];
  const m = /^(\d+):(\d{2})$/.exec(s);
  return m ? +m[1] * 60 + +m[2] : null;
};
const TOL = 3;
const LEN_TOL = 2;

const confirm = (w, s, e) =>
  w &&
  Math.abs(w.a - s) <= TOL && Math.abs(w.b - e) <= TOL &&
  Math.abs((w.b - w.a) - (e - s)) <= LEN_TOL &&
  w.b - w.a >= 5 && w.b - w.a <= 90;

// 1. h:mm:ss parsing (long VODs — the old parser returned null).
assert.equal(pc('17:00'), 1020);
assert.equal(pc('3:20'), 200);
assert.equal(pc('1:02:05'), 3725);
assert.equal(pc('0:05'), 5);

// 2. The failing case now confirms: editor clamps 1042 -> 1040 (VOD edge).
assert.ok(confirm({ a: 1020, b: 1040 }, 1020, 1042), 'edge clamp within tolerance');

// 3. But a real mismatch (handle at 17:00 while target 8:20) still fails.
assert.ok(!confirm({ a: 500, b: 520 }, 1020, 1042), 'wrong position rejected');

// 4. The local editor accepts the full native 90s chunk, but not longer.
assert.ok(confirm({ a: 75, b: 90 }, 75, 90), '15s local range confirms');
assert.ok(confirm({ a: 0, b: 90 }, 0, 90), 'native 90s window confirms');
assert.ok(!confirm({ a: 0, b: 91 }, 0, 91), 'over-long local window rejected');
assert.ok(!confirm({ a: 0, b: 4 }, 0, 4), 'under-long local window rejected');

console.log('clip-range logic OK (8 assertions)');
