import { test, expect, type Page } from '@playwright/test';
import AxeBuilder from '@axe-core/playwright';
import catalogManifest from '../fixtures/catalog-manifest.json' with { type: 'json' };
import builtinManifest from '../../public/admin-manifest.json' with { type: 'json' };
import { JORDAN, MockApi, RILEY, signIn } from './mock-api';

async function expectAccessible(page: Page, label: string) {
  const results = await new AxeBuilder({ page })
    .withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa'])
    .analyze();
  expect(results.violations, `${label}: accessibility violations`).toEqual([]);
}

const nav = (page: Page) => page.getByRole('navigation', { name: 'Primary' });

test('operator signs in to the overview; tokens never reach browser storage', async ({ page }) => {
  const api = new MockApi();
  await signIn(page, api);
  await expect(page.getByRole('heading', { name: 'Overview', exact: true })).toBeVisible();
  await expect(page.locator('html')).toHaveAttribute('lang', 'en');
  await expect(page.getByText('1 webhook event failed processing')).toBeVisible();
  const storage = await page.evaluate(() =>
    JSON.stringify({
      local: Object.entries(localStorage),
      session: Object.entries(sessionStorage),
    }),
  );
  expect(storage).not.toContain('synthetic-refresh');
  expect(storage).not.toContain('x.');
  await expectAccessible(page, 'overview');
  await page.screenshot({ path: 'test-results/overview.png', fullPage: true });
});

test('accounts without the platform admin role are refused at sign-in', async ({ page }) => {
  const api = new MockApi();
  api.admin = false;
  await signIn(page, api);
  await expect(page.getByRole('alert')).toContainText('Platform administrator access is required');
  await expect(nav(page)).toHaveCount(0);
});

test('users list: server-side search, filter chips and a saved view', async ({ page }) => {
  const api = new MockApi();
  await signIn(page, api);
  await nav(page).getByRole('link', { name: 'Users' }).click();
  await expect(page.getByRole('table', { name: 'Users' })).toContainText('Riley Moss');
  await page.getByRole('searchbox', { name: 'Search' }).fill('riley');
  await expect(page.getByRole('table', { name: 'Users' })).not.toContainText('Jordan Blake');
  expect(api.queries.some((q) => q.startsWith('/api/v1/users?') && q.includes('q=riley'))).toBe(
    true,
  );
  await expect(page).toHaveURL(/f\.q=riley/);
  await page.getByRole('button', { name: 'Filter' }).click();
  await page.getByLabel('Field').selectOption({ label: 'Active' });
  await page.getByLabel('Value').selectOption('true');
  await page.getByRole('button', { name: 'Apply filter' }).click();
  await expect(page.getByRole('button', { name: /Active is Yes/ })).toBeVisible();
  await page.getByRole('button', { name: 'Save view' }).click();
  await page.getByLabel('View name').fill('Active Riley');
  await page.getByRole('button', { name: 'Save', exact: true }).click();
  await expect(page.getByRole('navigation', { name: 'Saved views' })).toContainText('Active Riley');
  await page.getByRole('button', { name: 'Remove Active filter' }).click();
  await expect(page).not.toHaveURL(/f\.is_active/);
  await expectAccessible(page, 'users list');
});

