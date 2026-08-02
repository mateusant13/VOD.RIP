import { chromium } from 'playwright';
const browser = await chromium.launch({
  executablePath: 'C:/Users/Administrador/AppData/Local/ms-playwright/chromium_headless_shell-1228/chrome-headless-shell-win64/chrome-headless-shell.exe',
});
const ctx = await browser.newContext({ viewport: { width: 1600, height: 900 } });
const page = await ctx.newPage();

const drive = async (n) => {
  await page.goto('http://localhost:5173/', { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(2500);
  await page.click('text=CHANNELS');
  await page.waitForTimeout(800);
  await page.click('text=lubu');
  await page.waitForTimeout(3000);
  await page.click('text=/[ÚU]LTIMO DIA DO MUNDIAL/i', { timeout: 20000 });
  await page.waitForTimeout(4000);
  await page.locator('button:has-text("PREVIEW")').first().click({ timeout: 20000 });
  await page.waitForTimeout(7000);
  console.log(`drive${n}:`, JSON.stringify(await widths()));
};
const widths = async () => page.evaluate(() => {
  const row = document.querySelector('.vod-layout-row');
  return row ? Array.from(row.children).map((p) => Math.round(p.getBoundingClientRect().width)) : null;
});
const dragPreview = async (dx) => {
  const box = await page.evaluate(() => {
    const row = document.querySelector('.vod-layout-row');
    const panel = row.children[0];
    const pr = panel.getBoundingClientRect();
    const handle = Array.from(panel.querySelectorAll('[data-panel-resize]')).find((h) => {
      const hr = h.getBoundingClientRect();
      return Math.abs(hr.right - pr.right) < 20 && hr.width <= 12;
    });
    const hr = handle.getBoundingClientRect();
    return { x: hr.right - 1, y: hr.y + hr.height / 2 };
  });
  await page.mouse.move(box.x, box.y);
  await page.mouse.down();
  await page.mouse.move(box.x + dx, box.y, { steps: 15 });
  await page.mouse.up();
  await page.waitForTimeout(600);
};

await drive(1);
await dragPreview(150);
console.log('after +150:', JSON.stringify(await widths()));
await page.reload({ waitUntil: 'domcontentloaded' });
await drive(2);
const ok1 = JSON.stringify(await widths()) === JSON.stringify([790, 270, 420]) ||
  JSON.stringify(await widths()) === JSON.stringify([790, 288, 402]) ||
  JSON.stringify(await widths()) === JSON.stringify([790, 270, 402]);
console.log('RELOAD RESTORES DRAGGED STATE:', ok1 ? 'PASS' : 'FAIL');
await dragPreview(-150);
console.log('after back:', JSON.stringify(await widths()));
await page.reload({ waitUntil: 'domcontentloaded' });
await drive(3);
const ok2 = JSON.stringify(await widths()) === JSON.stringify([640, 288, 448]);
console.log('RELOAD RESTORES SQUARES:', ok2 ? 'PASS' : 'FAIL');
await browser.close();
process.exit(ok1 && ok2 ? 0 : 1);
