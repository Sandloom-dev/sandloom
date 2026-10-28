import type { ReactNode } from 'react';
import { CircleAlert, CircleHelp, Inbox, type LucideIcon } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Skeleton } from '@/components/ui/skeleton';
import { errorMessage, errorStatus } from '@/api/errors';
import { cn } from '@/lib/cn';

export function EmptyState({
  icon: Icon = Inbox,
  title,
  description,
  action,
  className,
}: {
  icon?: LucideIcon;
  title: string;
  description?: ReactNode;
  action?: ReactNode;
  className?: string;
}) {
  return (
    <div
      className={cn('flex flex-col items-center justify-center gap-2 p-10 text-center', className)}
    >
      <span className="mb-1 grid size-11 place-items-center rounded-lg bg-surface-muted text-fg-muted">
        <Icon aria-hidden className="size-5" />
      </span>
      <p className="text-lg font-semibold">{title}</p>
      {description && <p className="max-w-prose text-fg-muted">{description}</p>}
      {action && <div className="mt-2">{action}</div>}
    </div>
  );
}

const STATUS_TITLES: Record<number, string> = {
  401: 'Your session ended',
  403: 'You do not have access to this',
  404: 'Not found',
  409: 'The request conflicts with the current state',
  422: 'The request was rejected',
  501: 'Not available in this deployment',
  503: 'A service is unavailable',
};

/** A failed READ. Shows nothing stale; offers a retry when it makes sense. */
export function ErrorState({
  error,
  onRetry,
  className,
}: {
  error: unknown;
  onRetry?: () => void;
  className?: string;
}) {
  const status = errorStatus(error);
  const title =
    (status === undefined ? undefined : STATUS_TITLES[status]) ?? 'Could not load this data';
  return (
    <div
      role="alert"
      className={cn(
        'm-4 flex flex-col gap-2 rounded-lg border border-danger bg-danger-muted p-4',
        className,
      )}
    >
      <p className="flex items-center gap-2 font-semibold">
        <CircleAlert aria-hidden className="size-4 text-danger" />
        {title}
        {status !== undefined && <span className="font-mono text-xs text-fg-muted">{status}</span>}
      </p>
      <p className="text-fg-muted">{errorMessage(error)}</p>
      {onRetry && status !== 403 && status !== 404 && (
        <div>
          <Button size="sm" onClick={onRetry}>
            Try again
          </Button>
        </div>
      )}
    </div>
  );
}

/** A WRITE whose response was lost. Retrying with the same reference is safe. */
export function UnknownOutcome({
  what,
  reference,
  onRetry,
  inspect,
}: {
  what: string;
  reference: string;
  onRetry: () => void;
  inspect?: ReactNode;
}) {
  return (
    <div
      role="alert"
      className="flex flex-col gap-2 rounded-lg border border-warning bg-warning-muted p-4"
    >
      <p className="flex items-center gap-2 font-semibold">
        <CircleHelp aria-hidden className="size-4 text-warning" />
        We could not confirm whether {what}
      </p>
      <p className="text-fg-muted">
        The response was lost. Check the current state before doing anything else. Retrying with the
        same reference is safe — it is never applied twice.
      </p>
      <p className="font-mono text-xs break-all text-fg-subtle">Reference {reference}</p>
      <div className="flex flex-wrap gap-2">
        {inspect}
        <Button size="sm" onClick={onRetry}>
          Retry with same reference
        </Button>
      </div>
    </div>
  );
}

export function LoadingRows({ rows = 5, label = 'Loading' }: { rows?: number; label?: string }) {
  return (
    <div aria-busy="true" aria-label={label} className="flex flex-col">
      {Array.from({ length: rows }, (_, index) => (
        <div
          key={index}
          className="flex items-center gap-3 border-b border-border px-4 py-3 last:border-b-0"
        >
          <Skeleton className="size-7 rounded-full" />
          <span className="flex flex-1 flex-col gap-1.5">
            <Skeleton className="h-2.5 w-2/5" />
            <Skeleton className="h-2 w-3/5" />
          </span>
          <Skeleton className="h-4 w-16" />
        </div>
      ))}
    </div>
  );
}
