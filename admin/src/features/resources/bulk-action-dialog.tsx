import { useState } from 'react';
import type { QueryKey } from '@tanstack/react-query';
import { api } from '@/api/client';
import { bulkRunner, type BulkResult } from './bulk-runner';
import { Button } from '@/components/ui/button';
import { ConfirmDialog } from '@/components/patterns/confirm-dialog';
import { useCommand } from '@/hooks/use-command';
import type { Action, AdminResource } from '@/schema/types';
import { endpoint, payload } from '@/schema/values';
import { FormField } from './form-field';
import { fieldErrors } from './form-errors';
import { scopeHeaders, type ResourceRecord } from './data';

/**
 * Runs one declared action for each selected record, sequentially, each with
 * its own Idempotency-Key (`reference:id`). A retry after a partial failure
 * skips confirmed successes and preserves the failed selection for correction.
 */
export function BulkActionDialog({
  action,
  resource,
  records,
  scope,
  invalidate,
  onDone,
}: {
  action: Action;
  resource: AdminResource;
  records: ResourceRecord[];
  scope: string;
  invalidate: readonly QueryKey[];
  onDone: (failedIds: string[]) => void;
}) {
  const [open, setOpen] = useState(false);
  const [values, setValues] = useState<Record<string, unknown>>({});
  const [runBulk] = useState(bulkRunner);
  const [result, setResult] = useState<BulkResult | null>(null);
  const command = useCommand<Record<string, unknown>, BulkResult>({
    label: `${action.label} · ${records.length} ${resource.label}`,
    run: async (input, reference) => {
      const body = action.has_body ? JSON.stringify(payload(action, input, true)) : undefined;
      return runBulk(
        reference,
        records.map((record) => String(record.id)),
        (id) =>
          api.request(endpoint(action, resource, id, scope), {
            method: action.method,
            headers: { ...scopeHeaders(resource, scope), 'Idempotency-Key': `${reference}:${id}` },
            ...(body === undefined ? {} : { body }),
          }),
      );
    },
    success: (value) =>
      `${action.label}: ${value.applied} applied, ${value.failures.length} refused`,
    invalidate,
    onSuccess: (value) => {
      setResult(value);
      if (!value.failures.length) {
        setOpen(false);
        setValues({});
      }
      onDone(value.failures.map((failure) => failure.id));
    },
  });
  const errors = command.state === 'error' ? fieldErrors(command.error) : {};
  const noun = records.length === 1 ? 'record' : 'records';
  return (
    <>
      <Button
        size="sm"
        variant={action.tone === 'danger' ? 'danger-outline' : 'secondary'}
        onClick={() => {
          command.dismiss();
          setOpen(true);
        }}
      >
        {action.label}…
      </Button>
      {result?.failures.length ? (
        <p role="status" className="text-fg-muted">
          {result.applied} applied;{' '}
          {result.failures.map((failure) => `${failure.id}: ${failure.message}`).join('; ')}
        </p>
      ) : null}
      <ConfirmDialog
        open={open}
        onOpenChange={setOpen}
        title={`${action.label} — ${records.length} ${noun}`}
        description={`${action.confirm} Each ${resource.label.toLowerCase()} record is processed as its own audited command.`}
        confirmLabel={`${action.label} (${records.length})`}
        tone={action.tone}
        typedConfirmation={action.tone === 'danger' ? `${action.name} ${records.length}` : null}
        reference={command.reference}
        state={command.state}
        errorMessage={command.message}
        onConfirm={() => {
          command.submit(values);
        }}
      >
        {result?.failures.length ? (
          <p role="alert" className="text-danger">
            {result.applied} applied;{' '}
            {result.failures.map((failure) => `${failure.id}: ${failure.message}`).join('; ')}
          </p>
        ) : null}
        {action.inputs.map((field) => (
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
