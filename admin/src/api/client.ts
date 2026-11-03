export class HttpError extends Error {
  constructor(
    message: string,
    public status: number,
    public body?: unknown,
    public headers?: Headers,
  ) {
    super(message);
    this.name = 'HttpError';
  }
}

/**
 * Identity's answer to a correct password on an account with two-factor
 * authentication when no code was sent: `401` with `X-2FA-Required: true`.
 * It comes only after the password verified and is not a failed login.
 */
export function needsSecondFactor(error: unknown): boolean {
  return (
    error instanceof HttpError &&
    error.status === 401 &&
    error.headers?.get('X-2FA-Required') === 'true'
  );
}
import type { components } from './generated';

export type Schema<K extends keyof components['schemas']> = components['schemas'][K];
export type User = Schema<'UserOut'>;
export type Role = Schema<'RoleOut'>;
export type Organization = Schema<'OrganizationOut'>;
export type Member = Schema<'MemberOut'>;
type TokenPair = Schema<'TokenPair'>;
export interface Session {
  subject: string;
  roles: string[];
  expiresAt: number;
}
type StoredSession = Session & { access: string; refresh: string };

function claims(token: string): { sub: string; roles: string[] } {
  try {
    const raw = (token.split('.')[1] ?? '').replace(/-/g, '+').replace(/_/g, '/');
    const value: unknown = JSON.parse(
      new TextDecoder().decode(Uint8Array.from(atob(raw), (c) => c.charCodeAt(0))),
    );
    if (!value || typeof value !== 'object') throw new Error('claims');
    const record = value as Record<string, unknown>;
    if (typeof record.sub !== 'string') throw new Error('subject');
    const list = (key: string): unknown[] => {
      const item = record[key];
      return Array.isArray(item) ? (item as unknown[]) : [];
    };
    // Only trust roles that identity itself contributed to this token.
    const trusted =
      list('claims_sources').includes('identity') &&
      !list('claims_sources_failed').includes('identity');
    return {
      sub: record.sub,
      roles: trusted ? list('roles').filter((r): r is string => typeof r === 'string') : [],
    };
  } catch {
    throw new HttpError('The server returned an invalid session', 401);
  }
}

async function readResponse(response: Response): Promise<unknown> {
  const body: unknown =
    response.status === 204
      ? null
      : await response.json().catch(() => {
          if (response.ok)
            throw new HttpError(
              'The server returned an invalid response; inspect the operation before retrying',
              502,
            );
          return null;
        });
  if (!response.ok) {
    const detail = body && typeof body === 'object' && 'detail' in body ? body.detail : null;
    const message = typeof detail === 'string' ? detail : `Request failed (${response.status})`;
    throw new HttpError(message, response.status, body, response.headers);
  }
  return body;
}

/** Where a development console keeps its session across a reload of the tab. */
const SESSION_KEY = 'sandloom-admin-session';

function storedSession(raw: string | null): StoredSession | undefined {
  if (!raw) return undefined;
  try {
    const value: unknown = JSON.parse(raw);
    if (!value || typeof value !== 'object') return undefined;
    const r = value as Record<string, unknown>;
    if (
      typeof r.subject !== 'string' ||
      !Array.isArray(r.roles) ||
      !r.roles.every((role) => typeof role === 'string') ||
      typeof r.access !== 'string' ||
      typeof r.refresh !== 'string' ||
      typeof r.expiresAt !== 'number'
    )
      return undefined;
    return {
      subject: r.subject,
      roles: r.roles,
      access: r.access,
      refresh: r.refresh,
      expiresAt: r.expiresAt,
    };
  } catch {
    return undefined;
  }
}

/** This tab's sessionStorage, or undefined where the browser blocks it. */
function sessionStorageOrNone(): Storage | undefined {
  try {
    return window.sessionStorage;
  } catch {
    return undefined;
  }
}

