const dateTime = new Intl.DateTimeFormat('en-US', { dateStyle: 'medium', timeStyle: 'short' });
const dateOnly = new Intl.DateTimeFormat('en-US', { dateStyle: 'medium' });
const number = new Intl.NumberFormat('en-US', { maximumFractionDigits: 20 });
const relative = new Intl.RelativeTimeFormat('en-US', { numeric: 'auto' });

export const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export function formatDateTime(value: string | number | Date): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : dateTime.format(date);
}

export function formatDate(value: string | number | Date): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : dateOnly.format(date);
}

export function formatNumber(value: number): string {
  return number.format(value);
}

const STEPS: [Intl.RelativeTimeFormatUnit, number][] = [
  ['second', 60],
  ['minute', 60],
  ['hour', 24],
  ['day', 30],
  ['month', 12],
  ['year', Number.POSITIVE_INFINITY],
];

/** "3 hours ago" style; falls back to the raw value when unparsable. */
export function formatRelative(value: string | number | Date, now: number = Date.now()): string {
  const time = new Date(value).getTime();
  if (Number.isNaN(time)) return String(value);
  let delta = (time - now) / 1000;
  for (const [unit, size] of STEPS) {
    if (Math.abs(delta) < size) return relative.format(Math.round(delta), unit);
    delta /= size;
  }
  return formatDateTime(value);
}

/** 5f2c8a1d-…-91ab → "5f2c…91ab" for dense tables; the full value stays copyable. */
export function shortId(value: string): string {
  return value.length > 12 ? `${value.slice(0, 4)}…${value.slice(-4)}` : value;
}

/** "Avery Chen" → "AC"; email or empty input degrades gracefully. */
export function initials(name: string): string {
  const parts = name
    .replace(/@.*/, '')
    .split(/[\s._-]+/)
    .filter(Boolean);
  const first = parts[0] ?? '';
  const last = parts.length > 1 ? (parts.at(-1) ?? '') : '';
  return (last ? `${first.charAt(0)}${last.charAt(0)}` : first.slice(0, 2)).toUpperCase();
}

/** "is_active" → "Is active" — only for labels the manifest did not supply. */
export function humanize(name: string): string {
  const text = name.replace(/_/g, ' ');
  return text.charAt(0).toUpperCase() + text.slice(1);
}

/** Display text for an API scalar; structured values are shown as JSON. */
export function scalarText(value: unknown): string {
  if (typeof value === 'string') return value;
  if (typeof value === 'number' || typeof value === 'boolean' || typeof value === 'bigint')
    return String(value);
  if (value === null || value === undefined) return '';
  return JSON.stringify(value);
}