test('block flow: reason + typed confirmation, idempotency key, audited, then unblock', async ({
  page,
}) => {
  const api = new MockApi();
  await signIn(page, api);
  await nav(page).getByRole('link', { name: 'Users' }).click();
  await page.getByRole('link', { name: 'Riley Moss' }).click();
  await expect(page.getByRole('heading', { name: 'Riley Moss', level: 1 })).toBeVisible();
  await page.getByRole('button', { name: 'Block user' }).click();
  const dialog = page.getByRole('alertdialog');
  const confirm = dialog.getByRole('button', { name: 'Block user' });
  await expect(confirm).toBeDisabled();
  await dialog.getByLabel(/Reason/).fill('Credential stuffing, ticket SEC-1');
  await expect(confirm).toBeDisabled();
  await dialog.getByLabel(/to confirm/).fill('riley@northwind.example');
  await expectAccessible(page, 'block dialog');
  await confirm.click();
  await expect(page.getByRole('status').filter({ hasText: 'This user is blocked' })).toBeVisible();
  expect(api.writes).toEqual([
    {
      method: 'POST',
      path: `/api/v1/users/${RILEY}/block`,
      body: { reason: 'Credential stuffing, ticket SEC-1' },
      idempotencyKey: expect.stringMatching(/^[0-9a-f-]{36}$/) as unknown as string,
    },
  ]);
  await expect(page.getByText('auth.user.blocked')).toBeVisible();
  await page.getByRole('button', { name: 'Unblock user' }).click();
  await page
    .getByRole('alertdialog')
    .getByLabel(/Reason/)
    .fill('Verified owner, ticket SEC-1');
  await page.getByRole('alertdialog').getByRole('button', { name: 'Unblock user' }).click();
  await expect(page.getByRole('button', { name: 'Block user' })).toBeVisible();
  expect(api.writes[1]?.idempotencyKey).not.toBe(api.writes[0]?.idempotencyKey);
});

test('a lost response shows unknown outcome and retries with the SAME reference', async ({
  page,
}) => {
  const api = new MockApi();
  await signIn(page, api);
  await nav(page).getByRole('link', { name: 'Users' }).click();
  await page.getByRole('link', { name: 'Jordan Blake' }).click();
  await page.getByRole('button', { name: 'Revoke sessions' }).click();
  const dialog = page.getByRole('alertdialog');
  await dialog.getByLabel(/Reason/).fill('Lost laptop');
  api.failNextWrite = { status: 502, detail: 'Bad gateway' };
  await dialog.getByRole('button', { name: 'Revoke sessions' }).click();
  await expect(dialog.getByText(/could not confirm whether/)).toBeVisible();
  await dialog.getByRole('button', { name: 'Retry with same reference' }).click();
  await expect(page.getByRole('alertdialog')).toHaveCount(0);
  expect(api.writes).toHaveLength(2);
  expect(api.writes[0]?.idempotencyKey).toBe(api.writes[1]?.idempotencyKey);
  expect(api.writes[0]?.path).toBe(`/api/v1/users/${JORDAN}/revoke-sessions`);
  expect(api.audit.filter((row) => row.resource_id === JORDAN)).toHaveLength(1);
});

test('bulk command runs once per record with per-record idempotency keys', async ({ page }) => {
  const api = new MockApi();
  await signIn(page, api);
  await nav(page).getByRole('link', { name: 'Users' }).click();
  await page.getByRole('checkbox', { name: 'Select row 2' }).click();
  await page.getByRole('checkbox', { name: 'Select row 3' }).click();
  const bulk = page.getByRole('region', { name: 'Bulk actions' });
  await expect(bulk).toContainText('2 selected');
  await bulk.getByRole('button', { name: 'Revoke sessions…' }).click();
  const dialog = page.getByRole('alertdialog');
  await dialog.getByLabel(/Reason/).fill('Incident 42');
  await dialog.getByRole('button', { name: 'Revoke sessions (2)' }).click();
  await expect(page.getByRole('alertdialog')).toHaveCount(0);
  await expect(page.getByRole('region', { name: 'Bulk actions' })).toHaveCount(0);
  expect(api.writes.map((w) => w.path).sort()).toEqual(
    [`/api/v1/users/${JORDAN}/revoke-sessions`, `/api/v1/users/${RILEY}/revoke-sessions`].sort(),
  );
  const [first, second] = api.writes.map((w) => w.idempotencyKey ?? '');
  expect(first?.split(':')[0]).toBe(second?.split(':')[0]);
  expect(first).not.toBe(second);
});

test('command palette finds a user by name and opens the record', async ({ page }) => {
  const api = new MockApi();
  await signIn(page, api);
  await expect(page.getByRole('heading', { name: 'Overview' })).toBeVisible();
  await page.keyboard.press('ControlOrMeta+k');
  const palette = page.getByRole('dialog', { name: 'Command palette' });
  await expect(palette).toBeVisible();
  await page.keyboard.type('riley');
  await palette.getByRole('option', { name: /Riley Moss/ }).click();
  await expect(page.getByRole('heading', { name: 'Riley Moss', level: 1 })).toBeVisible();
  await page.keyboard.press('ControlOrMeta+k');
  await page.keyboard.type('Audit log');
  await page.keyboard.press('Enter');
  await expect(page).toHaveURL(/\/platform\/audit/);
});

