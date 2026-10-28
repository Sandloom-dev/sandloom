import type { LucideIcon } from 'lucide-react';
import {
  Activity,
  BookOpen,
  FileText,
  Home,
  Inbox,
  Network,
  SlidersHorizontal,
} from 'lucide-react';
import { iconFor } from '@/lib/icons';
import { allowed, type AdminManifest } from '@/schema/types';

export interface NavItem {
  label: string;
  to: string;
  icon: LucideIcon;
  /** Extra words the command palette matches on. */
  keywords?: string[];
}
export interface NavGroup {
  label: string | null;
  items: NavItem[];
}

/** Per-module audit/health resources are presented as one page with a source switch. */
export const isPlatformSource = (name: string) => /^(audit|services)_/.test(name);

/** Navigation is derived from the manifest: groups and icons are declared by modules. */
export function buildNavigation(manifest: AdminManifest, roles: readonly string[]): NavGroup[] {
  const visible = manifest.resources.filter((resource) => allowed(resource.access, roles));
  const groups = new Map<string, NavItem[]>();
  for (const resource of visible) {
    if (isPlatformSource(resource.name)) continue;
    const group = resource.group ?? resource.module;
    groups.set(group, [
      ...(groups.get(group) ?? []),
      {
        label: resource.label,
        to: `/${resource.name}`,
        icon: iconFor(resource.icon),
        keywords: [resource.module, resource.name],
      },
    ]);
  }
  const platform: NavItem[] = [];
  // The kernel installs every operations view next to `services` per module source.
  if (visible.some((resource) => resource.name.startsWith('services_')))
    platform.push(
      {
        label: 'Service health',
        to: '/platform/health',
        icon: Activity,
        keywords: ['status', 'services'],
      },
      {
        label: 'Event delivery',
        to: '/platform/events',
        icon: Inbox,
        keywords: ['outbox', 'dlq', 'dead letters', 'streams', 'consumers', 'projections'],
      },
      {
        label: 'Contracts & modules',
        to: '/platform/contracts',
        icon: Network,
        keywords: ['bindings', 'rpc', 'compatibility', 'modules'],
      },
      {
        label: 'Runtime settings',
        to: '/platform/settings',
        icon: SlidersHorizontal,
        keywords: ['config', 'infrastructure', 'feature flags'],
      },
    );
  if (visible.some((resource) => resource.name.startsWith('audit_')))
    platform.push({
      label: 'Audit log',
      to: '/platform/audit',
      icon: FileText,
      keywords: ['history', 'journal'],
    });
  platform.push({
    label: 'Operator guide',
    to: '/help',
    icon: BookOpen,
    keywords: ['help', 'docs'],
  });
  return [
    {
      label: null,
      items: [{ label: 'Overview', to: '/', icon: Home, keywords: ['dashboard', 'home'] }],
    },
    ...[...groups].map(([label, items]) => ({ label, items })),
    { label: 'Platform', items: platform },
  ];
}
