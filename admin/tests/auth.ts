import { ApiClient } from '../src/api/client';
export const identityAuth = {
  kind: 'password' as const,
  login: '/auth/login',
  refresh: '/auth/refresh',
  logout: '/auth/logout',
  me: '/auth/me',
  setup_state: '/api/v1/identity/setup-state',
  roles_owner: 'identity',
};
export const identityConfig = {
  issuer: 'identity',
  login_entry: identityAuth.login,
  console_auth: identityAuth,
};

export function identityClient(fetcher: typeof fetch) {
  const client = new ApiClient(fetcher);
  client.configureAuth(identityAuth);
  return client;
}
