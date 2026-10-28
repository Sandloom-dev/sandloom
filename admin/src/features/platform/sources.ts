import { useQueries } from '@tanstack/react-query';
import { api } from '@/api/client';
import { useConfig } from '@/app/config-context';
import { useSubject } from '@/app/session';
import { allowed, type AdminResource } from '@/schema/types';
import { useSession } from '@/app/session';

export interface ServiceRow {
  id: string;
  status: string;
  category: string;
}

/** Per-module platform sources (`services_<module>`, `audit_<module>`) the operator may read. */
export function usePlatformSources(kind: 'services' | 'audit'): AdminResource[] {
  const { manifest } = useConfig();
  const { session } = useSession();
  return manifest.resources.filter(
    (resource) =>
      resource.module === 'core' &&
      resource.name.startsWith(`${kind}_`) &&
      session !== undefined &&
      allowed(resource.access, session.roles),
  );
}

export const sourceName = (resource: AdminResource) => resource.name.replace(/^[a-z]+_/, '');

export function useServiceHealth() {
  const subject = useSubject();
  const sources = usePlatformSources('services');
  const results = useQueries({
    queries: sources.map((source) => ({
      queryKey: ['platform', subject, 'services', source.name],
      queryFn: ({ signal }: { signal: AbortSignal }) =>
        api.request<ServiceRow[]>(source.list.path, { signal }),
      refetchInterval: 30_000,
    })),
  });
  return sources.map((source, index) => ({ source, result: results[index] }));
}