test('theme follows the operator choice and dark mode stays accessible', async ({ page }) => {
  const api = new MockApi();
  await signIn(page, api);
  await page.getByRole('button', { name: /account menu/i }).click();
  await page.getByRole('menuitem', { name: 'Dark' }).click();
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark');
  await nav(page).getByRole('link', { name: 'Users' }).click();
  await expect(page.getByRole('table', { name: 'Users' })).toContainText('Riley Moss');
  await expectAccessible(page, 'users list (dark)');
  await page.screenshot({ path: 'test-results/users-dark.png', fullPage: true });
});

test('keyboard: skip to search, escape closes dialogs without running commands', async ({
  page,
}) => {
  const api = new MockApi();
  await signIn(page, api);
  await expect(page.getByRole('heading', { name: 'Overview' })).toBeVisible();
  await page.keyboard.press('Tab');
  await expect(page.getByRole('link', { name: 'Skip to content' })).toBeFocused();
  await page.keyboard.press('Tab');
  await expect(page.getByRole('button', { name: /Search or jump to/ })).toBeFocused();
  await nav(page).getByRole('link', { name: 'Users' }).click();
  await page.getByRole('link', { name: 'Riley Moss' }).click();
  await page.getByRole('button', { name: 'Block user' }).click();
  await page.keyboard.press('Escape');
  await expect(page.getByRole('alertdialog')).toHaveCount(0);
  expect(api.writes).toHaveLength(0);
});

test('a revoked session drops sensitive screens and requires a new login', async ({ page }) => {
  const api = new MockApi();
  await signIn(page, api);
  await expect(page.getByRole('heading', { name: 'Overview' })).toBeVisible();
  api.revoked = true;
  await nav(page).getByRole('link', { name: 'Users' }).click();
  await expect(page.getByRole('button', { name: 'Sign in', exact: true })).toBeVisible();
  await expect(page.getByRole('table', { name: 'Users' })).toHaveCount(0);
});

test('organizations: platform admins see the directory but no tenant authority', async ({
  page,
}) => {
  const api = new MockApi();
  await signIn(page, api);
  await nav(page).getByRole('link', { name: 'All organizations' }).click();
  await page.getByRole('link', { name: 'Northwind Labs' }).click();
  await expect(page.getByRole('heading', { name: 'Northwind Labs', level: 1 })).toBeVisible();
  // The platform directory is read-only: no tenant management controls.
  await expect(page.getByRole('button', { name: 'Save name' })).toHaveCount(0);
  await expect(
    page.getByRole('main').getByRole('link', { name: 'Billing customer' }),
  ).toHaveAttribute('href', /f\.organization_id=org-alpha/);
  await nav(page).getByRole('link', { name: 'Service health' }).click();
  await expect(page.getByRole('table', { name: 'identity checks' })).toContainText('database');
});

test('module configuration hides absent sections without rebuilding', async ({ page }) => {
  await page.route('**/config.json', (route) => route.fulfill({ json: { modules: ['identity'] } }));
  const api = new MockApi();
  await signIn(page, api);
  await expect(nav(page).getByRole('link', { name: 'Users' })).toBeVisible();
  await expect(nav(page).getByRole('link', { name: 'All organizations' })).toHaveCount(0);
  await expect(nav(page).getByRole('link', { name: 'Billing customers' })).toHaveCount(0);
});

