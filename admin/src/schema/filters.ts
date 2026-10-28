import type { Field } from './types';
import { validateValue } from './values';

/** Filters travel as strings (URL search params); convert per declared type. */
export function parseFilterValue(field: Field, raw: string): unknown {
  if (field.type === 'boolean') return raw === 'true' ? true : raw === 'false' ? false : raw;
  if (field.type === 'integer' || field.type === 'number') {
    const value = Number(raw);
    return raw.trim() === '' || Number.isNaN(value) ? raw : value;
  }
  if (field.enum) {
    const match = field.enum.find((option) => String(option) === raw);
    return match ?? raw;
  }
  return raw;
}

export function filterError(field: Field, raw: string): string | undefined {
  return validateValue({ ...field, required: false }, parseFilterValue(field, raw));
}

export function describeFilter(field: Field, raw: string): string {
  if (field.type === 'boolean') return raw === 'true' ? 'Yes' : 'No';
  return raw;
}
