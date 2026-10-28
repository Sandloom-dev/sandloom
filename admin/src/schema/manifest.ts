import type { AdminManifest } from './types';

function fail(): never {
  throw new Error('Invalid admin manifest');
}
function object(v: unknown): Record<string, unknown> {
  return v && typeof v === 'object' && !Array.isArray(v) ? (v as Record<string, unknown>) : fail();
}
function strings(v: unknown): string[] {
  return Array.isArray(v) && v.every((x) => typeof x === 'string') ? v : fail();
}
const safe = (v: unknown) =>
  typeof v === 'string' &&
  /^[a-zA-Z_][a-zA-Z0-9_]*$/.test(v) &&
  !['__proto__', 'constructor', 'prototype'].includes(v);
function access(v: unknown) {
  const a = object(v);
  const roles = strings(a.roles);
  if (
    typeof a.authenticated !== 'boolean' ||
    a.authenticated === Boolean(roles.length) ||
    roles.some((r) => !r.trim())
  )
    fail();
}
function fields(v: unknown, writable = false) {
  if (!Array.isArray(v) || v.length > 100) fail();
  const names = new Set();
  for (const value of v) {
    const f = object(value);
    if (
      !safe(f.name) ||
      names.has(f.name) ||
      typeof f.label !== 'string' ||
      typeof f.required !== 'boolean' ||
      typeof f.nullable !== 'boolean'
    )
      fail();
    names.add(f.name);
    if (
      !(
        writable
          ? ['string', 'boolean', 'integer', 'number']
          : ['string', 'boolean', 'integer', 'number', 'json']
      ).includes(String(f.type))
    )
      fail();
    if (
      f.format !== undefined &&
      !['email', 'uuid', 'date', 'date-time'].includes(f.format as string)
    )
      fail();
    if (
      f.enum !== undefined &&
      (!Array.isArray(f.enum) ||
        f.enum.some((x) => !['string', 'number', 'boolean'].includes(typeof x)))
    )
      fail();
    for (const n of [
      'minLength',
      'maxLength',
      'minimum',
      'maximum',
      'exclusiveMinimum',
      'exclusiveMaximum',
      'multipleOf',
    ]) {
      if (f[n] !== undefined && (typeof f[n] !== 'number' || !Number.isFinite(f[n]))) fail();
    }
    if (f.pattern !== undefined) {
      if (typeof f.pattern !== 'string' || f.pattern.length > 512 || /\(\?(?!:)/.test(f.pattern))
        fail();
      try {
        new RegExp(f.pattern);
      } catch {
        fail();
      }
    }
    if (f.description !== undefined && typeof f.description !== 'string') fail();
  }
}
function operation(v: unknown, scope: string | null, record: boolean, methods: string[]) {
  const op = object(v);
  if (
    !methods.includes(String(op.method)) ||
    typeof op.path !== 'string' ||
    !/^\/api\/v1\/[A-Za-z0-9_/{}.-]+$/.test(op.path) ||
    op.path.includes('//') ||
    op.path.split('/').some((p) => ['.', '..'].includes(p))
  )
    fail();
  const params = [...op.path.matchAll(/\{([^}]+)\}/g)].map((m) => m[1]);
  if (
    params.some((p) => !safe(p)) ||
    (scope && !params.includes(scope)) ||
    params.filter((p) => p !== scope).length !== Number(record)
  )
    fail();
}
function form(v: unknown, scope: string | null, record: boolean, methods: string[]) {
  operation(v, scope, record, methods);
  const f = object(v);
  fields(f.inputs, true);
  const names = strings(f.fields);
  if (
    f.idempotency_field != null &&
    !(f.inputs as { name: string; format?: string; required: boolean }[]).some(
      (x) => x.name === f.idempotency_field && x.format === 'uuid' && x.required,
    )
  )
    fail();
  if (
    typeof f.has_body !== 'boolean' ||
    JSON.stringify(names) !== JSON.stringify((f.inputs as { name: string }[]).map((x) => x.name))
  )
    fail();
}
/** Runtime validation complements the stricter OpenAPI/ownership compiler in CI. */
export function parseManifest(value: unknown): AdminManifest {
  const m = object(value);
  if (m.version !== 1 || !Array.isArray(m.resources) || m.resources.length > 500) fail();
  const modules = strings(m.modules);
  if (new Set(modules).size !== modules.length || modules.some((x) => !safe(x))) fail();
  const names = new Set();
  for (const item of m.resources) {
    const r = object(item);
    if (
      typeof r.name !== 'string' ||
      !/^[a-z][a-z0-9_.-]*$/.test(r.name) ||
      ['__proto__', 'constructor', 'prototype'].includes(r.name) ||
      names.has(r.name) ||
      typeof r.label !== 'string' ||
      !modules.includes(String(r.module))
    )
      fail();
    names.add(r.name);
    access(r.access);
    if (r.group !== null && (typeof r.group !== 'string' || !r.group.trim())) fail();
    if (r.icon !== null && (typeof r.icon !== 'string' || !/^[a-z][a-z0-9-]*$/.test(r.icon)))
      fail();
    const scope = r.scope === null ? null : object(r.scope);
    if (
      scope &&
      ((scope.parameter !== null && !safe(scope.parameter)) || typeof scope.label !== 'string')
    )
      fail();
    if (r.audit != null) {
      const binding = object(r.audit);
      if (
        !r.detail ||
        typeof binding.resource_type !== 'string' ||
        !/^[a-z][a-z0-9_.-]*$/.test(binding.resource_type) ||
        binding.resource_type.length > 100 ||
        !safe(binding.id_field)
      )
        fail();
    }
    const parameter = typeof scope?.parameter === 'string' ? scope.parameter : null;
    operation(r.list, parameter, false, ['GET']);
    const p = object(r.pagination);
    if (
      !['all', 'offset', 'cursor'].includes(String(p.mode)) ||
      ![p.limit, p.offset, p.cursor].every(safe) ||
      typeof p.next_header !== 'string' ||
      !/^[A-Za-z0-9-]+$/.test(p.next_header) ||
      !Number.isInteger(p.max_page_size) ||
      Number(p.max_page_size) < 1 ||
      Number(p.max_page_size) > 100
    )
      fail();
    fields(r.columns);
    fields(r.show);
    fields(r.filters, true);
    if (r.detail !== null) operation(r.detail, parameter, true, ['GET']);
    if (r.create !== null) form(r.create, parameter, false, ['POST']);
    if (r.edit !== null) form(r.edit, parameter, true, ['PUT', 'PATCH']);
    if (r.delete !== null) operation(r.delete, parameter, true, ['DELETE']);
    if (!r.detail && (r.edit || r.delete)) fail();
    if (r.items_field !== null && !safe(r.items_field)) fail();
    if (r.sort_parameter !== null && !safe(r.sort_parameter)) fail();
    strings(r.sort_fields);
    strings(r.show_widgets);
    strings(r.edit_widgets);
    if (!Array.isArray(r.actions)) fail();
    const actions = new Set();
    for (const a of r.actions) {
      form(a, parameter, true, ['POST', 'PUT', 'PATCH', 'DELETE']);
      const action = object(a);
      if (
        !r.detail ||
        typeof action.name !== 'string' ||
        !/^[a-z][a-z0-9_-]*$/.test(action.name) ||
        ['constructor', 'prototype', '__proto__'].includes(action.name) ||
        actions.has(action.name) ||
        typeof action.label !== 'string' ||
        typeof action.confirm !== 'string' ||
        !action.confirm.trim()
      )
        fail();
      actions.add(action.name);
      if (action.access !== null) access(action.access);
      if (
        !['default', 'danger'].includes(String(action.tone)) ||
        !['toolbar', 'widget'].includes(String(action.placement)) ||
        (action.typed_confirmation !== null &&
          !(r.show as { name: string }[]).some((f) => f.name === action.typed_confirmation))
      )
        fail();
    }
  }
  return value as AdminManifest;
}
