import { parseManifest } from '@/schema/manifest';
import { widgetNames } from '@/features/widgets';
import type { AdminManifest } from '@/schema/types';

export interface AdminConfig {
  modules: Set<string>;
  manifest: AdminManifest;
}
// Static same-origin bootstrap files, read before any API session exists.
/* eslint-disable no-restricted-globals */
export async function loadConfig(): Promise<AdminConfig> {
  const [response, manifestResponse] = await Promise.all([
    fetch('/config.json', { cache: 'no-store', credentials: 'omit' }),
    fetch('/admin-manifest.json', { cache: 'no-store', credentials: 'omit' }),
  ]);
  if (!response.ok || !manifestResponse.ok)
    throw new Error('Could not load the admin configuration');
  const manifest = parseManifest(await manifestResponse.json());
  for (const r of manifest.resources)
    for (const name of [...r.show_widgets, ...r.edit_widgets])
      if (!widgetNames.includes(name))
        throw new Error('The manifest requires an unknown UI component');
  const config: unknown = await response.json();
  const modules: unknown =
    config && typeof config === 'object' && 'modules' in config ? config.modules : null;
  if (
    !Array.isArray(modules) ||
    !modules.includes('identity') ||
    modules.some((m) => typeof m !== 'string' || !manifest.modules.includes(m)) ||
    new Set(modules).size !== modules.length
  )
    throw new Error('Invalid admin module configuration');
  const names = modules as string[];
  return {
    modules: new Set([...names, 'core']),
    manifest: {
      ...manifest,
      resources: manifest.resources.filter(
        (r) =>
          names.includes(r.module) ||
          (r.module === 'core' && names.some((m) => r.name.endsWith(`_${m}`))),
      ),
    },
  };
}
/* eslint-enable no-restricted-globals */
