/**
 * Synthetic backend for browser tests. It speaks the public API shapes only
 * and records every write; it proves UI behavior, never server authorization.
 */
import type { Page, Route } from '@playwright/test';

export const OPERATOR = '00000000-0000-4000-8000-0000000000aa';
export const RILEY = '00000000-0000-4000-8000-000000000001';
export const JORDAN = '00000000-0000-4000-8000-000000000002';

export interface Write {
  method: string;
  path: string;
  body: unknown;
  idempotencyKey: string | null;
}

const user = (id: string, full_name: string, email: string) => ({
  id,
  full_name,
  email,
  username: null,
  first_name: null,
  last_name: null,
  is_active: true,
  is_verified: true,
  is_superuser: false,
  created_at: '2026-10-01T09:00:00Z',
  updated_at: '2026-10-01T09:00:00Z',
  last_login_at: null,
  email_verified_at: null,
});

export function token(roles: string[]): string {
  const claims = { sub: OPERATOR, roles, claims_sources: ['identity'] };
  return `x.${Buffer.from(JSON.stringify(claims)).toString('base64url')}.x`;
}

export class MockApi {
  users = [
    user(OPERATOR, 'Morgan Ortiz', 'morgan@acme.example'),
    user(RILEY, 'Riley Moss', 'riley@northwind.example'),
    user(JORDAN, 'Jordan Blake', 'jordan@northwind.example'),
  ];
  blocked = new Set<string>();
  audit: {
    id: string;
    occurred_at: string;
    actor_id: string | null;
    actor_type: string;
    action: string;
    resource_type: string;
    resource_id: string;
    request_id: string | null;
    trace_id: string | null;
  }[] = [];
  writes: Write[] = [];
  queries: string[] = [];
  /** Status to answer the NEXT write with (e.g. 502 for a lost response). */
  failNextWrite: { status: number; detail: string } | null = null;
  revoked = false;
  deadLetters = [
    {
      id: '3b1e0000-0000-4000-8000-00000000c9d0',
      channel: 'billing.credits.spent.v1',
      attempts: 12,
      last_error: 'BusPublishError: broker timeout',
      dead_at: '2026-10-21T08:41:00Z',
      emitted_at: '2026-10-21T08:12:00Z',
    },
  ];
  streamDlq = [
    {
      id: '1-0',
      group: 'billing:notifier@sub',
      event_id: 'e-1',
      attempts: 5,
      reason: 'max_deliveries',
      replayable: true,
    },
    {
      id: '2-0',
      group: 'billing:notifier@sub',
      event_id: 'e-2',
      attempts: 5,
      reason: 'max_deliveries',
      replayable: true,
    },
    {
      id: '3-0',
      group: 'billing:other@sub',
      event_id: 'e-3',
      attempts: 5,
      reason: 'max_deliveries',
      replayable: true,
    },
  ];
  admin = true;
  /** Applied writes, keyed by Idempotency-Key, for replay like the server. */
  private applied = new Map<string, unknown>();

  async install(page: Page): Promise<void> {
    await page.route(/^http:\/\/127\.0\.0\.1:5179\/(auth|api\/v1)\//, (route) =>
      this.handle(route),
    );
  }

  private json(route: Route, data: unknown, status = 200, headers: Record<string, string> = {}) {
    return route.fulfill({
      status,
      contentType: 'application/json',
      headers,
      body: JSON.stringify(data),
    });
  }

  private record(action: string, resourceId: string) {
    this.audit.unshift({
      id: `audit-${this.audit.length + 1}`,
      occurred_at: new Date().toISOString(),
      actor_id: OPERATOR,
      actor_type: 'user',
      action,
      resource_type: 'user',
      resource_id: resourceId,
      request_id: null,
      trace_id: null,
    });
  }

