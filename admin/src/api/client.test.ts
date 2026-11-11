import { describe, expect, it, vi } from 'vitest';
import { ApiClient } from './client';
import { identityAuth } from '../../tests/auth';
function identityClient(fetcher: typeof fetch) {
  const client = new ApiClient(fetcher);
  client.configureAuth(identityAuth);
  return client;
}

function token(sub: string): string {
  const body = btoa(JSON.stringify({ sub, roles: ['admin'], claims_sources: ['identity'] }))
    .replace(/\+/g, '-')
    .replace(/\//g, '_')
    .replace(/=+$/, '');
  return `x.${body}.y`;
}

function server(): typeof fetch {
  return vi.fn((path: string) => {
    const body =
      path === '/auth/login'
        ? { access_token: token('u1'), refresh_token: 'r1', expires_in: 900 }
        : path === '/auth/me'
          ? { id: 'u1', email: 'owner@example.com' }
          : null;
    return Promise.resolve(new Response(JSON.stringify(body), { status: 200 }));
  }) as unknown as typeof fetch;
}

const KEY = 'sandloom-admin-session';

describe('ApiClient session storage', () => {
  it('keeps the session in memory only by default (production)', async () => {
    sessionStorage.clear();
    const client = identityClient(server());
    await client.login({ email: 'owner@example.com', password: 'x' });
    expect(client.snapshot?.subject).toBe('u1');
    expect(sessionStorage.length).toBe(0);
    expect(localStorage.getItem(KEY)).toBeNull();
    const reloaded = identityClient(server());
    reloaded.keepAcrossReloads(false);
    expect(reloaded.snapshot).toBeUndefined();
  });

  it('outside production a reload of the same tab keeps the session (sessionStorage only)', async () => {
    sessionStorage.clear();
    const client = identityClient(server());
    client.keepAcrossReloads(true);
    await client.login({ email: 'owner@example.com', password: 'x' });
    expect(sessionStorage.getItem(KEY)).toContain('r1');
    expect(localStorage.getItem(KEY)).toBeNull();
    // A reload: a new client in the same tab.
    const reloaded = identityClient(server());
    reloaded.keepAcrossReloads(true);
    expect(reloaded.snapshot?.subject).toBe('u1');
    await reloaded.logout();
    expect(sessionStorage.getItem(KEY)).toBeNull();
  });

  it('ignores a damaged stored session and production forgets a stored one', () => {
    sessionStorage.setItem(KEY, '{"subject": 1}');
    const client = identityClient(server());
    client.keepAcrossReloads(true);
    expect(client.snapshot).toBeUndefined();
    expect(sessionStorage.getItem(KEY)).toBeNull();
    sessionStorage.setItem(KEY, 'left over');
    identityClient(server()).keepAcrossReloads(false);
    expect(sessionStorage.getItem(KEY)).toBeNull();
  });

  it('a browser that blocks storage still gets a working console (memory only)', async () => {
    const blocked = vi.spyOn(window, 'sessionStorage', 'get').mockImplementation(() => {
      throw new DOMException('blocked', 'SecurityError');
    });
    try {
      for (const keep of [false, true]) {
        const client = identityClient(server());
        expect(() => {
          client.keepAcrossReloads(keep);
        }).not.toThrow();
        expect(client.keepsAcrossReloads).toBe(false);
        await client.login({ email: 'owner@example.com', password: 'x' });
        expect(client.snapshot?.subject).toBe('u1');
      }
    } finally {
      blocked.mockRestore();
    }
  });

  it('a storage write that fails keeps the session in memory', async () => {
    sessionStorage.clear();
    const client = identityClient(server());
    client.keepAcrossReloads(true);
    const full = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new DOMException('full', 'QuotaExceededError');
    });
    try {
      await client.login({ email: 'owner@example.com', password: 'x' });
      expect(client.snapshot?.subject).toBe('u1');
    } finally {
      full.mockRestore();
    }
  });
});

describe('declared issuer sign-in', () => {
  it('uses the issuer paths and roles owner without identity', async () => {
    const access = `x.${btoa(JSON.stringify({ sub: 'owner', roles: ['admin'], claims_sources: ['acme'], exp: Math.floor(Date.now() / 1000) + 300 }))}.y`;
    const fetcher = vi
      .fn()
      .mockResolvedValue(new Response(JSON.stringify({ access_token: access })));
    const client = new ApiClient(fetcher);
    client.configureAuth({ kind: 'id_token', login: '/api/v1/acme/login', roles_owner: 'acme' });
    await client.login({ id_token: 'external-token' }, 'admin');
    expect(fetcher).toHaveBeenCalledTimes(1);
    expect(fetcher.mock.calls[0]?.[0]).toBe('/api/v1/acme/login');
    expect(client.snapshot?.roles).toEqual(['admin']);
    await client.logout();
    expect(fetcher).toHaveBeenCalledTimes(1);
    expect(client.snapshot).toBeUndefined();
  });
});

it('uses a short-lived access-only token until its actual expiry', async () => {
  const access = `x.${btoa(JSON.stringify({ sub: 'owner', roles: ['admin'], claims_sources: ['acme'], exp: Math.floor(Date.now() / 1000) + 10 }))}.y`;
  const fetcher = vi
    .fn()
    .mockImplementation((path: string) =>
      Promise.resolve(
        new Response(JSON.stringify(path === '/login' ? { access_token: access } : [])),
      ),
    );
  const client = new ApiClient(fetcher);
  client.configureAuth({ kind: 'id_token', login: '/login', roles_owner: 'acme' });
  await client.login({ id_token: 'external' }, 'admin');
  await client.request('/api/v1/custom/items/a%2Fb/?limit=2');
  expect(fetcher).toHaveBeenCalledTimes(2);
  await expect(client.request('/api/v1/../../_sandloom')).rejects.toThrow(
    'Unsupported public API path',
  );
});
