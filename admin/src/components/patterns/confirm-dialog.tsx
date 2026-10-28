import { useId, useState, type SubmitEvent, type ReactNode } from 'react';
import { CircleAlert, ShieldAlert } from 'lucide-react';
import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogFooter,
  DialogHeader,
} from '@/components/ui/dialog';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { CopyText } from './copy-text';
import { UnknownOutcome } from './states';
import type { CommandState } from '@/hooks/use-command';

export interface ConfirmDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: string;
  /** What will happen, in the operator's words. */
  description: ReactNode;
  confirmLabel: string;
  tone?: 'default' | 'danger';
  /** When set, the operator must retype this exact value to enable the action. */
  typedConfirmation?: string | null;
  /** Operation reference (Idempotency-Key) shown for support and safe retries. */
  reference: string;
  state: CommandState;
  errorMessage?: string | null;
  /** Called with the form valid; the dialog stays open until the command settles. */
  onConfirm: () => void;
  /** Disable confirm while extra inputs are invalid. */
  canConfirm?: boolean;
  /** A link/button to inspect state when the outcome is unknown. */
  inspect?: ReactNode;
  children?: ReactNode;
}

/**
 * The single confirmation surface for every command. Accessible (alert-style
 * Radix dialog, labelled, focus-trapped), never closes while the request is in
 * flight, renders server errors inline and the unknown-outcome guidance.
 */
export function ConfirmDialog({
  open,
  onOpenChange,
  title,
  description,
  confirmLabel,
  tone = 'default',
  typedConfirmation = null,
  reference,
  state,
  errorMessage = null,
  onConfirm,
  canConfirm = true,
  inspect,
  children,
}: ConfirmDialogProps) {
  const typedId = useId();
  const [typed, setTyped] = useState('');
  const pending = state === 'pending';
  const typedOk = typedConfirmation === null || typed === typedConfirmation;
  const danger = tone === 'danger';
  const submit = (event: SubmitEvent) => {
    event.preventDefault();
    if (!pending && state !== 'unknown' && typedOk && canConfirm) onConfirm();
  };
  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (pending) return;
        if (!next) setTyped('');
        onOpenChange(next);
      }}
    >
      <DialogContent
        role="alertdialog"
        hideClose={pending}
        onEscapeKeyDown={(event) => {
          if (pending) event.preventDefault();
        }}
        onInteractOutside={(event) => {
          event.preventDefault();
        }}
      >
        <form onSubmit={submit} noValidate>
          <DialogHeader
            icon={
              <span
                className={
                  danger
                    ? 'grid size-9 shrink-0 place-items-center rounded-full bg-danger-muted text-danger'
                    : 'grid size-9 shrink-0 place-items-center rounded-full bg-accent-muted text-accent'
                }
              >
                <ShieldAlert aria-hidden className="size-4" />
              </span>
            }
            title={title}
            description={description}
          />
          <DialogBody>
            <fieldset disabled={pending || state === 'unknown'} className="flex flex-col gap-3">
              {children}
            </fieldset>
            {typedConfirmation !== null && (
              <div className="flex flex-col gap-1.5">
                <Label htmlFor={typedId}>
                  Type <span className="font-mono">{typedConfirmation}</span> to confirm
                </Label>
                <Input
                  id={typedId}
                  value={typed}
                  autoComplete="off"
                  spellCheck={false}
                  onChange={(event) => {
                    setTyped(event.target.value);
                  }}
                />
              </div>
            )}
            {state === 'error' && errorMessage && (
              <p
                role="alert"
                className="flex items-start gap-2 rounded-md bg-danger-muted p-3 text-danger"
              >
                <CircleAlert aria-hidden className="mt-0.5 size-4 shrink-0" />
                {errorMessage}
              </p>
            )}
            {state === 'unknown' && (
              <UnknownOutcome
                what="this was applied"
                reference={reference}
                onRetry={onConfirm}
                inspect={inspect}
              />
            )}
            <p className="flex flex-wrap items-center gap-2 text-xs text-fg-subtle">
              Operation reference <CopyText value={reference} className="text-fg-muted" />
            </p>
          </DialogBody>
          <DialogFooter>
            <Button
              disabled={pending}
              onClick={() => {
                setTyped('');
                onOpenChange(false);
              }}
            >
              {state === 'unknown' ? 'Close' : 'Cancel'}
            </Button>
            {state !== 'unknown' && (
              <Button
                type="submit"
                variant={danger ? 'danger' : 'primary'}
                loading={pending}
                disabled={!typedOk || !canConfirm}
              >
                {confirmLabel}
              </Button>
            )}
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
