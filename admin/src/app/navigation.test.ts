import { describe, expect, it } from 'vitest';
import { parseManifest } from '@/schema/manifest';
import raw from '../../public/admin-manifest.json';
import { buildNavigation } from './navigation';

const manifest = parseManifest(raw);

describe('buildNavigation', () => {
  it('groups manifest resources by their declared group and collapses platform sources', () => {
    const groups = buildNavigation(manifest, ['admin']);
    const labels = groups.map((group) => group.label);
    expect(labels[0]).toBeNull();
    expect(labels).toContain('Customers');
    expect(labels.at(-1)).toBe('Platform');
    const platform = groups.at(-1)?.items.map((item) => item.label);
    expect(platform).toEqual([
      'Service health',
      'Event delivery',
      'Contracts & modules',
      'Runtime settings',
      'Audit log',
      'Operator guide',
    ]);
    const all = groups.flatMap((group) => group.items.map((item) => item.to));
    expect(all.some((to) => /audit_|services_/.test(to))).toBe(false);
  });
  it('shows nothing role-gated to an operator without the role', () => {
    const groups = buildNavigation(manifest, []);
    expect(groups.flatMap((group) => group.items).map((item) => item.label)).toEqual([
      'Overview',
      'Operator guide',
    ]);
  });
});