test('a new module gets list, create form and a typed-confirmation command from its manifest', async ({
  page,
}) => {
  await page.route('**/admin-manifest.json', (route) =>
    route.fulfill({
      json: {
        version: 1,
        modules: [...builtinManifest.modules, 'catalog'],
        resources: [...builtinManifest.resources, ...catalogManifest.resources],
      },
    }),
  );
  await page.route('**/config.json', (route) =>
    route.fulfill({ json: { modules: ['identity', 'tenancy', 'billing', 'catalog'] } }),
  );
  const api = new MockApi();
  await signIn(page, api);
  let item: Record<string, unknown> | null = null;
  const writes: { path: string; data: unknown; key: string | undefined }[] = [];
  await page.route('**/api/v1/catalog**', async (route) => {
    const req = route.request();
    const path = new URL(req.url()).pathname;
    if (req.method() === 'GET')
      return route.fulfill({ json: path === '/api/v1/catalog' ? (item ? [item] : []) : item });
    writes.push({ path, data: req.postDataJSON(), key: req.headers()['idempotency-key'] });
    if (path.endsWith('/state')) {
      item = { ...item, ...(req.postDataJSON() as object) };
      return route.fulfill({ status: 204 });
    }
    item = { id: 'new-item', ...(req.postDataJSON() as object) };
    return route.fulfill({ json: item });
  });
  await nav(page).getByRole('link', { name: 'Catalog' }).click();
  await expect(page.getByText('No catalog yet')).toBeVisible();
  await page.getByRole('link', { name: 'New' }).click();
  await page.getByLabel('Name').fill('New product');
  await expect(page.getByLabel('Quantity')).toHaveValue('0');
  await page.getByRole('button', { name: 'Create' }).click();
  await expect(page.getByRole('heading', { name: 'New product', level: 1 })).toBeVisible();
  expect(writes[0]).toMatchObject({
    path: '/api/v1/catalog',
    data: { name: 'New product', state: 'draft', quantity: 0, enabled: false },
  });
  await page.getByRole('button', { name: 'Change state' }).click();
  const dialog = page.getByRole('alertdialog');
  await dialog.getByLabel(/^State/).selectOption('active');
  await expect(dialog.getByRole('button', { name: 'Change state' })).toBeDisabled();
  await dialog.getByLabel(/to confirm/).fill('New product');
  await dialog.getByRole('button', { name: 'Change state' }).click();
  await expect(page.getByRole('alertdialog')).toHaveCount(0);
  expect(writes[1]).toMatchObject({
    path: '/api/v1/catalog/new-item/state',
    data: { state: 'active' },
  });
  expect(writes[1]?.key).toMatch(/^[0-9a-f-]{36}$/);
});

test('tenant-scoped resources require an explicit tenant and isolate its data', async ({
  page,
}) => {
  const scoped = structuredClone(catalogManifest.resources[0]!);
  const resource = { ...scoped, scope: { parameter: null, label: 'Organization ID' } };
  await page.route('**/admin-manifest.json', (route) =>
    route.fulfill({
      json: {
        version: 1,
        modules: [...builtinManifest.modules, 'catalog'],
        resources: [...builtinManifest.resources, resource],
      },
    }),
  );
  await page.route('**/config.json', (route) =>
    route.fulfill({ json: { modules: ['identity', 'tenancy', 'billing', 'catalog'] } }),
  );
  const api = new MockApi();
  await signIn(page, api);
  const tenants: (string | undefined)[] = [];
  const alpha = '00000000-0000-4000-8000-0000000000a1';
  await page.route('**/api/v1/catalog**', (route) => {
    const tenant = route.request().headers()['x-tenant-id'];
    tenants.push(tenant);
    return route.fulfill({
      json: [
        {
          id: 'item',
          name: `Data of ${tenant ?? 'none'}`,
          state: 'draft',
          quantity: 0,
          enabled: false,
        },
      ],
    });
  });
  await nav(page).getByRole('link', { name: 'Catalog' }).click();
  await expect(page.getByLabel('Organization ID')).toBeVisible();
  expect(tenants).toHaveLength(0);
  await page.getByLabel('Organization ID').fill(alpha);
  await page.getByRole('button', { name: 'Open' }).click();
  await expect(page.getByText(`Data of ${alpha}`)).toBeVisible();
  expect(tenants).toEqual([alpha]);
});

