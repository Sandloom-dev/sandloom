import { useId, useState } from 'react';
import { useQuery, type QueryKey } from '@tanstack/react-query';
import { api, type Member, type Schema } from '@/api/client';
import { typedApi } from '@/api/typed';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Input, NativeSelect } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { ConfirmDialog } from '@/components/patterns/confirm-dialog';
import { CopyText } from '@/components/patterns/copy-text';
import { EmptyState, ErrorState, LoadingRows } from '@/components/patterns/states';
import { useCommand } from '@/hooks/use-command';
import { useSubject } from '@/app/session';
import type { WidgetProps } from '@/features/widget-types';
import { scalarText } from '@/lib/format';

const client = typedApi(api);
const idempotency = (reference: string) => ({ 'Idempotency-Key': reference });
type Role = 'owner' | 'admin' | 'member';

interface Pending {
  title: string;
  description: string;
  label: string;
  danger: boolean;
  run: (reference: string) => Promise<unknown>;
}

/**
 * The operator's OWN membership in this organization. Platform access grants
 * no tenant authority: management controls appear only for an owner/admin
 * member, and the tenancy API re-checks every command.
 */
export function OrganizationWorkspace({ record }: WidgetProps) {
  const subject = useSubject();
  const orgId = String(record.id);
  const keyBase = ['tenancy', subject, orgId] as const;
  const [pending, setPending] = useState<Pending | null>(null);
  const membership = useQuery({
    queryKey: [...keyBase, 'membership'],
    queryFn: ({ signal }) =>
      client.get('/api/v1/orgs/{org_id}/membership', { path: { org_id: orgId }, signal }),
    retry: false,
  });
  const role = membership.data?.role as Role | undefined;
  // Narrowed role of a member allowed to manage, else null.
  const manager = role === 'owner' || role === 'admin' ? role : null;
  const command = useCommand<Pending, unknown>({
    run: (input, reference) => input.run(reference),
    success: 'Changes saved',
    invalidate: [keyBase],
    onSuccess: () => {
      setPending(null);
    },
  });
  const ask = (next: Pending) => {
    // A different command: new reference (an unknown outcome keeps its own)
    command.reset();
    setPending(next);
  };
  return (
    <Card>
      <CardHeader>
        <CardTitle>Your access to this organization</CardTitle>
        {role && <Badge tone="accent">{role}</Badge>}
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        {membership.isError ? (
          <p className="text-fg-muted">
            You are not a member of this organization. Platform administration does not grant
            organization roles; membership management stays with its owners.
          </p>
        ) : (
          manager && <Rename orgId={orgId} current={scalarText(record.name)} ask={ask} />
        )}
        {membership.isSuccess && (
          <Members orgId={orgId} subject={subject} role={role} ask={ask} queryKey={keyBase} />
        )}
        {manager && <Invitations orgId={orgId} role={manager} ask={ask} queryKey={keyBase} />}
      </CardContent>
      {pending && (
        <ConfirmDialog
          open
          onOpenChange={(open) => {
            if (!open) setPending(null);
          }}
          title={pending.title}
          description={pending.description}
          confirmLabel={pending.label}
          tone={pending.danger ? 'danger' : 'default'}
          reference={command.reference}
          state={command.state}
          errorMessage={command.message}
          onConfirm={() => {
            command.submit(pending);
          }}
        />
      )}
    </Card>
  );
}

function Rename({
  orgId,
  current,
  ask,
}: {
  orgId: string;
  current: string;
  ask: (p: Pending) => void;
}) {
  const id = useId();
  const [name, setName] = useState(current);
  const valid = name.trim().length > 0 && name.length <= 255 && name !== current;
  return (
    <form
      className="flex flex-wrap items-end gap-2"
      onSubmit={(event) => {
        event.preventDefault();
        if (valid)
          ask({
            title: 'Rename organization?',
            description: `The organization will be called “${name.trim()}”.`,
            label: 'Rename',
            danger: false,
            run: (reference) =>
              client.patch('/api/v1/orgs/{org_id}', {
                path: { org_id: orgId },
                body: { name: name.trim() },
                headers: idempotency(reference),
              }),
          });
      }}
    >
      <div className="flex flex-col gap-1.5">
        <Label htmlFor={id}>Organization name</Label>
        <Input
          id={id}
          className="w-64"
          value={name}
          onChange={(event) => {
            setName(event.target.value);
          }}
        />
      </div>
      <Button type="submit" disabled={!valid}>
        Save name
      </Button>
    </form>
  );
}

