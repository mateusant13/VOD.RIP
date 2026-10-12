// Local e2e config: drives the Chrome that is ALREADY installed on this box.
//
// The bundled Playwright 1.63 expects chromium_headless_shell-1243 while this
// machine only has chromium-1228 cached, so `npx playwright test` fails with
// "Executable doesn't exist" before any test runs - an environment gap, not an
// app result. `channel: 'chrome'` reuses the installed Google Chrome instead of
// downloading a browser, so the user-level specs can actually run here.
import base from './playwright.config';

export default {
  ...base,
  use: {
    ...(base as any).use,
    channel: 'chrome',
    headless: true,
  },
};