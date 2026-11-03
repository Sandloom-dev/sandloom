import { describe, expect, it, vi } from 'vitest';
import { ApiClient } from './client';

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
    const client = new ApiClient(server());
    await client.login({ email: 'owner@example.com', password: 'x' });
    expect(client.snapshot?.subject).toBe('u1');
    expect(sessionStorage.length).toBe(0);
    expect(localStorage.getItem(KEY)).toBeNull();
    const reloaded = new ApiClient(server());
    reloaded.keepAcrossReloads(false);
    expect(reloaded.snapshot).toBeUndefined();
  });

  it('outside production a reload of the same tab keeps the session (sessionStorage only)', async () => {
    sessionStorage.clear();
    const client = new ApiClient(server());
    client.keepAcrossReloads(true);
    await client.login({ email: 'owner@example.com', password: 'x' });
    expect(sessionStorage.getItem(KEY)).toContain('r1');
    expect(localStorage.getItem(KEY)).toBeNull();
    // A reload: a new client in the same tab.
    const reloaded = new ApiClient(server());
    reloaded.keepAcrossReloads(true);
    expect(reloaded.snapshot?.subject).toBe('u1');
    await reloaded.logout();
    expect(sessionStorage.getItem(KEY)).toBeNull();
  });

  it('ignores a damaged stored session and production forgets a stored one', () => {
    sessionStorage.setItem(KEY, '{"subject": 1}');
    const client = new ApiClient(server());
    client.keepAcrossReloads(true);
    expect(client.snapshot).toBeUndefined();
    expect(sessionStorage.getItem(KEY)).toBeNull();
    sessionStorage.setItem(KEY, 'left over');
    new ApiClient(server()).keepAcrossReloads(false);
    expect(sessionStorage.getItem(KEY)).toBeNull();
  });

  it('a browser that blocks storage still gets a working console (memory only)', async () => {
    const blocked = vi.spyOn(window, 'sessionStorage', 'get').mockImplementation(() => {
      throw new DOMException('blocked', 'SecurityError');
    });
    try {
      for (const keep of [false, true]) {
        const client = new ApiClient(server());
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
    const client = new ApiClient(server());
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
