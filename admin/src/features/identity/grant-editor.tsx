import { useId, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Plus, X } from 'lucide-react';
import { api } from '@/api/client';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { NativeSelect } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Skeleton } from '@/components/ui/skeleton';
import { ConfirmDialog } from '@/components/patterns/confirm-dialog';
import { ErrorState } from '@/components/patterns/states';
import { useCommand } from '@/hooks/use-command';
import { useSubject } from '@/app/session';

interface Grant {
  id: string;
  name: string;
}
interface Change {
  grant: Grant;
  remove: boolean;
}

export interface GrantEditorProps {
  title: string;
  /** Lists current grants; `field` selects them inside a wrapper object. */
  currentUrl: string;
  field?: 'permissions';
  optionsUrl: string;
  /** `${commandUrl}/{grantId}`: POST assigns, DELETE revokes. */
  commandUrl: string;
  note: string;
}

/** Assign/revoke many-to-many grants (roles, permissions) with confirmation. */
export function GrantEditor({
  title,
  currentUrl,
  field,
  optionsUrl,
  commandUrl,
  note,
}: GrantEditorProps) {
  const subject = useSubject();
  const selectId = useId();
  const currentKey = ['grants', subject, currentUrl] as const;
  const [selected, setSelected] = useState('');
  const [pending, setPending] = useState<Change | null>(null);
  const assigned = useQuery({
    queryKey: currentKey,
    queryFn: async ({ signal }) => {
      const value = await api.request<Grant[] | { permissions: Grant[] }>(currentUrl, { signal });
      return Array.isArray(value) ? value : field ? value.permissions : [];
    },
  });
  const options = useQuery({
    queryKey: ['grant-options', subject, optionsUrl],
    queryFn: ({ signal }) => api.request<Grant[]>(optionsUrl, { signal }),
  });
  const command = useCommand<Change, unknown>({
    label: `Change ${title}`,
    run: ({ grant, remove }, reference) =>
      api.request(`${commandUrl}/${encodeURIComponent(grant.id)}`, {
        method: remove ? 'DELETE' : 'POST',
        headers: { 'Idempotency-Key': reference },
      }),
    success: (_result, { remove }) => (remove ? 'Access revoked' : 'Access assigned'),
    invalidate: [currentKey],
    onSuccess: () => {
      setPending(null);
      setSelected('');
    },
  });
  const available = (options.data ?? []).filter(
    (option) => !assigned.data?.some((grant) => grant.id === option.id),
  );
  return (
    <Card>
      <CardHeader>
        <CardTitle>{title}</CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        {assigned.isError || options.isError ? (
          <ErrorState
            className="m-0"
            error={assigned.error ?? options.error}
            onRetry={() => {
              void assigned.refetch();
              void options.refetch();
            }}
          />
        ) : assigned.isPending ? (
          <Skeleton className="h-6 w-1/2" />
        ) : (
          <ul aria-label={title} className="flex flex-wrap gap-1.5">
            {assigned.data.length === 0 && <li className="text-fg-subtle">None assigned</li>}
            {assigned.data.map((grant) => (
              <li
                key={grant.id}
                className="inline-flex h-control-sm items-center gap-1 rounded-sm border border-border pr-0.5 pl-2"
              >
                {grant.name}
                <button
                  type="button"
                  aria-label={`Revoke ${grant.name}`}
                  className="grid size-6 place-items-center rounded-sm text-fg-subtle hover:bg-surface-hover hover:text-fg"
                  onClick={() => {
                    // A different command: new reference (an unknown outcome keeps its own)
                    if (!command.reset()) return;
                    setPending({ grant, remove: true });
                  }}
                >
                  <X aria-hidden className="size-3.5" />
                </button>
              </li>
            ))}
          </ul>
        )}
        <div className="flex flex-wrap items-end gap-2">
          <div className="flex flex-col gap-1.5">
            <Label htmlFor={selectId}>Add</Label>
            <NativeSelect
              id={selectId}
              className="w-56"
              value={selected}
              disabled={!assigned.isSuccess || available.length === 0}
              onChange={(event) => {
                setSelected(event.target.value);
              }}
            >
              <option value="">{available.length ? 'Select…' : 'Nothing to add'}</option>
              {available.map((option) => (
                <option key={option.id} value={option.id}>
                  {option.name}
                </option>
              ))}
            </NativeSelect>
          </div>
          <Button
            disabled={!selected}
            onClick={() => {
              const grant = available.find((option) => option.id === selected);
              if (!grant) return;
              // A different command: new reference (an unknown outcome keeps its own)
              if (!command.reset()) return;
              setPending({ grant, remove: false });
            }}
          >
            <Plus aria-hidden />
            Assign
          </Button>
        </div>
        <p className="text-xs text-fg-subtle">{note}</p>
      </CardContent>
      {pending && (
        <ConfirmDialog
          open
          onOpenChange={(open) => {
            if (!open) setPending(null);
          }}
          title={`${pending.remove ? 'Revoke' : 'Assign'} “${pending.grant.name}”?`}
          description={note}
          confirmLabel={pending.remove ? 'Revoke' : 'Assign'}
          tone={pending.remove ? 'danger' : 'default'}
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
