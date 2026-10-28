import { describe, it, expect } from 'vitest';
import { ApiClient, HttpError } from '../src/api/client';
import { fetchListPage, type ListParams } from '../src/features/resources/data';
import { fieldErrors } from '../src/features/resources/form-errors';
import { parseManifest } from '../src/schema/manifest';
import { parseFilterValue, filterError } from '../src/schema/filters';
import { payload, validateValue, endpoint, formError } from '../src/schema/values';
import raw from './fixtures/catalog-manifest.json';

const manifest = parseManifest(raw);
const resource = manifest.resources[0]!;
const params: ListParams = {
  page: 1,
  pageSize: 2,
  sort: { field: 'name', desc: true },
  filters: { state: 'draft' },
  scope: '',
};
const token = `x.${btoa(JSON.stringify({ sub: 'operator', roles: ['admin'], claims_sources: ['identity'] }))}.x`;

async function client(handler: (path: string, options?: RequestInit) => Response) {
  const api = new ApiClient(async (path, options) =>
    path === '/auth/login'
      ? Response.json({ access_token: token, refresh_token: 'private', expires_in: 3600 })
      : path === '/auth/me'
        ? Response.json({ id: 'operator' })
        : handler(
            typeof path === 'string' ? path : path instanceof URL ? path.href : path.url,
            options,
          ),
  );
  await api.login({ email: 'operator@example.com', password: 'private' });
  return api;
}

describe('compiled resource language', () => {
  it('lists with declared filters, the explicit sort protocol and offset lookahead', async () => {
    const api = await client((path) => {
      expect(path).toBe('/api/v1/catalog?state=draft&sort=-name&skip=0&limit=3');
      return Response.json([{ id: 'a' }, { id: 'b' }, { id: 'c' }]);
    });
    const page = await fetchListPage(api, resource, params, new Map());
    expect(page.rows.map((r) => r.id)).toEqual(['a', 'b']);
    expect(page).toMatchObject({ hasNextPage: true, hasPreviousPage: false });
  });

  it('refuses unknown filters, bad pages and bad sorts without a request', async () => {
    const api = await client(() => {
      throw new Error('must not call');
    });
    for (const bad of [
      { ...params, filters: { role: 'admin' } },
      { ...params, page: 0 },
      { ...params, pageSize: 101 },
      { ...params, sort: { field: 'quantity', desc: false } },
    ])
      await expect(fetchListPage(api, resource, bad, new Map())).rejects.toBeInstanceOf(HttpError);
  });

  it('walks cursors and never jumps to an unseen page', async () => {
    const cursorResource = {
      ...resource,
      pagination: { ...resource.pagination, mode: 'cursor' as const },
    };
    const seen: string[] = [];
    const api = await client((path) => {
      seen.push(path);
      return Response.json([{ id: 'row' }], { headers: { 'X-Next-Cursor': 'opaque' } });
    });
    const chain = new Map<number, string>();
    await expect(
      fetchListPage(api, cursorResource, { ...params, page: 2 }, chain),
    ).rejects.toMatchObject({ status: 400 });
    const first = await fetchListPage(api, cursorResource, params, chain);
    expect(first.hasNextPage).toBe(true);
    await fetchListPage(api, cursorResource, { ...params, page: 2 }, chain);
    expect(seen.at(-1)).toContain('cursor=opaque');
  });

  it('sends the tenant header for header-scoped resources', async () => {
    const scoped = { ...resource, scope: { parameter: null, label: 'Tenant' } };
    const tenant = '00000000-0000-4000-8000-000000000001';
    let header: string | null = null;
    const api = await client((_path, options) => {
      header = new Headers(options?.headers).get('X-Tenant-Id');
      return Response.json([]);
    });
    await fetchListPage(api, scoped, { ...params, scope: tenant }, new Map());
    expect(header).toBe(tenant);
  });

  it('writes only admitted input fields and keeps false and zero defaults', () => {
    expect(
      payload(resource.create!, { name: 'New', is_superuser: true, id: 'fake' }, true),
    ).toEqual({
      name: 'New',
      state: 'draft',
      quantity: 0,
      enabled: false,
    });
    expect(() => payload(resource.create!, { name: 'x', quantity: 501 }, true)).toThrow(
      'Check form fields',
    );
  });

  it('maps local and server validation errors onto inputs', () => {
    try {
      payload(resource.create!, { name: 'x' }, true);
    } catch (error) {
      expect(fieldErrors(error)).toEqual({ name: 'Minimum 2 characters' });
    }
    const server = new HttpError('Invalid', 422, {
      detail: [{ loc: ['body', 'name'], msg: 'Already exists' }],
    });
    expect(fieldErrors(server)).toEqual({ name: 'Already exists' });
    expect(() => formError(server)).toThrow(HttpError);
  });

  it('validates enums, numeric types, constraints and UUIDs', () => {
    const f = resource.create!.inputs.find((field) => field.name === 'state')!;
    expect(validateValue(f, 'unknown')).toBeDefined();
    expect(validateValue({ ...f, type: 'integer', enum: undefined }, 2.2)).toBeDefined();
    expect(
      validateValue({ ...f, type: 'string', format: 'uuid', enum: undefined }, 'other'),
    ).toBeDefined();
  });

  it('converts URL filter strings to the declared type', () => {
    const state = resource.filters[0]!;
    expect(parseFilterValue(state, 'draft')).toBe('draft');
    expect(filterError(state, 'nope')).toBeDefined();
    expect(parseFilterValue({ ...state, type: 'boolean', enum: undefined }, 'true')).toBe(true);
    expect(parseFilterValue({ ...state, type: 'integer', enum: undefined }, '7')).toBe(7);
  });

  it('requires an explicit tenant context and binds it independently of record IDs', () => {
    const scoped = { ...resource, scope: { parameter: 'org_id', label: 'Tenant' } };
    const op = { ...resource.detail!, path: '/api/v1/orgs/{org_id}/catalog/{item_id}' };
    expect(() => endpoint(op, scoped, 'entry', '')).toThrow('tenant UUID');
    expect(endpoint(op, scoped, 'entry', '00000000-0000-4000-8000-000000000001')).toBe(
      '/api/v1/orgs/00000000-0000-4000-8000-000000000001/catalog/entry',
    );
  });

  it('carries presentation hints from the compiler', () => {
    expect(resource.group).toBe('Inventory');
    expect(resource.icon).toBe('package');
    expect(resource.actions[0]).toMatchObject({ tone: 'danger', typed_confirmation: 'name' });
  });

  it.each<(m: typeof raw) => void>([
    (m) => {
      m.version = 2;
    },
    (m) => {
      m.resources[0]!.list.path = 'https://evil.test/api/v1/catalog';
    },
    (m) => {
      m.resources[0]!.create.inputs[0]!.name = '__proto__';
    },
    (m) => {
      m.resources[0]!.access.roles = [];
    },
    (m) => {
      m.resources.push(m.resources[0]!);
    },
    (m) => {
      m.resources[0]!.icon = 'Not An Icon';
    },
    (m) => {
      m.resources[0]!.actions[0]!.tone = 'scary';
    },
    (m) => {
      m.resources[0]!.actions[0]!.typed_confirmation = 'secret_field';
    },
  ])('rejects unsupported versions, remote endpoints and unsafe declarations', (change) => {
    const m = structuredClone(raw);
    change(m);
    expect(() => parseManifest(m)).toThrow('manifest');
  });
});
