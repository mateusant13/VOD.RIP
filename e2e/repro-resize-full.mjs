import { chromium } from 'playwright';

const browser = await chromium.launch({
  executablePath: 'C:/Users/Administrador/AppData/Local/ms-playwright/chromium_headless_shell-1228/chrome-headless-shell-win64/chrome-headless-shell.exe',
});
const ctx = await browser.newContext({ viewport: { width: 1600, height: 900 } });
const page = await ctx.newPage();

const widths = async (label) => {
  const data = await page.evaluate(() => {
    const row = document.querySelector('.vod-layout-row');
    if (!row) return null;
    return Array.from(row.children).map((p) => {
      const r = p.getBoundingClientRect();
      return { w: Math.round(r.width), h: Math.round(r.height), x: Math.round(r.x) };
    });
  });
  console.log(label, JSON.stringify(data));
  return data;
};

// Grab the target panel's OWN handle at a point that is NOT covered by the
// neighboring panel's overlapping handle (hit-testing picks the topmost).
const dragHandle = async (panelIndex, edge, dx, dy = 0) => {
  const box = await page.evaluate(([pi, e]) => {
    const row = document.querySelector('.vod-layout-row');
    const panels = row ? Array.from(row.children) : [];
    const panel = panels[pi];
    if (!panel) return null;
    const pr = panel.getBoundingClientRect();
    const handle = Array.from(panel.querySelectorAll('[data-panel-resize]')).find((h) => {
      const hr = h.getBoundingClientRect();
      if (e === 'e') return Math.abs(hr.right - pr.right) < 20 && hr.width <= 12;
      if (e === 'w') return Math.abs(hr.left - pr.left) < 20 && hr.width <= 12;
      return false;
    });
    if (!handle) return null;
    const hr = handle.getBoundingClientRect();
    // e: click the right-most 2px (away from the neighbor's west handle overlap)
    // w: click the left-most 2px (away from the neighbor's east handle overlap)
    const x = e === 'e' ? hr.right - 1 : hr.left + 1;
    return { x, y: hr.y + hr.height / 2 };
  }, [panelIndex, edge]);
  if (!box) throw new Error(`no ${edge} handle for panel ${panelIndex}`);
  await page.mouse.move(box.x, box.y);
  await page.mouse.down();
  await page.mouse.move(box.x + dx, box.y + dy, { steps: 15 });
  await page.mouse.up();
  await page.waitForTimeout(400);
};

const widthsOnly = (arr) => arr.map((p) => p.w);
const same = (a, b) => a.length === b.length && a.every((w, i) => Math.abs(w - b[i]) < 6);
const check = (ok, label) => { console.log(`${label}: ${ok ? 'PASS' : 'FAIL'}`); return ok ? 0 : 1; };

let failures = 0;

await page.goto('http://localhost:5173/', { waitUntil: 'domcontentloaded' });
await page.waitForTimeout(2500);
await page.click('text=CHANNELS');
await page.waitForTimeout(800);
await page.click('text=lubu');
await page.waitForTimeout(3000);
await page.click('text=/[ÚU]LTIMO DIA DO MUNDIAL/i');
await page.waitForTimeout(4000);
await page.locator('button:has-text("PREVIEW")').first().click();
await page.waitForTimeout(7000);

const before = await widths('1 SQUARES (0=preview 1=urlAside 2=main):');
const beforeW = widthsOnly(before);

// --- A: push leftmost (preview) right by 300, then back exactly 300 ---
await dragHandle(0, 'e', 300);
const widened = await widths('2 PREVIEW +300:');
failures += check(widened[0].w === before[0].w + 300, 'A1 preview follows pointer exactly (+300)');
failures += check(widened[1].w < before[1].w - 40 && widened[2].w < before[2].w - 40, 'A2 siblings squeezed');
await dragHandle(0, 'e', -300);
const backA = await widths('3 PREVIEW BACK -300:');
failures += check(same(beforeW, widthsOnly(backA)), 'A3 full restore after widen/back');

// --- B: push preview to the max cap, then back to the original width ---
await dragHandle(0, 'e', 700);
const maxed = await widths('4 PREVIEW +700 (capped):');
const capReached = maxed[0].w < before[0].w + 700; // capped below the pointer
failures += check(capReached, 'B1 preview capped by row budget');
failures += check(maxed[1].w <= 241 && maxed[2].w <= 241, 'B2 siblings at min when capped');
// drag back by the ACTUAL delta (width delta-based, not pointer absolute)
const backDx = before[0].w - maxed[0].w;
await dragHandle(0, 'e', backDx);
const backB = await widths('5 PREVIEW BACK:');
failures += check(same(beforeW, widthsOnly(backB)), 'B3 full restore after max/back');

// --- C: drag rightmost (main) wide enough to squeeze, then back ---
await dragHandle(2, 'e', 450);
const mainWide = await widths('6 MAIN +450:');
failures += check(mainWide[2].w === before[2].w + 450, 'C1 main follows pointer exactly');
failures += check(mainWide[0].w < before[0].w - 40 && mainWide[1].w < before[1].w - 40, 'C2 siblings squeezed');
await dragHandle(2, 'e', -450);
const backC = await widths('7 MAIN BACK:');
failures += check(same(beforeW, widthsOnly(backC)), 'C3 full restore after main widen/back');

// --- D: drag middle (urlAside) wide, then back ---
await dragHandle(1, 'e', 300);
const midWide = await widths('8 URLASIDE +300:');
failures += check(midWide[1].w === before[1].w + 300, 'D1 urlAside follows pointer exactly');
await dragHandle(1, 'e', -300);
const backD = await widths('9 URLASIDE BACK:');
failures += check(same(beforeW, widthsOnly(backD)), 'D2 full restore after urlAside widen/back');

// --- E: reverse scenario — drag preview narrower first, then back ---
await dragHandle(0, 'e', -150);
const narrow = await widths('10 PREVIEW -150:');
failures += check(narrow[0].w === before[0].w - 150, 'E1 preview shrinks freely');
await dragHandle(0, 'e', 150);
const backE = await widths('11 PREVIEW BACK +150:');
failures += check(same(beforeW, widthsOnly(backE)), 'E2 restore after narrow/back');

console.log(failures === 0 ? 'ALL SCENARIOS PASS' : `${failures} FAILURES`);
await page.screenshot({ path: 'e2e/resize-final-fixed.png' });
await browser.close();
process.exit(failures === 0 ? 0 : 1);
