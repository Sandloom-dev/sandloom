import { afterEach, describe, expect, it, vi } from 'vitest';
import { ApiClient } from '../src/api/client';

const token = (sub = 'operator') =>
  `test.${btoa(JSON.stringify({ sub, roles: ['admin'], claims_sources: ['identity'] }))}.test`;
const pair = (expires = 3600) => ({
  access_token: token(),
  refresh_token: 'private-refresh',
  expires_in: expires,
});
const json = (data: unknown, status = 200) =>
  new Response(JSON.stringify(data), { status, headers: { 'Content-Type': 'application/json' } });
afterEach(() => vi.useRealTimers());

describe('session boundary', () => {
  it('shares one rotating refresh across concurrent requests', async () => {
    let refreshes = 0;
    const fetcher = vi.fn(async (path: RequestInfo | URL) => {
      if (path === '/auth/login') return json(pair(20));
      if (path === '/auth/refresh') {
        refreshes++;
        await Promise.resolve();
        return json(pair());
      }
      return json({ id: 'operator' });
    });
    const client = new ApiClient(fetcher);
    await client.login({ email: 'admin@example.test', password: 'private' });
    vi.useFakeTimers();
    vi.setSystemTime(Date.now() + 10_000);
    await Promise.all([client.me(), client.me(), client.me()]);
    expect(refreshes).toBe(1);
    expect(client.snapshot?.subject).toBe('operator');
  });
  it('never replays a write on a 502 or turns it into success', async () => {
    const fetcher = vi.fn(async (path: RequestInfo | URL) =>
      path === '/auth/login'
        ? json(pair())
        : path === '/auth/me'
          ? json({ id: 'operator' })
          : json({ detail: 'Uncertain result' }, 502),
    );
    const client = new ApiClient(fetcher);
    await client.login({ email: 'admin@example.test', password: 'private' });
    await expect(
      client.request('/api/v1/roles', { method: 'POST', body: '{}' }),
    ).rejects.toMatchObject({ status: 502 });
    expect(fetcher.mock.calls.filter(([path]) => path === '/api/v1/roles')).toHaveLength(1);
  });
  it('discards a response from the previous user even if the transport ignores abort', async () => {
    let finish!: (value: Response) => void;
    const client = new ApiClient(async (path) =>
      path === '/auth/login'
        ? json(pair())
        : path === '/auth/me'
          ? json({ id: 'operator' })
          : new Promise((resolve) => {
              finish = resolve;
            }),
    );
    await client.login({ email: 'admin@example.test', password: 'private' });
    const request = client.request('/api/v1/users');
    await Promise.resolve();
    client.clear();
    finish(json([{ id: 'private-user' }]));
    await expect(request).rejects.toMatchObject({ status: 401 });
    expect(client.snapshot).toBeUndefined();
  });
  it('clears all local authority on a revoked token without refreshing it', async () => {
    let revoked = false;
    const fetcher = vi.fn(async (path: RequestInfo | URL) =>
      path === '/auth/login'
        ? json(pair())
        : revoked
          ? json({ detail: 'Token has been revoked' }, 401)
          : json({ id: 'operator' }),
    );
    const client = new ApiClient(fetcher);
    await client.login({ email: 'admin@example.test', password: 'private' });
    revoked = true;
    await expect(client.me()).rejects.toMatchObject({ status: 401 });
    expect(client.snapshot).toBeUndefined();
    expect(fetcher.mock.calls.some(([path]) => path === '/auth/refresh')).toBe(false);
  });
});

describe('authority changes', () => {
  it('does not resurrect a session when refresh completes after logout', async () => {
    let finish!: (response: Response) => void;
    const client = new ApiClient(async (path) =>
      path === '/auth/login'
        ? json(pair(20))
        : path === '/auth/refresh'
          ? new Promise((resolve) => {
              finish = resolve;
            })
          : json({ id: 'operator' }),
    );
    await client.login({ email: 'admin@example.test', password: 'private' });
    vi.useFakeTimers();
    vi.setSystemTime(Date.now() + 10_000);
    const request = client.me();
    await Promise.resolve();
    client.clear();
    finish(json(pair()));
    await expect(request).rejects.toMatchObject({ status: 401 });
    expect(client.snapshot).toBeUndefined();
  });
  it('invalidates the UI context when refresh returns fewer permissions', async () => {
    const client = new ApiClient(async (path) =>
      path === '/auth/login'
        ? json(pair(20))
        : path === '/auth/refresh'
          ? json({
              ...pair(),
              access_token: `x.${btoa(JSON.stringify({ sub: 'operator', roles: [], claims_sources: ['identity'] }))}.x`,
            })
          : json({ id: 'operator' }),
    );
    await client.login({ email: 'admin@example.test', password: 'private' });
    const originalContext = client.epoch;
    vi.useFakeTimers();
    vi.setSystemTime(Date.now() + 10_000);
    await client.me();
    expect(client.snapshot?.roles).toEqual([]);
    expect(client.epoch).not.toBe(originalContext);
  });
});

it('classifies a malformed successful mutation response as unknown, rather than acknowledging it', async () => {
  const api = new ApiClient(async (path) =>
    path === '/auth/login'
      ? json(pair())
      : path === '/auth/me'
        ? json({ id: 'operator' })
        : new Response('<html>broken upstream</html>', { status: 200 }),
  );
  await api.login({ email: 'operator@example.com', password: 'synthetic' });
  await expect(api.request('/api/v1/change', { method: 'POST' })).rejects.toMatchObject({
    status: 502,
  });
});
