import { expect, it } from 'vitest';
import { listHref, recordHref, formHref } from './routes';
it('keeps tenant context through the complete resource navigation cycle', () => {
  expect(recordHref('notes', 1, 'tenant-a')).toBe('/notes/1?tenant=tenant-a');
  expect(formHref('notes', undefined, 'tenant-a')).toBe('/notes/new?tenant=tenant-a');
  expect(formHref('notes', 1, 'tenant-a')).toBe('/notes/1/edit?tenant=tenant-a');
  expect(listHref('notes', { q: 'test' }, 'tenant-a')).toBe('/notes?f.q=test&tenant=tenant-a');
});
