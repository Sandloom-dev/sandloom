import { HttpError } from '../api/client';
import type { Field, FormOperation, Operation, AdminResource } from './types';

export function endpoint(op: Operation, resource: AdminResource, id?: string | number, scope = '') {
  if (
    resource.scope &&
    !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(scope)
  )
    throw new HttpError('Select an organization / tenant UUID', 400);
  return op.path.replace(/\{([^}]+)\}/g, (_match, parameter: string) => {
    const value = parameter === resource.scope?.parameter ? scope : id;
    if (value === undefined || String(value).length === 0 || ['.', '..'].includes(String(value)))
      throw new HttpError('Missing record ID', 400);
    return encodeURIComponent(value);
  });
}
export function validateValue(f: Field, value: unknown): string | undefined {
  if (value === null) return f.nullable ? undefined : 'This value cannot be null';
  if (value === undefined || value === '') {
    return f.required ? 'Required' : undefined;
  }
  if (f.type === 'boolean' && typeof value !== 'boolean') return 'Expected a boolean value';
  if (f.type === 'string' && typeof value !== 'string') return 'Expected a text value';
  if (f.enum && !f.enum.includes(value as string | number | boolean))
    return 'Select a value from the list';
  if (typeof value === 'string') {
    // Count code points like the server (Python len), not UTF-16 units.
    // eslint-disable-next-line @typescript-eslint/no-misused-spread
    const length = [...value].length;
    if (f.minLength !== undefined && length < f.minLength)
      return `Minimum ${f.minLength} characters`;
    if (f.maxLength !== undefined && length > f.maxLength)
      return `Maximum ${f.maxLength} characters`;
    if (f.pattern && !new RegExp(f.pattern).test(value)) return 'Invalid format';
    if (f.format === 'email' && !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(value)) return 'Invalid email';
    if (
      f.format === 'uuid' &&
      !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(value)
    )
      return 'Invalid UUID';
    if (
      f.format === 'date' &&
      (!/^\d{4}-\d{2}-\d{2}$/.test(value) || Number.isNaN(Date.parse(value)))
    )
      return 'Invalid date';
    if (f.format === 'date-time' && Number.isNaN(Date.parse(value))) return 'Invalid date and time';
  }
  if (f.type === 'integer' || f.type === 'number') {
    if (
      typeof value !== 'number' ||
      !Number.isFinite(value) ||
      (f.type === 'integer' && !Number.isSafeInteger(value))
    )
      return 'Invalid number';
    if (f.minimum !== undefined && value < f.minimum) return `Minimum ${f.minimum}`;
    if (f.maximum !== undefined && value > f.maximum) return `Maximum ${f.maximum}`;
    if (f.exclusiveMinimum !== undefined && value <= f.exclusiveMinimum)
      return `Greater than ${f.exclusiveMinimum}`;
    if (f.exclusiveMaximum !== undefined && value >= f.exclusiveMaximum)
      return `Less than ${f.exclusiveMaximum}`;
    if (
      f.multipleOf !== undefined &&
      Math.abs(value / f.multipleOf - Math.round(value / f.multipleOf)) > 1e-9
    )
      return `Must be a multiple of ${f.multipleOf}`;
  }
}
/** Build from INPUT allowlist only; response-only authority fields never leak. */
export function payload(op: FormOperation, data: Record<string, unknown>, create = false) {
  const result: Record<string, unknown> = {};
  const errors: Record<string, string> = {};
  for (const f of op.inputs) {
    let value = Object.hasOwn(data, f.name) ? data[f.name] : create ? f.default : undefined;
    if (value === '' && f.nullable) value = null;
    const error = validateValue(f, value);
    if (error) errors[f.name] = error;
    if (value !== undefined) result[f.name] = value;
  }
  if (Object.keys(errors).length) throw new HttpError('Check form fields', 422, { errors });
  return result;
}
export function formError(error: unknown): never {
  if (
    error instanceof HttpError &&
    error.status === 422 &&
    error.body &&
    typeof error.body === 'object'
  ) {
    const body = error.body as { detail?: { loc?: unknown[]; msg?: string }[]; errors?: unknown };
    if (Array.isArray(body.detail)) {
      const errors: Record<string, string> = {};
      for (const e of body.detail)
        if (e.loc?.[0] === 'body' && typeof e.loc[1] === 'string' && e.loc.length === 2)
          errors[e.loc[1]] = e.msg ?? 'Invalid value';
      throw new HttpError(error.message, 422, { ...body, errors });
    }
  }
  throw error;
}
