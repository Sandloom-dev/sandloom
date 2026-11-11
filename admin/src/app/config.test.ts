import { afterEach, expect, it, vi } from 'vitest';
import { loadConfig } from './config';

afterEach(() => vi.unstubAllGlobals());

it('loads a declared OIDC issuer without a module named identity', async () => {
  vi.stubGlobal(
    'fetch',
    vi.fn((path: string) =>
      Promise.resolve(
        new Response(
          JSON.stringify(
            path.endsWith('config.json')
              ? {
                  modules: ['acme'],
                  issuer: 'acme',
                  login_entry: '/api/v1/acme/login',
                  console_auth: { kind: 'id_token', roles_owner: 'grants' },
                }
              : { version: 1, modules: ['acme'], resources: [] },
          ),
        ),
      ),
    ),
  );
  const config = await loadConfig();
  expect(config.auth).toEqual({
    kind: 'id_token',
    login: '/api/v1/acme/login',
    roles_owner: 'grants',
  });
  expect(config.modules.has('identity')).toBe(false);
});
