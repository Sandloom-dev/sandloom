// Manual live check against a real backend. Intentionally no embedded
// credentials: SANDLOOM_ADMIN_PREVIEW_CREDS points to a private local JSON file
// ({"email","password"}), never a checked-in account.
import fs from 'node:fs';
import { chromium } from '@playwright/test';

const credentials = JSON.parse(fs.readFileSync(process.env.SANDLOOM_ADMIN_PREVIEW_CREDS, 'utf8'));
const base = process.env.SANDLOOM_ADMIN_PREVIEW_URL ?? 'http://127.0.0.1:5187/';
const browser = await chromium.launch({ headless: true });
try {
  const page = await browser.newPage();
  await page.goto(base);
  await page.getByLabel('Email').fill(credentials.email);
  await page.getByLabel('Password').fill(credentials.password);
  await page.getByRole('button', { name: 'Sign in', exact: true }).click();
  await page.getByRole('heading', { name: 'Overview', exact: true }).waitFor();
  const nav = page.getByRole('navigation', { name: 'Primary' });
  for (const [link, table] of [
    ['Users', 'Users'],
    ['All organizations', 'All organizations'],
    ['Billing customers', 'Billing customers'],
  ]) {
    await nav.getByRole('link', { name: link, exact: true }).click();
    await page.getByRole('table', { name: table }).waitFor();
  }
  await nav.getByRole('link', { name: 'Service health' }).click();
  await page.getByRole('heading', { name: 'Service health', exact: true }).waitFor();
  await page.screenshot({ path: 'test-results/live-admin-preview.png', fullPage: true });
  console.log('Live login, users, organizations, billing customers and service health: PASS');
} finally {
  await browser.close();
}