function Members({
  orgId,
  subject,
  role,
  ask,
  queryKey,
}: {
  orgId: string;
  subject: string;
  role: Role | undefined;
  ask: (p: Pending) => void;
  queryKey: QueryKey;
}) {
  const [cursors, setCursors] = useState<string[]>(['']);
  const cursor = cursors.at(-1) ?? '';
  const members = useQuery({
    queryKey: [...queryKey, 'members', cursor],
    queryFn: ({ signal }) =>
      api.response<Member[]>(
        `/api/v1/orgs/${encodeURIComponent(orgId)}/members?limit=25${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ''}`,
        { signal },
      ),
  });
  const next = members.data?.headers.get('X-Next-Cursor');
  const manager = role === 'owner' || role === 'admin';
  const path = (userId: string) => ({ org_id: orgId, member_user_id: userId });
  return (
    <section aria-labelledby={`${orgId}-members`} className="flex flex-col gap-2">
      <h3 id={`${orgId}-members`} className="font-semibold">
        Members
      </h3>
      <div className="overflow-hidden rounded-md border border-border">
        {members.isError ? (
          <ErrorState error={members.error} onRetry={() => void members.refetch()} />
        ) : members.isPending ? (
          <LoadingRows rows={3} label="Loading members" />
        ) : members.data.data.length === 0 ? (
          <EmptyState title="No members" />
        ) : (
          <ul>
            {members.data.data.map((member) => {
              const editable =
                manager && member.role !== 'owner' && (role === 'owner' || member.role !== 'admin');
              return (
                <li
                  key={member.id}
                  className="flex flex-wrap items-center gap-3 border-b border-border px-3 py-2 last:border-b-0"
                >
                  <CopyText value={member.user_id} short />
                  {member.user_id === subject && <Badge tone="outline">you</Badge>}
                  {editable ? (
                    <NativeSelect
                      aria-label={`Role of ${member.user_id}`}
                      className="ml-auto h-control-sm w-28"
                      value={member.role}
                      onChange={(event) => {
                        const nextRole = event.target.value;
                        ask({
                          title: `Make this member ${nextRole}?`,
                          description:
                            'Their active sessions are revoked so the new role applies at once.',
                          label: 'Change role',
                          danger: false,
                          run: (reference) =>
                            client.patch('/api/v1/orgs/{org_id}/members/{member_user_id}', {
                              path: path(member.user_id),
                              headers: idempotency(reference),
                              body: { role: nextRole },
                            }),
                        });
                      }}
                    >
                      <option value="member">member</option>
                      <option value="admin">admin</option>
                    </NativeSelect>
                  ) : (
                    <Badge className="ml-auto">{member.role}</Badge>
                  )}
                  {editable && (
                    <Button
                      size="sm"
                      variant="danger-outline"
                      onClick={() => {
                        ask({
                          title: 'Remove this member?',
                          description: 'They lose access to the organization immediately.',
                          label: 'Remove member',
                          danger: true,
                          run: (reference) =>
                            client.delete('/api/v1/orgs/{org_id}/members/{member_user_id}', {
                              path: path(member.user_id),
                              headers: idempotency(reference),
                            }),
                        });
                      }}
                    >
                      Remove
                    </Button>
                  )}
                </li>
              );
            })}
          </ul>
        )}
      </div>
      <div className="flex gap-2">
        <Button
          size="sm"
          disabled={cursors.length < 2}
          onClick={() => {
            setCursors((list) => list.slice(0, -1));
          }}
        >
          Previous
        </Button>
        <Button
          size="sm"
          disabled={!next}
          onClick={() => {
            if (next) setCursors((list) => [...list, next]);
          }}
        >
          Next
        </Button>
      </div>
    </section>
  );
}

const EMAIL = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

function Invitations({
  orgId,
  role,
  ask,
  queryKey,
}: {
  orgId: string;
  role: Role;
  ask: (p: Pending) => void;
  queryKey: QueryKey;
}) {
  const emailId = useId();
  const roleId = useId();
  const [email, setEmail] = useState('');
  const [inviteRole, setInviteRole] = useState<'member' | 'admin'>('member');
  const invitations = useQuery({
    queryKey: [...queryKey, 'invitations'],
    queryFn: ({ signal }) =>
      client.get('/api/v1/orgs/{org_id}/invitations', { path: { org_id: orgId }, signal }),
  });
  return (
    <section aria-labelledby={`${orgId}-invites`} className="flex flex-col gap-2">
      <h3 id={`${orgId}-invites`} className="font-semibold">
        Invitations
      </h3>
      <form
        className="flex flex-wrap items-end gap-2"
        onSubmit={(event) => {
          event.preventDefault();
          if (!EMAIL.test(email)) return;
          ask({
            title: `Invite ${email}?`,
            description: `They will join as ${inviteRole} after accepting.`,
            label: 'Send invitation',
            danger: false,
            run: (reference) =>
              client.post('/api/v1/orgs/{org_id}/invitations', {
                path: { org_id: orgId },
                headers: idempotency(reference),
                body: { email, role: inviteRole },
              }),
          });
        }}
      >
        <div className="flex flex-col gap-1.5">
          <Label htmlFor={emailId}>Email</Label>
          <Input
            id={emailId}
            type="email"
            className="w-64"
            value={email}
            onChange={(event) => {
              setEmail(event.target.value);
            }}
          />
        </div>
        <div className="flex flex-col gap-1.5">
          <Label htmlFor={roleId}>Role</Label>
          <NativeSelect
            id={roleId}
            className="w-28"
            value={inviteRole}
            onChange={(event) => {
              setInviteRole(event.target.value === 'admin' ? 'admin' : 'member');
            }}
          >
            <option value="member">member</option>
            {role === 'owner' && <option value="admin">admin</option>}
          </NativeSelect>
        </div>
        <Button type="submit" disabled={!EMAIL.test(email)}>
          Invite
        </Button>
      </form>
      {invitations.isError && <ErrorState error={invitations.error} />}
      <ul>
        {invitations.data?.map((invite: Schema<'InvitationOut'>) => (
          <li key={invite.id} className="flex flex-wrap items-center gap-3 py-1.5">
            {invite.email}
            <Badge>{invite.role}</Badge>
            <Button
              size="sm"
              variant="ghost"
              className="ml-auto"
              onClick={() => {
                ask({
                  title: 'Revoke this invitation?',
                  description: `${invite.email} will no longer be able to join with it.`,
                  label: 'Revoke',
                  danger: true,
                  run: (reference) =>
                    client.delete('/api/v1/orgs/{org_id}/invitations/{invitation_id}', {
                      path: { org_id: orgId, invitation_id: invite.id },
                      headers: idempotency(reference),
                    }),
                });
              }}
            >
              Revoke
            </Button>
          </li>
        ))}
      </ul>
    </section>
  );
}