test('operator guide explains platform and financial boundaries', async ({ page }) => {
  const api = new MockApi();
  await signIn(page, api);
  await nav(page).getByRole('link', { name: 'Operator guide' }).click();
  await expect(page.getByRole('heading', { name: 'Operator guide', level: 1 })).toBeVisible();
  await expect(page.getByText(/Credit adjustments affect credits only/)).toBeVisible();
  await expect(page.getByRole('heading', { name: 'When a result is uncertain' })).toBeVisible();
  await expectAccessible(page, 'help');
});

test('phone layout: navigation moves into a drawer', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const api = new MockApi();
  await signIn(page, api);
  await expect(page.getByRole('heading', { name: 'Overview' })).toBeVisible();
  await page.getByRole('button', { name: 'Open navigation' }).click();
  await page
    .getByRole('dialog', { name: 'Navigation' })
    .getByRole('link', { name: 'Users' })
    .click();
  await expect(page.getByRole('table', { name: 'Users' })).toBeVisible();
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth > window.innerWidth,
  );
  expect(overflow).toBe(false);
});

test('event delivery: requeue outbox dead letters with a reason and idempotency key', async ({
  page,
}) => {
  const api = new MockApi();
  await signIn(page, api);
  await expect(page.getByText('1 outbox event dead-lettered')).toBeVisible();
  await page.getByText('1 outbox event dead-lettered').click();
  await expect(page).toHaveURL(/platform\/events\?source=billing/);
  const table = page.getByRole('table', { name: 'Outbox dead letters' });
  await expect(table).toContainText('billing.credits.spent.v1');
  await expectAccessible(page, 'event delivery');
  await table.getByRole('checkbox', { name: 'Select row 1' }).click();
  await page.getByRole('button', { name: 'Requeue 1…' }).click();
  const dialog = page.getByRole('alertdialog');
  await expect(dialog.getByRole('button', { name: 'Requeue' })).toBeDisabled();
  await dialog.getByLabel(/Reason/).fill('Broker recovered, INC-7');
  await dialog.getByRole('button', { name: 'Requeue' }).click();
  await expect(page.getByRole('alertdialog')).toHaveCount(0);
  expect(api.writes.at(-1)).toMatchObject({
    method: 'POST',
    path: '/api/v1/platform/billing/outbox/requeue',
    body: { ids: ['3b1e0000-0000-4000-8000-00000000c9d0'], reason: 'Broker recovered, INC-7' },
  });
  expect(api.writes.at(-1)?.idempotencyKey).toMatch(/^[0-9a-f-]{36}$/);
  await expect(page.getByText('No dead letters')).toBeVisible();
});

test('event delivery: Streams replay is targeted at one consumer group', async ({ page }) => {
  const api = new MockApi();
  await signIn(page, api);
  await nav(page).getByRole('link', { name: 'Event delivery' }).click();
  await page
    .getByRole('group', { name: 'Source' })
    .getByRole('button', { name: 'billing' })
    .click();
  await page.getByRole('tab', { name: /Consumers/ }).click();
  await expect(page.getByRole('table', { name: 'Event consumers' })).toContainText('reconnecting');
  await page.getByRole('tab', { name: 'Streams DLQ' }).click();
  const table = page.getByRole('table', { name: 'Streams dead letters' });
  await expect(table).toContainText('billing:other@sub');
  await table.getByRole('checkbox', { name: 'Select row 1' }).click();
  await table.getByRole('checkbox', { name: 'Select row 3' }).click();
  await expect(page.getByRole('button', { name: 'Replay 2…' })).toBeDisabled();
  await table.getByRole('checkbox', { name: 'Select row 3' }).click();
  await table.getByRole('checkbox', { name: 'Select row 2' }).click();
  await page.getByRole('button', { name: 'Replay 2…' }).click();
  const dialog = page.getByRole('alertdialog');
  await dialog.getByLabel(/Reason/).fill('Handler fixed in 1.4.2');
  await dialog.getByRole('button', { name: 'Replay' }).click();
  await expect(page.getByRole('alertdialog')).toHaveCount(0);
  expect(api.writes.at(-1)).toMatchObject({
    path: '/api/v1/platform/billing/streams/replay',
    body: {
      group: 'billing:notifier@sub',
      ids: ['1-0', '2-0'],
      reason: 'Handler fixed in 1.4.2',
    },
  });
});