/** A storage write that may fail (quota, policy); the session stays in memory. */
function quietly(write: () => void) {
  try {
    write();
  } catch {
    // Persistence is a convenience; the in-memory session is the source.
  }
}

/**
 * Credentials in memory; one refresh per session, no automatic replay of
 * mutations. Outside production (`keepAcrossReloads(true)`) the session is
 * also kept in this tab's sessionStorage, so a reload keeps it; closing the
 * tab ends it. Never localStorage.
 */
export class ApiClient {
  private session?: StoredSession;
  private storage?: Storage;
  private generation = 0;
  private refreshing?: Promise<void>;
  private requests = new Set<AbortController>();
  private listeners = new Set<() => void>();
  constructor(private readonly fetcher: typeof fetch = (...args) => fetch(...args)) {}

  get snapshot(): Session | undefined {
    if (!this.session) return undefined;
    const { subject, roles, expiresAt } = this.session;
    return { subject, roles: [...roles], expiresAt };
  }
  get epoch() {
    return this.generation;
  }
  subscribe = (listener: () => void) => {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  };
  private changed() {
    for (const listener of this.listeners) listener();
  }
  /** True outside production: a reload of this tab keeps the session. */
  get keepsAcrossReloads(): boolean {
    return this.storage !== undefined;
  }
  /**
   * Outside production, keep the session in this tab's sessionStorage and
   * take back the one a reload left there; in production, drop any stored one.
   */
  keepAcrossReloads(keep: boolean) {
    // Storage is optional: a browser policy that blocks it (SecurityError)
    // leaves the session in memory, as in production.
    this.storage = undefined;
    const storage = sessionStorageOrNone();
    if (!storage) return;
    if (!keep) {
      quietly(() => {
        storage.removeItem(SESSION_KEY);
      });
      return;
    }
    let restored: StoredSession | undefined;
    try {
      restored = storedSession(storage.getItem(SESSION_KEY));
    } catch {
      return;
    }
    this.storage = storage;
    if (!restored) {
      quietly(() => {
        storage.removeItem(SESSION_KEY);
      });
      return;
    }
    this.generation++;
    this.session = restored;
    this.changed();
  }
  private persist() {
    const storage = this.storage;
    if (!storage) return;
    const session = this.session;
    quietly(() => {
      if (session) storage.setItem(SESSION_KEY, JSON.stringify(session));
      else storage.removeItem(SESSION_KEY);
    });
  }
  clear() {
    this.generation++;
    this.session = undefined;
    this.persist();
    this.refreshing = undefined;
    for (const request of this.requests) request.abort();
    this.requests.clear();
    this.changed();
  }
  private install(received: TokenPair) {
    // Runtime check of a network payload; the static type is only a claim.
    const pair = received as Partial<TokenPair> | null;
    if (
      !pair ||
      typeof pair.access_token !== 'string' ||
      typeof pair.refresh_token !== 'string' ||
      typeof pair.expires_in !== 'number' ||
      !Number.isFinite(pair.expires_in) ||
      pair.expires_in <= 0
    ) {
      throw new HttpError('The server returned an invalid session', 401);
    }
    const parsed = claims(pair.access_token);
    this.session = {
      subject: parsed.sub,
      roles: parsed.roles,
      access: pair.access_token,
      refresh: pair.refresh_token,
      expiresAt: Date.now() + pair.expires_in * 1000,
    };
    this.persist();
  }
  async login(data: Schema<'LoginIn'>, requiredRole?: string) {
    if (this.session) this.clear();
    else {
      this.generation++;
      for (const request of this.requests) request.abort();
      this.requests.clear();
    }
    const epoch = this.generation;
    const pair = await this.request<TokenPair>(
      '/auth/login',
      { method: 'POST', body: JSON.stringify(data) },
      false,
    );
    if (epoch !== this.generation) throw new HttpError('Sign-in cancelled', 401);
    if (requiredRole && !claims(pair.access_token).roles.includes(requiredRole)) {
      await this.request(
        '/auth/logout',
        { method: 'POST', body: JSON.stringify({ refresh_token: pair.refresh_token }) },
        false,
      ).catch(() => undefined);
      throw new HttpError('Platform administrator access is required', 403);
    }
    this.install(pair);
    try {
      await this.me();
    } catch (error) {
      this.clear();
      throw error;
    }
    this.changed();
  }
  async logout() {
    const refresh = this.session?.refresh;
    this.clear();
    if (refresh) {
      // Local access is removed even when the server is unreachable. No retries.
      await this.request(
        '/auth/logout',
        { method: 'POST', body: JSON.stringify({ refresh_token: refresh }) },
        false,
      );
    }
  }
  private async ensure() {
    if (!this.session) throw new HttpError('Sign in to continue', 401);
    if (this.session.expiresAt - Date.now() > 15_000) return;
    if (!this.refreshing) {
      const epoch = this.generation;
      const refresh = this.session.refresh;
      this.refreshing = (async () => {
        try {
          const pair = await this.request<TokenPair>(
            '/auth/refresh',
            { method: 'POST', body: JSON.stringify({ refresh_token: refresh }) },
            false,
          );
          if (epoch !== this.generation) throw new HttpError('Session ended', 401);
          const subject = this.session?.subject;
          const roles = JSON.stringify(this.session?.roles);
          this.install(pair);
          if (subject !== this.session?.subject)
            throw new HttpError('Session subject changed', 401);
          if (roles !== JSON.stringify(this.session?.roles)) {
            this.generation++;
            this.refreshing = undefined;
            for (const request of this.requests) request.abort();
            this.requests.clear();
            this.changed();
          }
        } catch (error) {
          if (epoch === this.generation) this.clear();
          throw error;
        } finally {
          if (epoch === this.generation) this.refreshing = undefined;
        }
      })();
    }
    await this.refreshing;
  }
  /** T is the caller's declared response shape (typed helpers derive it from OpenAPI). */
  async request<T = unknown>(
    path: string,
    options: RequestInit = {},
    authenticated = true,
  ): Promise<T> {
    return (await this.response<T>(path, options, authenticated)).data;
  }
  // eslint-disable-next-line @typescript-eslint/no-unnecessary-type-parameters
  async response<T>(
    path: string,
    options: RequestInit = {},
    authenticated = true,
  ): Promise<{ data: T; headers: Headers }> {
    if (!path.startsWith('/auth/') && !path.startsWith('/api/v1/'))
      throw new Error('Unsupported public API path');
    if (authenticated) await this.ensure();
    const epoch = this.generation;
    const controller = new AbortController();
    const abort = () => {
      controller.abort();
    };
    options.signal?.addEventListener('abort', abort, { once: true });
    if (options.signal?.aborted) controller.abort();
    this.requests.add(controller);
    try {
      const headers = new Headers(options.headers);
      headers.set('Accept', 'application/json');
      if (options.body) headers.set('Content-Type', 'application/json');
      if (authenticated && this.session)
        headers.set('Authorization', `Bearer ${this.session.access}`);
      const response = await this.fetcher(path, {
        ...options,
        headers,
        signal: controller.signal,
        credentials: 'omit',
        cache: 'no-store',
        redirect: 'error',
      });
      const data = await readResponse(response);
      if (epoch !== this.generation) throw new HttpError('Session context changed', 401);
      return { data: data as T, headers: response.headers };
    } catch (error) {
      if (
        authenticated &&
        error instanceof HttpError &&
        error.status === 401 &&
        epoch === this.generation
      )
        this.clear();
      throw error;
    } finally {
      this.requests.delete(controller);
      options.signal?.removeEventListener('abort', abort);
    }
  }
  me() {
    return this.request<User>('/auth/me');
  }
}

export const api = new ApiClient();
