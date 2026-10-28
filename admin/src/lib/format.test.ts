import { describe, expect, it } from 'vitest';
import { formatRelative, humanize, initials, scalarText, shortId } from './format';

describe('format helpers', () => {
  it('derives initials from names and emails', () => {
    expect(initials('Avery Chen')).toBe('AC');
    expect(initials('Morgan Lee Ortiz')).toBe('MO');
    expect(initials('riley@northwind.example')).toBe('RI');
    expect(initials('')).toBe('');
  });
  it('shortens long identifiers but keeps short ones', () => {
    expect(shortId('5f2c8a1d-0000-4000-8000-0000000091ab')).toBe('5f2c…91ab');
    expect(shortId('cus_1')).toBe('cus_1');
  });
  it('formats relative times', () => {
    const now = Date.parse('2026-10-21T12:00:00Z');
    expect(formatRelative('2026-10-21T10:00:00Z', now)).toBe('2 hours ago');
    expect(formatRelative('not a date', now)).toBe('not a date');
  });
  it('renders scalars safely', () => {
    expect(scalarText(3)).toBe('3');
    expect(scalarText(null)).toBe('');
    expect(scalarText({ a: 1 })).toBe('{"a":1}');
    expect(humanize('is_active')).toBe('Is active');
  });
});
