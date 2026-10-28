import { Link } from 'react-router-dom';
import type { AdminResource } from '@/schema/types';
import type { ResourceRecord } from './data';
import { useQueries } from '@tanstack/react-query';
import { api } from '@/api/client';
import { Card, CardHeader, CardTitle } from '@/components/ui/card';
import { Timeline, type TimelineItem } from '@/components/patterns/timeline';
import { EmptyState, ErrorState, LoadingRows } from '@/components/patterns/states';
import { useConfig } from '@/app/config-context';
import { useSubject } from '@/app/session';
import { shortId } from '@/lib/format';
import { useActorNames } from '@/features/platform/actors';
import type { AuditRow } from '@/features/platform/api';

export const auditKeys = {
  all: (subject: string) => ['audit', subject] as const,
  record: (subject: string, source: string, type: string, id: string) =>
    ['audit', subject, source, type, id] as const,
};

function tone(action: string): TimelineItem['tone'] {
  if (/(blocked|revoked|deleted|removed|deactivated|failed)/.test(action)) return 'danger';
  if (/(unblocked|created|assigned|added|registered|activated)/.test(action)) return 'success';
  return 'accent';
}

/**
 * Audit trail of one record, merged from every audit source the console can
 * read (one per module database; a monolith's sources share a journal, so
 * rows are de-duplicated by id).
 */
export function ActivityCard({
  resource,
  record,
}: {
  resource: AdminResource;
  record: ResourceRecord;
}) {
  const binding = resource.audit;
  const rawId = binding ? record[binding.id_field] : null;
  const resourceId = typeof rawId === 'string' || typeof rawId === 'number' ? String(rawId) : '';
  const resourceType = binding?.resource_type ?? '';
  const subject = useSubject();
  const { manifest } = useConfig();
  const sources = manifest.resources.filter(
    (resource) => resource.module === 'core' && resource.name.startsWith('audit_'),
  );
  const results = useQueries({
    queries: sources.map((source) => ({
      queryKey: auditKeys.record(subject, source.name, resourceType, resourceId),
      enabled: Boolean(binding && resourceId),
      queryFn: async ({ signal }: { signal: AbortSignal }) => {
        const query = new URLSearchParams({
          resource_type: resourceType,
          resource_id: resourceId,
          limit: '50',
        });
        const response = await api.response<AuditRow[]>(`${source.list.path}?${query}`, { signal });
        return { rows: response.data, more: Boolean(response.headers.get('X-Next-Cursor')) };
      },
    })),
  });
  const failed = results.find((result) => result.isError);
  const actorName = useActorNames(
    results.flatMap((result) => (result.data?.rows ?? []).map((row) => row.actor_id)),
  );
  const rows = new Map<string, AuditRow>();
  for (const result of results) for (const row of result.data?.rows ?? []) rows.set(row.id, row);
  const items: TimelineItem[] = [...rows.values()]
    .sort((a, b) => b.occurred_at.localeCompare(a.occurred_at))
    .map((row) => ({
      id: row.id,
      at: row.occurred_at,
      tone: tone(row.action),
      meta: [row.action, row.request_id ? `req ${shortId(row.request_id)}` : null]
        .filter(Boolean)
        .join(' · '),
      title: (
        <>
          <span className="font-semibold">
            {row.actor_id ? actorName(row.actor_id) : row.actor_type}
          </span>{' '}
          {row.action.split('.').slice(1).join(' ').replace(/_/g, ' ')}
        </>
      ),
    }));
  return (
    <Card>
      <CardHeader>
        <CardTitle>Activity</CardTitle>
        <span className="ml-auto text-xs text-fg-subtle">Audit journal</span>
      </CardHeader>
      {!binding || !resourceId ? (
        <EmptyState
          title="Record activity is not configured"
          description="This resource has no explicit audit identity. Use the audit explorer for deployment-wide activity."
        />
      ) : sources.length === 0 ? (
        <EmptyState title="No audit source" description="This deployment exposes no audit API." />
      ) : failed ? (
        <ErrorState error={failed.error} onRetry={() => void failed.refetch()} />
      ) : results.some((result) => result.isPending) ? (
        <LoadingRows rows={3} label="Loading activity" />
      ) : items.length === 0 ? (
        <EmptyState title="No recorded activity" />
      ) : (
        <div className="p-4">
          <Timeline items={items} />
        </div>
      )}
      {binding && resourceId && sources.length > 0 && (
        <div className="flex flex-wrap gap-3 p-4 text-sm">
          {sources.map((source, index) => (
            <Link
              key={source.name}
              className="text-accent hover:underline"
              to={`/platform/audit?${new URLSearchParams({ source: source.name.replace(/^audit_/, ''), range: 'all', 'f.resource_type': resourceType, 'f.resource_id': resourceId })}`}
            >
              View {results[index]?.data?.more ? 'all activity' : 'audit explorer'} ·{' '}
              {source.name.replace(/^audit_/, '')}
            </Link>
          ))}
        </div>
      )}
    </Card>
  );
}
