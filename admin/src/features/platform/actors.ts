import { useQueries } from '@tanstack/react-query';
import { api } from '@/api/client';
import { typedApi } from '@/api/typed';
import { useConfig } from '@/app/config-context';
import { useSession } from '@/app/session';
import { allowed } from '@/schema/types';
import { shortId } from '@/lib/format';

const client = typedApi(api);

/**
 * Display names for audit actors, resolved through identity's own user API
 * (one cached read per distinct actor; nothing when users are not admitted).
 * Unknown or unreadable actors fall back to a short id.
 */
export function useActorNames(
  ids: readonly (string | null | undefined)[],
): (id: string | null) => string {
  const { manifest } = useConfig();
  const { session } = useSession();
  const canRead =
    session !== undefined &&
    manifest.resources.some((r) => r.name === 'users' && allowed(r.access, session.roles));
  const unique = [...new Set(ids.filter((id): id is string => Boolean(id)))].slice(0, 50);
  const results = useQueries({
    queries: unique.map((id) => ({
      queryKey: ['actor', session?.subject, id],
      enabled: canRead,
      staleTime: Number.POSITIVE_INFINITY,
      retry: false,
      queryFn: ({ signal }: { signal: AbortSignal }) =>
        client.get('/api/v1/users/{user_id}', { path: { user_id: id }, signal }),
    })),
  });
  const names = new Map<string, string>();
  unique.forEach((id, index) => {
    const user = results[index]?.data;
    if (user) names.set(id, user.full_name || user.email);
  });
  return (id) => {
    if (!id) return 'System';
    if (id === session?.subject) return 'You';
    return names.get(id) ?? `Operator ${shortId(id)}`;
  };
}