  private async handle(route: Route) {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    const method = request.method();
    if (path === '/auth/login')
      return this.json(route, {
        access_token: token(this.admin ? ['admin'] : []),
        refresh_token: 'synthetic-refresh',
        expires_in: 3600,
      });
    if (path === '/auth/logout') return route.fulfill({ status: 204 });
    if (this.revoked) return this.json(route, { detail: 'Token has been revoked' }, 401);
    if (path === '/auth/me') return this.json(route, this.users[0]);

    if (method !== 'GET') {
      const body = request.postDataJSON() as unknown;
      const idempotencyKey = request.headers()['idempotency-key'] ?? null;
      this.writes.push({ method, path, body, idempotencyKey });
      if (this.failNextWrite) {
        const failure = this.failNextWrite;
        this.failNextWrite = null;
        // A lost response: the command DID apply server-side.
        if (failure.status >= 502) this.apply(path, idempotencyKey, body);
        return this.json(route, { detail: failure.detail }, failure.status);
      }
      return this.json(route, this.apply(path, idempotencyKey, body));
    }

    this.queries.push(`${path}${url.search}`);
    if (path === '/api/v1/users') {
      const q = url.searchParams.get('q')?.toLowerCase();
      const rows = this.users.filter(
        (u) => !q || u.full_name.toLowerCase().includes(q) || u.email.includes(q),
      );
      return this.json(route, this.admin ? rows : { detail: 'Forbidden' }, this.admin ? 200 : 403);
    }
    const userMatch = /^\/api\/v1\/users\/([^/]+)(\/[a-z-]+)?$/.exec(path);
    if (userMatch) {
      const [, id, suffix] = userMatch;
      const found = this.users.find((u) => u.id === id);
      if (!found) return this.json(route, { detail: 'User not found' }, 404);
      if (suffix === '/block') return this.json(route, { id, blocked: this.blocked.has(id!) });
      if (suffix === '/roles') return this.json(route, []);
      return this.json(route, found);
    }
    if (path === '/api/v1/roles')
      return this.json(route, [
        { id: 'support', name: 'Support', code: 'support', is_system: false },
      ]);
    if (path === '/api/v1/permissions') return this.json(route, []);
    if (path.endsWith('/audit')) {
      const rid = url.searchParams.get('resource_id');
      const prefix = url.searchParams.get('action_prefix');
      const limit = Number(url.searchParams.get('limit') ?? 50);
      const offset = Number(url.searchParams.get('cursor') ?? 0);
      const rows = this.audit.filter(
        (row) => (!rid || row.resource_id === rid) && (!prefix || row.action.startsWith(prefix)),
      );
      const page = rows.slice(offset, offset + limit);
      const next: Record<string, string> =
        offset + limit < rows.length ? { 'X-Next-Cursor': String(offset + limit) } : {};
      return this.json(route, page, 200, next);
    }
    const ops = /^\/api\/v1\/platform\/([a-z]+)\/(.+)$/.exec(path);
    const VIEWS = [
      'outbox/dead',
      'streams/dlq',
      'consumers',
      'projections',
      'contracts',
      'modules',
      'settings',
    ];
    if (ops && VIEWS.includes(ops[2] ?? '')) {
      const view = ops[2] ?? '';
      if (view === 'outbox/dead')
        return this.json(route, ops[1] === 'billing' ? this.deadLetters : []);
      if (view === 'streams/dlq')
        return this.json(route, ops[1] === 'billing' ? this.streamDlq : []);
      if (view === 'consumers')
        return this.json(route, [
          {
            name: 'notifier@sub',
            channel: 'billing.subscription.created.v1',
            group: 'billing:notifier@sub',
            kind: 'side_effect',
            module: 'sandloom_billing',
            required: false,
            state: 'reconnecting',
            attempts: 4,
            lag: null,
            pending: null,
            last_connected_at: null,
            last_error: 'ConnectionError (attempt 4)',
          },
          {
            name: 'access@removed',
            channel: 'tenancy.member_removed.v1',
            group: 'billing:access@removed',
            kind: 'side_effect',
            module: 'sandloom_billing',
            required: true,
            state: 'running',
            attempts: 0,
            lag: 0,
            pending: 0,
            last_connected_at: '2026-10-21T08:00:00Z',
            last_error: null,
          },
        ]);
      if (view === 'projections') return this.json(route, []);
      if (view === 'contracts')
        return this.json(route, {
          provided: ['billing.facts.v1', 'billing.credits.v1'],
          consumed: [
            {
              module: 'sandloom_billing',
              contract: 'tenancy.membership.v1',
              required: true,
              resolution: 'remote',
            },
          ],
          bindings: [
            {
              contract: 'tenancy.membership.v1',
              required: true,
              provided_locally: false,
              status: 'ok',
              detail: null,
            },
            {
              contract: 'identity.claims.v1',
              required: false,
              provided_locally: false,
              status: 'incompatible',
              detail: 'identity.claims.v1: removed method contribute',
            },
          ],
          compat_check: 'ready',
        });
      if (view === 'modules')
        return this.json(route, [
          {
            name: 'sandloom_billing',
            version: '1.0.0',
            status: 'loaded',
            required: true,
            detail: null,
          },
          {
            name: 'sandloom.reports',
            version: null,
            status: 'failed',
            required: false,
            detail: 'ImportError: missing dependency',
          },
        ]);
      if (view === 'settings')
        return this.json(route, {
          settings: [
            { key: 'environment', value: 'prod' },
            { key: 'event_bus_backend', value: 'streams' },
          ],
          infrastructure: [
            { capability: 'database', name: 'default', driver: 'sqlalchemy', status: 'ok' },
          ],
          feature_flags: 'not_configured',
        });
    }
    if (path.endsWith('/services'))
      return this.json(route, [
        { id: 'database', status: 'ok', category: 'infrastructure' },
        { id: 'cache', status: 'ok', category: 'infrastructure' },
      ]);
    if (path === '/api/v1/platform/billing/provider')
      return this.json(route, { provider: 'fake', mode: 'demo', dashboard_url: null });
    if (path === '/api/v1/platform/billing/webhooks')
      return this.json(
        route,
        url.searchParams.get('status') === 'failed'
          ? [{ id: 1, event_id: 'evt_1', status: 'failed', received_at: '2026-10-21T08:00:00Z' }]
          : [],
      );
    if (path.startsWith('/api/v1/platform/billing/')) return this.json(route, []);
    if (path === '/api/v1/platform/tenancy/organizations')
      return this.json(route, [
        {
          id: 'org-alpha',
          name: 'Northwind Labs',
          slug: 'northwind',
          owner_id: RILEY,
          is_active: true,
          created_at: '2026-09-01T00:00:00Z',
        },
      ]);
    if (path.startsWith('/api/v1/platform/tenancy/organizations/'))
      return this.json(route, {
        id: 'org-alpha',
        name: 'Northwind Labs',
        slug: 'northwind',
        owner_id: RILEY,
        is_active: true,
        created_at: '2026-09-01T00:00:00Z',
      });
    if (path.endsWith('/membership')) return this.json(route, { detail: 'Not a member' }, 403);
    if (path.startsWith('/api/v1/platform/tenancy/')) return this.json(route, []);
    return this.json(route, { detail: 'Not found' }, 404);
  }

