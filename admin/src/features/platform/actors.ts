import { useQueries } from '@tanstack/react-query';
import { api } from '@/api/client';
import { typedApi } from '@/api/typed';
import { useConfig } from '@/app/config-context';
import { useSession } from '@/app/session';
import { allowed } from '@/schema/types';
import { shortId } from '@/lib/format';

const client = typedApi(api);

/** The parts of an audit row that name who did what to whom. */
export interface AuditParties {
  actor_id: string | null;
  actor_type: string;
  resource_type?: string | null;
  resource_id: string | null;
}

/**
 * Who acted and on whom, for a timeline line: users by email when identity
 * told us (`nameOf`), the signed-in operator as "You", anyone else by a short
 * id. A subject that is not a user keeps its short id.
 */
export function auditParties(
  row: AuditParties,
  nameOf: (id: string) => string | undefined,
  self: string | undefined,
): { actor: string; subject: string | null } {
  const actor = row.actor_id
    ? row.actor_id === self
      ? 'You'
      : (nameOf(row.actor_id) ?? `Operator ${shortId(row.actor_id)}`)
    : row.actor_type;
  const id = row.resource_id;
  const subject = id
    ? ((row.resource_type === 'user' ? nameOf(id) : undefined) ?? shortId(id))
    : null;
  return { actor, subject };
}

/**
 * The emails of users the audit names (actors, and subjects of type `user`),
 * read through identity's own user API: one cached read per distinct id,
 * nothing when users are not admitted. Unknown ids stay unknown.
 */
export function useUserEmails(
  ids: readonly (string | null | undefined)[],
): (id: string) => string | undefined {
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
  const emails = new Map<string, string>();
  unique.forEach((id, index) => {
    const user = results[index]?.data;
    if (user) emails.set(id, user.email);
  });
  return (id) => emails.get(id);
}

/** Display names for audit actors: email, "You", "System", else a short id. */
export function useActorNames(
  ids: readonly (string | null | undefined)[],
): (id: string | null) => string {
  const { session } = useSession();
  const emailOf = useUserEmails(ids);
  return (id) => {
    if (!id) return 'System';
    return auditParties(
      { actor_id: id, actor_type: '', resource_id: null },
      emailOf,
      session?.subject,
    ).actor;
  };
}
