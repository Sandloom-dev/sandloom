import { useState } from 'react';
import type { QueryKey } from '@tanstack/react-query';
import { Button, type ButtonProps } from '@/components/ui/button';
import { ConfirmDialog } from '@/components/patterns/confirm-dialog';
import { useCommand } from '@/hooks/use-command';
import type { Field } from '@/schema/types';
import { validateValue } from '@/schema/values';
import { FormField } from '@/features/resources/form-field';
import type { CommandResult } from './api';

const REASON: Field = {
  name: 'reason',
  label: 'Reason',
  type: 'string',
  required: true,
  nullable: false,
  minLength: 3,
  maxLength: 500,
  description: 'Stored in the audit log. No secrets or personal data.',
};

/**
 * An audited operations command that needs only a reason (requeue, replay).
 * The operation reference is the Idempotency-Key; see useCommand.
 */
export function ReasonCommand({
  trigger,
  variant = 'secondary',
  disabled = false,
  title,
  description,
  confirmLabel,
  run,
  invalidate,
  onDone,
}: {
  trigger: string;
  variant?: ButtonProps['variant'];
  disabled?: boolean;
  title: string;
  description: string;
  confirmLabel: string;
  run: (reason: string, reference: string) => Promise<CommandResult>;
  invalidate: readonly QueryKey[];
  onDone?: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [reason, setReason] = useState('');
  const command = useCommand<string, CommandResult>({
    label: title,
    run,
    success: (result) => `${confirmLabel}: ${result.applied} of ${result.requested} applied`,
    invalidate,
    onSuccess: () => {
      setOpen(false);
      setReason('');
      onDone?.();
    },
  });
  const error = reason ? validateValue(REASON, reason) : undefined;
  return (
    <>
      <Button
        size="sm"
        variant={variant}
        disabled={disabled}
        onClick={() => {
          command.dismiss();
          setOpen(true);
        }}
      >
        {trigger}
      </Button>
      <ConfirmDialog
        open={open}
        onOpenChange={setOpen}
        title={title}
        description={description}
        confirmLabel={confirmLabel}
        reference={command.reference}
        state={command.state}
        errorMessage={command.message}
        canConfirm={Boolean(reason) && !error}
        onConfirm={() => {
          command.submit(reason.trim());
        }}
      >
        <FormField
          field={REASON}
          value={reason}
          error={error}
          onChange={(value) => {
            setReason(typeof value === 'string' ? value : '');
          }}
        />
      </ConfirmDialog>
    </>
  );
}