test('contracts, modules and runtime settings are read-only and accessible', async ({ page }) => {
  const api = new MockApi();
  await signIn(page, api);
  await nav(page).getByRole('link', { name: 'Contracts & modules' }).click();
  await page
    .getByRole('group', { name: 'Source' })
    .getByRole('button', { name: 'billing' })
    .click();
  await expect(page.getByRole('table', { name: 'Remote bindings' })).toContainText('incompatible');
  await expect(page.getByRole('table', { name: 'Modules' })).toContainText('ImportError');
  await expectAccessible(page, 'contracts');
  await nav(page).getByRole('link', { name: 'Runtime settings' }).click();
  await expect(page.getByText('Feature flags: not configured')).toBeVisible();
  await expect(page.getByText('streams')).toBeVisible();
  await expectAccessible(page, 'settings');
  expect(api.writes).toHaveLength(0);
});

test('audit explorer: actor names, prefix filter in the URL and keyset paging', async ({
  page,
}) => {
  const api = new MockApi();
  for (let index = 0; index < 60; index++)
    api.audit.push({
      id: `audit-${100 + index}`,
      occurred_at: new Date(Date.now() - index * 60_000).toISOString(),
      actor_id: index % 2 ? RILEY : null,
      actor_type: index % 2 ? 'user' : 'system',
      action: index % 3 ? 'identity.role.assigned' : 'billing.credits.adjusted',
      resource_type: 'user',
      resource_id: JORDAN,
      request_id: null,
      trace_id: null,
    });
  await signIn(page, api);
  await nav(page).getByRole('link', { name: 'Audit log' }).click();
  const table = page.getByRole('table', { name: 'Audit log' });
  await expect(table).toContainText('Riley Moss');
  await expect(table.getByRole('link', { name: 'Riley Moss' }).first()).toHaveAttribute(
    'href',
    `/users/${RILEY}`,
  );
  await page.getByRole('button', { name: 'Older' }).click();
  await expect(page.getByText('Page 2')).toBeVisible();
  expect(api.queries.some((q) => q.includes('/audit?') && q.includes('cursor=50'))).toBe(true);
  await page.getByRole('searchbox', { name: 'Action starts with' }).fill('billing.');
  await expect(page).toHaveURL(/f\.action_prefix=billing\./);
  await expect(page.getByText('Page 1')).toBeVisible();
  await expect(table).not.toContainText('identity.role.assigned');
  await expectAccessible(page, 'audit');
});

test('organization page shows the operator’s own membership without tenant authority', async ({
  page,
}) => {
  const api = new MockApi();
  await signIn(page, api);
  await nav(page).getByRole('link', { name: 'All organizations' }).click();
  await page.getByRole('link', { name: 'Northwind Labs' }).click();
  await expect(page.getByText(/You are not a member of this organization/)).toBeVisible();
});

test('unknown command is immutable and recoverable after closing its screen', async ({ page }) => {
  const api = new MockApi();
  await signIn(page, api);
  await nav(page).getByRole('link', { name: 'Users' }).click();
  await page.getByRole('link', { name: 'Riley Moss' }).click();
  api.failNextWrite = { status: 502, detail: 'Lost response' };
  await page.getByRole('button', { name: 'Block user' }).click();
  const dialog = page.getByRole('alertdialog');
  await dialog.getByLabel(/Reason/).fill('Original reason');
  await dialog.getByLabel(/to confirm/).fill('riley@northwind.example');
  await dialog.getByRole('button', { name: 'Block user' }).click();
  await expect(dialog.getByLabel(/Reason/)).toBeDisabled();
  await dialog.getByRole('button', { name: 'Close', exact: true }).first().click();
  await nav(page).getByRole('link', { name: 'Users' }).click();
  await page.getByRole('button', { name: 'Retry original operation' }).click();
  await expect(page.getByRole('region', { name: 'Operation recovery' })).toHaveCount(0);
  expect(api.writes).toHaveLength(2);
  expect(api.writes[1]).toEqual(api.writes[0]);
  await expect(page.getByRole('heading', { name: 'Users', exact: true })).toBeVisible();
});