  /** Apply a write once per Idempotency-Key, replaying the first result. */
  private apply(path: string, key: string | null, body: unknown): unknown {
    if (key && this.applied.has(key)) return this.applied.get(key);
    let result: unknown = null;
    const input = (body ?? {}) as { ids?: string[] };
    if (path.endsWith('/outbox/requeue')) {
      const ids = new Set(input.ids ?? []);
      const before = this.deadLetters.length;
      this.deadLetters = this.deadLetters.filter((row) => !ids.has(row.id));
      result = { requested: ids.size, applied: before - this.deadLetters.length };
    }
    if (path.endsWith('/streams/replay')) {
      const ids = new Set(input.ids ?? []);
      const before = this.streamDlq.length;
      this.streamDlq = this.streamDlq.filter((row) => !ids.has(row.id));
      result = { requested: ids.size, applied: before - this.streamDlq.length };
    }
    const safety = /^\/api\/v1\/users\/([^/]+)\/(block|unblock|revoke-sessions)$/.exec(path);
    if (safety) {
      const [, id, command] = safety;
      if (command === 'block') this.blocked.add(id!);
      if (command === 'unblock') this.blocked.delete(id!);
      this.record(
        command === 'revoke-sessions' ? 'identity.user.sessions_revoked' : `auth.user.${command}ed`,
        id!,
      );
      result = command === 'revoke-sessions' ? null : { id, blocked: command === 'block' };
    }
    if (key) this.applied.set(key, result);
    return result;
  }
}

export async function signIn(page: Page, api: MockApi): Promise<void> {
  await api.install(page);
  await page.goto('/');
  await page.getByLabel('Email').fill('morgan@acme.example');
  await page.getByLabel('Password').fill('synthetic-test-password');
  await page.getByRole('button', { name: 'Sign in', exact: true }).click();
}
