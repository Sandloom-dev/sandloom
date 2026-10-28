import { useState, type ReactNode } from 'react';
import type { QueryKey } from '@tanstack/react-query';
import { api } from '@/api/client';
import { Button, type ButtonProps } from '@/components/ui/button';
import { ConfirmDialog } from '@/components/patterns/confirm-dialog';
import { useCommand } from '@/hooks/use-command';
import { allowed, type Action, type AdminResource } from '@/schema/types';
import { endpoint, payload } from '@/schema/values';
import { useSession } from '@/app/session';
import { FormField } from './form-field';
import { fieldErrors } from './form-errors';
import { scopeHeaders, type ResourceRecord } from './data';

export interface ActionDialogProps {
  action: Action;
  resource: AdminResource;
  record: ResourceRecord;
  scope: string;
  invalidate: readonly QueryKey[];
  /** Button appearance; defaults follow the action's tone. */
  variant?: ButtonProps['variant'];
  icon?: ReactNode;
  inspect?: ReactNode;
}

/**
 * A declared record command: generated inputs, the shared confirmation
 * surface, Idempotency-Key = operation reference, typed confirmation when the
 * manifest asks for it. Hidden when the operator's roles do not allow it.
 */
export function ActionDialog({
  action,
  resource,
  record,
  scope,
  invalidate,
  variant,
  icon,
  inspect,
}: ActionDialogProps) {
  const { session } = useSession();
  const [open, setOpen] = useState(false);
  const [values, setValues] = useState<Record<string, unknown>>(() => defaults(action));
  const command = useCommand<Record<string, unknown>, unknown>({
    label: `${action.label} · ${resource.label} · ${record.id}`,
    run: (input, reference) =>
      api.request(endpoint(action, resource, record.id, scope), {
        method: action.method,
        headers: { ...scopeHeaders(resource, scope), 'Idempotency-Key': reference },
        ...(action.has_body
          ? {
              body: JSON.stringify(
                payload(
                  action,
                  action.idempotency_field
                    ? { ...input, [action.idempotency_field]: reference }
                    : input,
                  true,
                ),
              ),
            }
          : {}),
      }),
    success: `${action.label} — completed`,
    invalidate,
    onSuccess: () => {
      setOpen(false);
      setValues(defaults(action));
    },
  });
  if (!session || !allowed(action.access ?? resource.access, session.roles)) return null;
  const errors = command.state === 'error' ? fieldErrors(command.error) : {};
  const typed = action.typed_confirmation ? record[action.typed_confirmation] : null;
  const visibleInputs = action.inputs.filter((field) => field.name !== action.idempotency_field);
  return (
    <>
      <Button
        variant={variant ?? (action.tone === 'danger' ? 'danger-outline' : 'secondary')}
        onClick={() => {
          command.dismiss();
          setOpen(true);
        }}
      >
        {icon}
        {action.label}
      </Button>
      <ConfirmDialog
        open={open}
        onOpenChange={setOpen}
        title={action.label}
        description={action.confirm}
        confirmLabel={action.label}
        tone={action.tone}
        typedConfirmation={typeof typed === 'string' ? typed : null}
        reference={command.reference}
        state={command.state}
        errorMessage={command.message}
        inspect={inspect}
        onConfirm={() => {
          command.submit(values);
        }}
      >
        {visibleInputs.map((field) => (
          <FormField
            key={field.name}
            field={field}
            value={values[field.name]}
            error={errors[field.name]}
            onChange={(value) => {
              setValues((current) => ({ ...current, [field.name]: value }));
            }}
          />
        ))}
      </ConfirmDialog>
    </>
  );
}

function defaults(action: Action): Record<string, unknown> {
  return Object.fromEntries(
    action.inputs.filter((field) => field.default !== undefined).map((f) => [f.name, f.default]),
  );
}
