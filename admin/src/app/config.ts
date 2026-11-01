import { parseManifest } from '@/schema/manifest';
import { widgetNames } from '@/features/widgets';
import type { AdminManifest } from '@/schema/types';

export interface AdminConfig {
  modules: Set<string>;
  manifest: AdminManifest;
}
// Static same-origin bootstrap files, read before any API session exists.
const bootstrapFile = (name: string) =>
  fetch(`${import.meta.env.BASE_URL}${name}`, { cache: 'no-store', credentials: 'omit' }); // eslint-disable-line no-restricted-globals

// A server that serves the console says what is wrong; a static host just answers 404.
async function failure(response: Response): Promise<string> {
  const body: unknown = await response.json().catch(() => null);
  return body && typeof body === 'object' && 'detail' in body && typeof body.detail === 'string'
    ? body.detail
    : 'Could not load the admin configuration';
}

export async function loadConfig(): Promise<AdminConfig> {
  const [response, manifestResponse] = await Promise.all([
    bootstrapFile('config.json'),
    bootstrapFile('admin-manifest.json'),
  ]);
  if (!manifestResponse.ok) throw new Error(await failure(manifestResponse));
  if (!response.ok) throw new Error(await failure(response));
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
