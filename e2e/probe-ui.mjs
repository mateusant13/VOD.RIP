// Probe fresh-profile UI structure
import { chromium } from 'playwright-core';
const BASE = 'http://localhost:5174';
const browser = await chromium.launch({ headless: true, executablePath: 'C:/Program Files/Google/Chrome/Application/chrome.exe' });
const page = await browser.newPage({ viewport: { width: 1920, height: 949 } });
await page.goto(BASE, { waitUntil: 'domcontentloaded' });
await page.waitForTimeout(3000);
const info = await page.evaluate(() => {
  const inputs = [...document.querySelectorAll('input')].map((i) => ({ ph: i.placeholder, v: i.value, x: Math.round(i.getBoundingClientRect().x), y: Math.round(i.getBoundingClientRect().y), w: Math.round(i.getBoundingClientRect().width) }));
  const btns = [...document.querySelectorAll('button')].map((b) => ({ t: (b.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 40), y: Math.round(b.getBoundingClientRect().y), x: Math.round(b.getBoundingClientRect().x) })).filter((b) => b.t);
  const panels = [...document.querySelectorAll('div')].filter((d) => d.style && d.style.width && d.style.width.includes('px') && d.getBoundingClientRect().width > 300 && d.getBoundingClientRect().height > 300).map((d) => { const r = d.getBoundingClientRect(); return { x: Math.round(r.x), w: Math.round(r.width), h: Math.round(r.height), cls: (d.className ? d.className.toString() : '').slice(0, 40) }; });
  return { inputs, btns: btns.slice(0, 30), panels: panels.slice(0, 6) };
});
console.log(JSON.stringify(info, null, 1));
await browser.close();