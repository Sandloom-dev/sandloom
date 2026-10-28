import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: './tests/browser',
  fullyParallel: true,
  workers: process.env.CI ? 2 : 4,
  timeout: 30_000,
  use: {
    baseURL: 'http://127.0.0.1:5179',
    headless: true,
    actionTimeout: 5000,
    viewport: { width: 1280, height: 800 },
    trace: 'retain-on-failure',
  },
  webServer: {
    command: 'npx vite build && npx vite preview --host 127.0.0.1 --port 5179 --strictPort',
    url: 'http://127.0.0.1:5179',
    reuseExistingServer: false,
  },
});
