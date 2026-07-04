import { defineConfig } from '@playwright/test';

const API_PORT = process.env.PORT || '7897';
const UI_PORT = '5173';

export default defineConfig({
  testDir: './tests',
  timeout: 60_000,
  expect: {
    timeout: 10_000,
  },
  use: {
    baseURL: `http://localhost:${UI_PORT}`,
    headless: true,
    screenshot: 'only-on-failure',
    trace: 'retain-on-failure',
  },
  webServer: [
    {
      command: `cd ../backend && python run.py`,
      port: Number(API_PORT),
      timeout: 30_000,
      reuseExistingServer: !process.env.CI,
      cwd: '.',
    },
    {
      command: 'npx vite --port 5173',
      port: Number(UI_PORT),
      timeout: 30_000,
      reuseExistingServer: !process.env.CI,
      cwd: '..',
    },
  ],
  projects: [
    {
      name: 'chromium',
      use: {
        browserName: 'chromium',
        launchOptions: {
          args: ['--no-sandbox', '--disable-setuid-sandbox'],
        },
      },
    },
  ],
});
