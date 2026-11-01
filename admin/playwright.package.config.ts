import { defineConfig } from '@playwright/test';

/** The console as a package serves it: tools/smoke/admin_package_check.py starts the app. */
export default defineConfig({
  testDir: './tests/package',
  workers: 1,
  timeout: 240_000,
  use: {
    baseURL: process.env.SANDLOOM_CONSOLE_ORIGIN,
    headless: true,
    actionTimeout: 10_000,
    trace: 'off',
    viewport: { width: 1440, height: 1000 },
  },
});
