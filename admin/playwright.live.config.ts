import { defineConfig } from '@playwright/test';
export default defineConfig({
  testDir: './tests/live',
  workers: 1,
  timeout: 60_000,
  use: {
    baseURL: 'http://127.0.0.1:5189',
    headless: true,
    actionTimeout: 10_000,
    trace: 'off',
    viewport: { width: 1440, height: 1000 },
  },
  webServer: {
    command: 'npx vite build && npx vite preview --host 127.0.0.1 --port 5189 --strictPort',
    url: 'http://127.0.0.1:5189',
    reuseExistingServer: false,
  },
});
