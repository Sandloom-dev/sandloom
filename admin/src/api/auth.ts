/** Bootstrap data, generated from the composition's issuer declaration. */
export interface ConsoleAuth {
  kind: 'password' | 'id_token';
  login: string;
  refresh?: string;
  logout?: string;
  me?: string;
  setup_state?: string;
  roles_owner: string;
}

export function routePath(value: unknown): value is string {
  return (
    typeof value === 'string' &&
    /^\/[A-Za-z0-9_~-]+(?:[./][A-Za-z0-9_~-]+)*$/.test(value) &&
    !value.split('/').some((part) => part === '.' || part === '..')
  );
}

export function consoleAuth(value: unknown): ConsoleAuth {
  if (!value || typeof value !== 'object') throw new Error('Invalid console sign-in configuration');
  const config = value as Record<string, unknown>;
  if (
    !['password', 'id_token'].includes(String(config.kind)) ||
    !routePath(config.login) ||
    typeof config.roles_owner !== 'string' ||
    !config.roles_owner ||
    ['refresh', 'logout', 'me', 'setup_state'].some(
      (name) => config[name] !== undefined && !routePath(config[name]),
    )
  )
    throw new Error('Invalid console sign-in configuration');
  return config as unknown as ConsoleAuth;
}
