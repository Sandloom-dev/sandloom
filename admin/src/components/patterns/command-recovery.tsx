import { useEffect, useSyncExternalStore } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { commandRecovery } from '@/hooks/command-recovery';
import { Button } from '@/components/ui/button';
import { CopyText } from './copy-text';

/** Survives route/dialog closure, but is discarded with the session QueryClient. */
export function CommandRecovery() {
  const store = commandRecovery(useQueryClient());
  const rows = useSyncExternalStore(store.subscribe, store.snapshot);
  useEffect(() => {
    if (!rows.length) return;
    const warn = (event: BeforeUnloadEvent) => {
      event.preventDefault();
    };
    window.addEventListener('beforeunload', warn);
    return () => {
      window.removeEventListener('beforeunload', warn);
    };
  }, [rows.length]);
  if (!rows.length) return null;
  return (
    <section
      aria-label="Operation recovery"
      className="flex flex-col gap-2 rounded-md border border-warning bg-warning-muted p-3"
    >
      {rows.map((row) => (
        <div key={row.reference} className="flex flex-wrap items-center gap-2">
          <strong>{row.label}</strong>
          <span>
            {row.pending
              ? 'Change in progress.'
              : 'Outcome unknown. Retry the original operation before making another change.'}
          </span>
          <CopyText value={row.reference} />
          {!row.pending && (
            <Button size="sm" onClick={row.retry}>
              Retry original operation
            </Button>
          )}
        </div>
      ))}
    </section>
  );
}
