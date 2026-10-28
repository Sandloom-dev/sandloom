import { test, expect } from '@playwright/test';

/** Real backend, real authorization/transactions. No route mocks or production fixtures. */
test('owner operates accounts, grants, billing and audit against the real backend', async ({
  page,
  request,
}) => {
  const required = (key: string) => {
    const value = process.env[key];
    if (!value) throw new Error(`Missing isolated acceptance setting: ${key}`);
    return value;
  };
  const email = required('SANDLOOM_ACCEPTANCE_EMAIL');
  const password = required('SANDLOOM_ACCEPTANCE_PASSWORD');
  const target = required('SANDLOOM_ACCEPTANCE_TARGET');
  const customer = required('SANDLOOM_ACCEPTANCE_CUSTOMER');
  const product = required('SANDLOOM_ACCEPTANCE_PRODUCT');
  const backend = required('SANDLOOM_ADMIN_API_TARGET');
  const nav = page.getByRole('navigation', { name: 'Primary' });
  const consoleErrors: string[] = [];
  page.on('pageerror', (error) => consoleErrors.push(error.message));
  await page.goto('/');
  await page.getByLabel('Email', { exact: true }).fill(email);
  await page.getByLabel('Password', { exact: true }).fill(password);
  const login = page.waitForResponse(
    (response) => new URL(response.url()).pathname === '/auth/login',
  );
  await page.getByRole('button', { name: 'Sign in', exact: true }).click();
  const session = (await (await login).json()) as { access_token: string };
  const headers = { Authorization: `Bearer ${session.access_token}` };
  await expect(page.getByRole('heading', { name: 'Overview', exact: true })).toBeVisible();

  // A tenant account cannot use owner APIs even outside the UI.
  const memberLogin = await request.post(`${backend}/auth/login`, {
    data: { email: 'member@acceptance.example.com', password },
  });
  expect(memberLogin.status()).toBe(200);
  const member = (await memberLogin.json()) as { access_token: string };
  expect(
    (
      await request.get(`${backend}/api/v1/users`, {
        headers: { Authorization: `Bearer ${member.access_token}` },
      })
    ).status(),
  ).toBe(403);

  const targetSession = await request.post(`${backend}/auth/login`, {
    data: { email: 'target@acceptance.example.com', password },
  });
  expect(targetSession.status()).toBe(200);
  const targetToken = ((await targetSession.json()) as { access_token: string }).access_token;
  await nav.getByRole('link', { name: 'Users', exact: true }).click();
  await page.getByRole('link', { name: 'Acceptance target', exact: true }).click();
  await page.getByRole('button', { name: 'Block user', exact: true }).click();
  let dialog = page.getByRole('alertdialog');
  await dialog.getByLabel(/Reason/).fill('Isolated live acceptance');
  await dialog.getByLabel(/to confirm/).fill('target@acceptance.example.com');
  await dialog.getByRole('button', { name: 'Block user', exact: true }).click();
  await expect(page.getByRole('status').filter({ hasText: 'This user is blocked' })).toBeVisible();
  await expect(page.getByText('auth.user.blocked')).toBeVisible();
  expect(
    (
      await request.get(`${backend}/auth/me`, {
        headers: { Authorization: `Bearer ${targetToken}` },
      })
    ).status(),
  ).toBe(401);
  await page.getByRole('button', { name: 'Unblock user', exact: true }).click();
  dialog = page.getByRole('alertdialog');
  await dialog.getByLabel(/Reason/).fill('Isolated acceptance resolved');
  await dialog.getByRole('button', { name: 'Unblock user', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Block user', exact: true })).toBeVisible();

  await nav.getByRole('link', { name: 'Platform roles', exact: true }).click();
  await page.getByRole('link', { name: 'Acceptance editor', exact: true }).click();
  await page.getByRole('link', { name: 'Edit', exact: true }).click();
  await expect(
    page.getByRole('heading', { name: 'Edit · Platform roles', exact: true }),
  ).toBeVisible();
  await page.getByLabel('Add', { exact: true }).selectOption({ label: 'Acceptance read' });
  await page.getByRole('button', { name: 'Assign', exact: true }).click();
  await page.getByRole('alertdialog').getByRole('button', { name: 'Assign', exact: true }).click();
  await expect(page.getByRole('list', { name: 'Permissions' })).toContainText('Acceptance read');

  await page.evaluate((url) => {
    history.pushState(null, '', url);
    window.dispatchEvent(new PopStateEvent('popstate'));
  }, `/billing_customers/${customer}`);
  await page.getByRole('button', { name: 'Adjust credits', exact: true }).click();
  dialog = page.getByRole('alertdialog');
  await dialog.getByLabel('Direction', { exact: true }).selectOption('grant');
  await dialog.getByLabel('Credits', { exact: true }).fill('7');
  await dialog.getByLabel(/Reason/).fill('Isolated acceptance grant');
  const outgoing = page.waitForRequest(
    (r) => r.method() === 'POST' && r.url().includes('/adjust-credits'),
  );
  await dialog.getByRole('button', { name: 'Adjust credits', exact: true }).click();
  await expect(dialog).toHaveCount(0);
  const original = await outgoing;
  const replay = await request.post(original.url(), {
    headers: { ...headers, 'Idempotency-Key': original.headers()['idempotency-key'] ?? '' },
    data: original.postDataJSON() as object,
  });
  expect(replay.status()).toBe(200);
  expect(replay.headers()['idempotent-replay']).toBe('true');
  const balance = await request.get(
    `${backend}/api/v1/platform/billing/balances?customer_id=${customer}`,
    { headers },
  );
  expect(balance.status()).toBe(200);
  expect(await balance.json()).toEqual(
    expect.arrayContaining([expect.objectContaining({ amount: 7 })]),
  );

  await page.evaluate((url) => {
    history.pushState(null, '', url);
    window.dispatchEvent(new PopStateEvent('popstate'));
  }, `/billing_products/${product}`);
  await page.getByRole('link', { name: 'Edit', exact: true }).click();
  await page.getByLabel('Name', { exact: true }).fill('Acceptance product updated');
  await page.getByLabel(/Reason/).fill('Isolated catalog edit');
  await page.getByRole('button', { name: 'Save changes', exact: true }).click();
  await expect(
    page.getByRole('heading', { name: 'Acceptance product updated', exact: true }),
  ).toBeVisible();
  await expect(page.getByText('billing.platform.product_updated')).toBeVisible();
  const audits = await request.get(
    `${backend}/api/v1/platform/billing/audit?resource_type=billing_customer&resource_id=${customer}`,
    { headers },
  );
  const journal = (await audits.json()) as { action: string }[];
  expect(journal.filter((row) => row.action === 'billing.platform.credit_adjusted')).toHaveLength(
    1,
  );
  expect((await request.get(`${backend}/api/v1/users/${target}`, { headers })).status()).toBe(200);
  expect(consoleErrors).toEqual([]);
});