test('tenant scope survives create, detail, edit and cancel and resets the draft on scope changes', async ({
  page,
}) => {
  const resource = {
    ...structuredClone(catalogManifest.resources[0]!),
    scope: { parameter: null, label: 'Organization ID' },
  };
  await page.route('**/admin-manifest.json', (route) =>
    route.fulfill({
      json: {
        version: 1,
        modules: [...builtinManifest.modules, 'catalog'],
        resources: [...builtinManifest.resources, resource],
      },
    }),
  );
  await page.route('**/config.json', (route) =>
    route.fulfill({ json: { modules: ['identity', 'tenancy', 'billing', 'catalog'] } }),
  );
  const api = new MockApi();
  await signIn(page, api);
  const alpha = '00000000-0000-4000-8000-0000000000a1';
  const beta = '00000000-0000-4000-8000-0000000000b2';
  const items = new Map<string, Record<string, unknown>>();
  const scopes: string[] = [];
  await page.route('**/api/v1/catalog**', async (route) => {
    const request = route.request();
    const tenant = request.headers()['x-tenant-id'] ?? '';
    scopes.push(tenant);
    const path = new URL(request.url()).pathname;
    if (request.method() !== 'GET')
      items.set(tenant, { id: 'same-id', ...(request.postDataJSON() as Record<string, unknown>) });
    return route.fulfill({
      json:
        path === '/api/v1/catalog' && request.method() === 'GET'
          ? items.has(tenant)
            ? [items.get(tenant)]
            : []
          : items.get(tenant),
    });
  });
  await nav(page).getByRole('link', { name: 'Catalog' }).click();
  await page.getByLabel('Organization ID').fill(alpha);
  await page.getByRole('button', { name: 'Open' }).click();
  await page.getByRole('link', { name: 'New', exact: true }).click();
  await expect(page).toHaveURL(new RegExp(`tenant=${alpha}`));
  await page.getByLabel('Name', { exact: true }).fill('Tenant A item');
  await page.getByRole('button', { name: 'Create', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Tenant A item', exact: true })).toBeVisible();
  await page.getByRole('link', { name: 'Edit', exact: true }).click();
  await expect(page.getByLabel('Name', { exact: true })).toHaveValue('Tenant A item');
  await page.getByRole('link', { name: 'Cancel', exact: true }).click();
  await expect(page).toHaveURL(new RegExp(`/catalog/same-id\\?tenant=${alpha}`));
  await page.evaluate((url) => {
    history.pushState(null, '', url);
    window.dispatchEvent(new PopStateEvent('popstate'));
  }, `/catalog/new?tenant=${alpha}`);
  await page.getByLabel('Name', { exact: true }).fill('Private draft A');
  await page.evaluate((url) => {
    history.pushState(null, '', url);
    window.dispatchEvent(new PopStateEvent('popstate'));
  }, `/catalog/new?tenant=${beta}`);
  await expect(page.getByLabel('Name', { exact: true })).toHaveValue('');
  await page.getByLabel('Name', { exact: true }).fill('Tenant B item');
  await page.getByRole('button', { name: 'Create', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Tenant B item', exact: true })).toBeVisible();
  await expect(page).toHaveURL(new RegExp(`tenant=${beta}`));
  expect(scopes.every((scope) => [alpha, beta].includes(scope))).toBe(true);
});

test('incident lists follow continuation instead of silently stopping at the first window', async ({
  page,
}) => {
  const api = new MockApi();
  await signIn(page, api);
  const seen: string[] = [];
  await page.route('**/api/v1/platform/*/outbox/dead**', async (route) => {
    const url = new URL(route.request().url());
    const cursor = url.searchParams.get('cursor');
    seen.push(cursor ?? 'first');
    const start = cursor ? Number(cursor) : 0;
    const size = Number(url.searchParams.get('limit'));
    const rows = Array.from({ length: Math.min(size, 203 - start) }, (_, i) => ({
      id: `envelope-${start + i}`,
      channel: 'paging.v1',
      attempts: 12,
      dead_at: '2026-10-28T00:00:00',
      emitted_at: '2026-10-28T00:00:00',
      last_error: null,
    }));
    return route.fulfill({
      json: rows,
      headers: start + size < 203 ? { 'X-Next-Cursor': String(start + size) } : {},
    });
  });
  await nav(page).getByRole('link', { name: 'Event delivery' }).click();
  await expect(page.getByRole('table', { name: 'Outbox dead letters' })).toContainText(
    'envelope-0',
  );
  await page.getByRole('button', { name: 'Next', exact: true }).click();
  await expect(page.getByRole('table', { name: 'Outbox dead letters' })).toContainText(
    'envelope-100',
  );
  await page.getByRole('button', { name: 'Next', exact: true }).click();
  await expect(page.getByRole('table', { name: 'Outbox dead letters' })).toContainText(
    'envelope-202',
  );
  await expect(page.getByRole('button', { name: 'Next', exact: true })).toBeDisabled();
  expect(seen).toContain('100');
  expect(seen).toContain('200');
});

test('role permissions declared for edit forms are actually rendered and usable', async ({
  page,
}) => {
  const api = new MockApi();
  await signIn(page, api);
  const id = '00000000-0000-4000-8000-000000000091';
  const role = {
    id,
    name: 'Catalog editor',
    code: 'catalog_editor',
    description: null,
    is_system: false,
    permissions: [] as { id: string; name: string }[],
  };
  await page.route('**/api/v1/permissions', (route) =>
    route.fulfill({
      json: [
        { id: '00000000-0000-4000-8000-000000000092', name: 'Catalog read', code: 'catalog.read' },
      ],
    }),
  );
  await page.route('**/api/v1/roles**', (route) => {
    const req = route.request();
    const path = new URL(req.url()).pathname;
    if (req.method() === 'POST') {
      role.permissions = [{ id: '00000000-0000-4000-8000-000000000092', name: 'Catalog read' }];
      return route.fulfill({ status: 204 });
    }
    return route.fulfill({ json: path === '/api/v1/roles' ? [role] : role });
  });
  await nav(page).getByRole('link', { name: 'Platform roles' }).click();
  await page.getByRole('link', { name: 'Catalog editor', exact: true }).click();
  await page.getByRole('link', { name: 'Edit', exact: true }).click();
  await expect(
    page.getByRole('heading', { name: 'Edit · Platform roles', exact: true }),
  ).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Permissions', exact: true })).toBeVisible();
  await page.getByLabel('Add', { exact: true }).selectOption({ label: 'Catalog read' });
  await page.getByRole('button', { name: 'Assign', exact: true }).click();
  await page.getByRole('alertdialog').getByRole('button', { name: 'Assign', exact: true }).click();
  await expect(page.getByRole('list', { name: 'Permissions' })).toContainText('Catalog read');
});

test('activity binds both resource type and the public audit identity, including PSP subscription IDs', async ({
  page,
}) => {
  const api = new MockApi();
  await signIn(page, api);
  await nav(page).getByRole('link', { name: 'Users' }).click();
  await page.getByRole('link', { name: 'Riley Moss' }).click();
  await expect
    .poll(() =>
      api.queries.some(
        (q) => q.includes('resource_type=user') && q.includes(`resource_id=${RILEY}`),
      ),
    )
    .toBe(true);
  await page.route('**/api/v1/platform/billing/subscriptions/1', (route) =>
    route.fulfill({
      json: {
        id: 1,
        customer_id: RILEY,
        stripe_subscription_id: 'sub_audit_binding',
        status: 'active',
        price_id: 1,
        quantity: 1,
        cancel_at_period_end: false,
      },
    }),
  );
  await page.evaluate(() => {
    history.pushState(null, '', '/billing_subscriptions/1');
    window.dispatchEvent(new PopStateEvent('popstate'));
  });
  await expect
    .poll(() =>
      api.queries.some(
        (q) =>
          q.includes('resource_type=billing_subscription') &&
          q.includes('resource_id=sub_audit_binding'),
      ),
    )
    .toBe(true);
  await expect(page.getByRole('link', { name: /audit explorer/ }).first()).toHaveAttribute(
    'href',
    /f.resource_type=billing_subscription/,
  );
});
