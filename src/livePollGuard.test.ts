/**
 * The live-status poll is held for the MAIN preview player only.
 *
 * This is not an oversight, and this test exists so nobody "fixes" it again.
 *
 * On 2026-10-14 a subagent found the real mechanism behind the slow previews
 * with a controlled A/B (same page, same click, same protocol; only the
 * app's own background burst fired 900 ms before it):
 *
 *   burst with real upstream work : 8.13s  queue_max 7374.7ms
 *                                  session POST queued 7339.9ms
 *                                  0 long tasks, main thread IDLE
 *   burst absorbed by cache      : 0.80s / 0.71s
 *   idle                         : 0.75s / 0.73s / 0.72s (queue 2.6-3.1ms)
 *
 * So the preview's own requests were sitting in the browser's 6-per-origin
 * connection queue behind the burst. The subagent proposed extending the
 * existing previewOpen guard to channel explore popups. It predicted 8.13s
 * -> 0.75s.
 *
 * MEASURED, same harness, fresh Chrome profile, onboarding dismissed:
 *
 *   main player only (current)  : 8/12 <=3s  median 2.65s  max 11.64s
 *   + explore popups            : 2/12 <=3s  median 4.42s  max 22.41s
 *
 * TWICE WORSE, the opposite of the prediction. The mechanism: holding the poll
 * while a popup is open lets the LIVE badges go stale, and when the popup
 * closes the poll fires every channel at once - a bigger burst than the
 * incremental one it replaced. The guard converted a trickle into a flood.
 *
 * Do not re-apply this fix without a measurement that beats 8/12.
 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";
import { describe, expect, it } from "vitest";

const HERE = dirname(fileURLToPath(import.meta.url));
const APP = resolve(HERE, "App.tsx");

function livePollEffect(src: string): string {
  const start = src.indexOf("// Poll live status for every saved channel");
  expect(start).toBeGreaterThan(-1);
  const end = src.indexOf("\n  }, [", start);
  expect(end).toBeGreaterThan(start);
  return src.slice(start, end);
}

describe("live-status poll guard stays scoped to the main player", () => {
  it("does NOT extend the guard to explore popups (measured twice worse)", () => {
    const body = livePollEffect(readFileSync(APP, "utf8"));
    expect(body).not.toContain("explorePopups.length > 0) return");
  });

  it("still holds the poll for the main preview player", () => {
    const body = livePollEffect(readFileSync(APP, "utf8"));
    expect(body).toContain("previewOpen) return;");
  });
});
