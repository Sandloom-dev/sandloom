import { useQuery } from '@tanstack/react-query';
import { Ban, LogOut, ShieldCheck } from 'lucide-react';
import { api } from '@/api/client';
import { typedApi } from '@/api/typed';
import { Skeleton } from '@/components/ui/skeleton';
import { useSession, useSubject } from '@/app/session';
import { ActionDialog } from '@/features/resources/action-dialog';
import type { WidgetProps } from '@/features/widget-types';

const client = typedApi(api);

export const blockStatusKey = (subject: string, userId: string) =>
  ['identity', subject, 'block', userId] as const;

function useBlockStatus(userId: string) {
  const subject = useSubject();
  return useQuery({
    queryKey: blockStatusKey(subject, userId),
    queryFn: ({ signal }) =>
      client.get('/api/v1/users/{user_id}/block', { path: { user_id: userId }, signal }),
  });
}

/** Header commands: revoke sessions and block/unblock, chosen from live state. */
export function AccountSafetyActions({ resource, record, scope, invalidate }: WidgetProps) {
  const subject = useSubject();
  const { session } = useSession();
  const userId = String(record.id);
  const status = useBlockStatus(userId);
  if (userId === session?.subject) return null;
  const find = (name: string) => resource.actions.find((action) => action.name === name);
  const revoke = find('revoke-sessions');
  const block = find('block');
  const unblock = find('unblock');
  const keys = [...invalidate, blockStatusKey(subject, userId)];
  if (status.isPending) return <Skeleton className="h-control w-40" />;
  return (
    <>
      {revoke && (
        <ActionDialog
          action={revoke}
          resource={resource}
          record={record}
          scope={scope}
          invalidate={keys}
          icon={<LogOut aria-hidden />}
        />
      )}
      {status.data?.blocked
        ? unblock && (
            <ActionDialog
              action={unblock}
              resource={resource}
              record={record}
              scope={scope}
              invalidate={keys}
              variant="primary"
              icon={<ShieldCheck aria-hidden />}
            />
          )
        : block && (
            <ActionDialog
              action={block}
              resource={resource}
              record={record}
              scope={scope}
              invalidate={keys}
              icon={<Ban aria-hidden />}
            />
          )}
    </>
  );
}

/** Banner shown while the kernel's block mark is set for this user. */
export function AccountSafetyBanner({ record }: WidgetProps) {
  const status = useBlockStatus(String(record.id));
  if (status.isError)
    return (
      <p role="status" className="rounded-lg border border-warning bg-warning-muted p-3">
        Block state is unavailable right now; the account may or may not be blocked.
      </p>
    );
  if (!status.data?.blocked) return null;
  return (
    <div
      role="status"
      className="flex items-start gap-3 rounded-lg border border-danger bg-danger-muted p-3"
    >
      <Ban aria-hidden className="mt-0.5 size-4 shrink-0 text-danger" />
      <p>
        <span className="font-semibold">This user is blocked.</span> Every request with an existing
        token is rejected and sign-in is refused until the block is lifted. The reason is in the
        activity log.
      </p>
    </div>
  );
}
