import { expect, test, type Page } from '@playwright/test';
import AxeBuilder from '@axe-core/playwright';

/** A generated application, real wheels, real backend. No route mocks. */
const required = (key: string) => {
  const value = process.env[key];
  if (!value) throw new Error(`Missing package check setting: ${key}`);
  return value;
};
const email = required('SANDLOOM_CONSOLE_ADMIN');
const password = required('SANDLOOM_CONSOLE_PASSWORD');
const modules = required('SANDLOOM_CONSOLE_MODULES').split(',');

function watchProblems(page: Page) {
  const problems: string[] = [];
  page.on('pageerror', (error) => problems.push(`pageerror: ${error.message}`));
  page.on('console', (message) => {
    // A refused sign-in logs its 401/403 as a failed resource; that is the expected path.
    if (message.type() === 'error' && !/status of 4\d\d/.test(message.text()))
      problems.push(`console: ${message.text()}`);
  });
  page.on('response', (response) => {
    if (response.status() >= 404) problems.push(`${response.status()}: ${response.url()}`);
  });
  page.on('requestfailed', (request) => {
    // Moving to the next screen cancels the previous screen's requests.
    if (request.failure()?.errorText !== 'net::ERR_ABORTED')
      problems.push(`failed: ${request.url()}`);
  });
  page.on('request', (request) => {
    if (new URL(request.url()).origin !== required('SANDLOOM_CONSOLE_ORIGIN'))
      problems.push(`foreign: ${request.url()}`);
  });
  return problems;
}

async function signIn(page: Page, address = email) {
  await page.goto('/admin/');
  await page.getByLabel('Email', { exact: true }).fill(address);
  await page.getByLabel('Password', { exact: true }).fill(password);
  // Production limits sign-ins; the check signs in many times, an operator wouldn't.
  for (let attempt = 0; attempt < 5; attempt += 1) {
    await page.getByRole('button', { name: 'Sign in', exact: true }).click();
    const limited = page.getByRole('alert').filter({ hasText: 'Rate limit' });
    const done = await limited
      .waitFor({ timeout: 2_000 })
      .then(() => false)
      .catch(() => true);
    if (done) return;
    await page.waitForTimeout(30_000);
  }
}

test('an administrator signs in and the console stays under /admin/', async ({ page }) => {
  const problems = watchProblems(page);
  await signIn(page);
  await expect(page.getByRole('heading', { name: 'Overview', exact: true })).toBeVisible();
  expect(new URL(page.url()).pathname.startsWith('/admin')).toBe(true);
  const nav = page.getByRole('navigation', { name: 'Primary' });
  await nav.getByRole('link', { name: 'Users', exact: true }).first().click();
  await expect(page).toHaveURL(/\/admin\/.+/);
  await expect(page.getByText(email).first()).toBeVisible();
  expect(problems).toEqual([]);
});

test('a deep link survives a reload', async ({ page }) => {
  const problems = watchProblems(page);
  await signIn(page);
  await expect(page.getByRole('heading', { name: 'Overview', exact: true })).toBeVisible();
  const nav = page.getByRole('navigation', { name: 'Primary' });
  await nav.getByRole('link', { name: 'Users', exact: true }).first().click();
  await expect(page).toHaveURL(/\/admin\/.+/);
  const deep = page.url();
  await page.reload();
  expect(page.url()).toBe(deep);
  await expect(page.getByLabel('Email', { exact: true })).toBeVisible();
  expect(problems).toEqual([]);
});

test('every screen in the menu opens without a failed request', async ({ page }) => {
  const problems = watchProblems(page);
  await signIn(page);
  await expect(page.getByRole('heading', { name: 'Overview', exact: true })).toBeVisible();
  const links = page.getByRole('navigation', { name: 'Primary' }).getByRole('link');
  const count = await links.count();
  expect(count).toBeGreaterThan(3);
  for (let index = 0; index < count; index += 1) {
    const link = links.nth(index);
    const label = await link.innerText();
    await link.click();
    await page.waitForLoadState('networkidle');
    await expect(page.getByRole('heading').first(), label).toBeVisible();
    await expect(page.getByText('Something went wrong'), label).toHaveCount(0);
    expect(new URL(page.url()).pathname.startsWith('/admin'), label).toBe(true);
  }
  expect(problems).toEqual([]);
});

test('the menu shows only the composed modules', async ({ page }) => {
  await signIn(page);
  await expect(page.getByRole('heading', { name: 'Overview', exact: true })).toBeVisible();
  const nav = page.getByRole('navigation', { name: 'Primary' });
  const text = await nav.innerText();
  expect(/organizations/i.test(text)).toBe(modules.includes('tenancy'));
  expect(/Subscriptions|Credit/.test(text)).toBe(modules.includes('billing'));
});

test('an application module works without any build', async ({ page, request }) => {
  const label = process.env.SANDLOOM_CONSOLE_APP_RESOURCE;
  test.skip(!label, 'this application declares no admin screen of its own');
  const signedIn = await request.post('/auth/login', { data: { email, password } });
  const session = (await signedIn.json()) as { access_token: string };
  const headers = { Authorization: `Bearer ${session.access_token}` };
  const created = await request.post('/api/v1/orgs', {
    headers: { ...headers, 'Idempotency-Key': crypto.randomUUID() },
    data: { name: 'Package check', slug: `check-${Date.now()}` },
  });
  expect(created.status(), await created.text()).toBe(201);
  const organization = (await created.json()) as { id: string };

  await signIn(page);
  await expect(page.getByRole('heading', { name: 'Overview', exact: true })).toBeVisible();
  const nav = page.getByRole('navigation', { name: 'Primary' });
  await nav.getByRole('link', { name: label!, exact: true }).first().click();
  await page.getByRole('textbox', { name: 'Organization' }).fill(organization.id);
  await page.getByRole('textbox', { name: 'Organization' }).press('Enter');
  await page
    .getByRole('link', { name: /^(New|Create)/ })
    .first()
    .click();
  await page.getByLabel('Title').fill('Made in the console');
  await page.getByRole('button', { name: /^(Create|Save)/ }).click();
  await nav.getByRole('link', { name: label!, exact: true }).first().click();
  await page.getByRole('textbox', { name: 'Organization' }).fill(organization.id);
  await page.getByRole('textbox', { name: 'Organization' }).press('Enter');
  await expect(page.getByText('Made in the console').first()).toBeVisible();
});

test('an account without the administrator role gets no screens', async ({ page }) => {
  await signIn(page, required('SANDLOOM_CONSOLE_MEMBER'));
  await expect(page.getByRole('heading', { name: 'Overview', exact: true })).toBeHidden();
  await expect(page.getByRole('alert').first()).toBeVisible();
});

test('it works on a phone and has no serious accessibility problem', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await signIn(page);
  await expect(page.getByRole('heading', { name: 'Overview', exact: true })).toBeVisible();
  const scroll = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
  );
  expect(scroll).toBeLessThanOrEqual(0);
  const { violations } = await new AxeBuilder({ page }).analyze();
  expect(violations.filter((v) => v.impact === 'critical' || v.impact === 'serious')).toEqual([]);
});
