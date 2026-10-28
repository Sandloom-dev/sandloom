import { Link } from 'react-router-dom';
import { useQueries, useQuery } from '@tanstack/react-query';
import {
  ArrowRight,
  CircleCheck,
  Inbox,
  TriangleAlert,
  Webhook,
  Zap,
  type LucideIcon,
} from 'lucide-react';
import { api } from '@/api/client';
import { Badge } from '@/components/ui/badge';
import { Card, CardHeader, CardTitle } from '@/components/ui/card';
import { Skeleton } from '@/components/ui/skeleton';
import { PageHeader } from '@/components/patterns/page-header';
import { Timeline, type TimelineItem } from '@/components/patterns/timeline';
import { EmptyState, ErrorState, LoadingRows } from '@/components/patterns/states';
import { useConfig } from '@/app/config-context';
import { useSession, useSubject } from '@/app/session';
import { fetchListPage } from '@/features/resources/data';
import { listHref } from '@/features/resources/routes';
import { sourceName, usePlatformSources, useServiceHealth } from '@/features/platform/sources';
import { platformKeys, platformPath, type DeadLetter } from '@/features/platform/api';
import { allowed } from '@/schema/types';
import { shortId } from '@/lib/format';
import { useActorNames } from '@/features/platform/actors';

interface AttentionCheck {
  resource: string;
  filters: Record<string, string>;
  label: (count: string) => string;
  icon: LucideIcon;
}

/** Live filters over admitted resources; nothing here is a stored metric. */
const CHECKS: AttentionCheck[] = [
  {
    resource: 'billing_webhooks',
    filters: { status: 'failed' },
    label: (n) => `${n} webhook event${n === '1' ? '' : 's'} failed processing`,
    icon: Webhook,
  },
  {
    resource: 'billing_intents',
    filters: { followup: 'true' },
    label: (n) => `${n} operation intent${n === '1' ? '' : 's'} awaiting follow-up`,
    icon: Zap,
  },
];

function HealthTiles() {
  const services = useServiceHealth();
  if (services.length === 0) return null;
  return (
    <section
      aria-label="Service health"
      className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-4"
    >
      {services.map(({ source, result }) => {
        const rows = result?.data ?? [];
        const bad = rows.filter((row) => row.status !== 'ok');
        return (
          <Link
            key={source.name}
            to="/platform/health"
            className="flex flex-col gap-2 rounded-lg border border-border bg-surface p-4 shadow-sm hover:border-border-strong"
          >
            <span className="flex items-center gap-2 text-fg-muted">
              {sourceName(source)}
              <span className="sr-only">service</span>
            </span>
            {!result || result.isPending ? (
              <Skeleton className="h-7 w-24" />
            ) : result.isError ? (
              <Badge tone="danger" dot className="w-fit">
                Unreachable
              </Badge>
            ) : bad.length === 0 ? (
              <span className="flex items-center gap-2 text-lg font-semibold">
                <CircleCheck aria-hidden className="size-5 text-success" />
                Healthy
              </span>
            ) : (
              <span className="flex items-center gap-2 text-lg font-semibold">
                <TriangleAlert aria-hidden className="size-5 text-warning" />
                {bad.length} degraded
              </span>
            )}
            <span className="text-xs text-fg-subtle">
              {rows.length ? `${rows.length} checks` : 'Live sample'}
            </span>
          </Link>
        );
      })}
    </section>
  );
}

function NeedsAttention() {
  const subject = useSubject();
  const { session } = useSession();
  const { manifest } = useConfig();
  const checks = CHECKS.flatMap((check) => {
    const resource = manifest.resources.find((r) => r.name === check.resource);
    return resource && session && allowed(resource.access, session.roles) && resource.filters.length
      ? [{ check, resource }]
      : [];
  });
  const results = useQueries({
    queries: checks.map(({ check, resource }) => ({
      queryKey: ['dashboard', subject, check.resource, check.filters],
      queryFn: ({ signal }: { signal: AbortSignal }) =>
        fetchListPage(
          api,
          resource,
          {
            page: 1,
            pageSize: Math.min(100, resource.pagination.max_page_size),
            filters: check.filters,
            sort: null,
            scope: '',
          },
          new Map(),
          signal,
        ),
      refetchInterval: 60_000,
    })),
  });
  const items = checks.flatMap(({ check }, index) => {
    const result = results[index];
    if (!result?.data || result.data.rows.length === 0) return [];
    const count = `${result.data.rows.length}${result.data.hasNextPage ? '+' : ''}`;
    return [{ check, count }];
  });
  const sources = usePlatformSources('services').map(sourceName);
  const dead = useQueries({
    queries: sources.map((source) => ({
      queryKey: platformKeys.view(subject, source, 'outbox/dead', { limit: 200 }),
      queryFn: ({ signal }: { signal: AbortSignal }) =>
        api.request<DeadLetter[]>(platformPath(source, 'outbox/dead', { limit: 200 }), { signal }),
      refetchInterval: 60_000,
    })),
  });
  const deadItems = sources.flatMap((source, index) => {
    const count = dead[index]?.data?.length ?? 0;
    return count > 0 ? [{ source, count }] : [];
  });
  const pending = results.some((r) => r.isPending) || dead.some((r) => r.isPending);
  const failed = results.find((r) => r.isError) ?? dead.find((r) => r.isError);
  return (
    <Card className="flex-1 basis-96">
      <CardHeader>
        <CardTitle>Needs attention</CardTitle>
        <span className="ml-auto text-xs text-fg-subtle">From live filters</span>
      </CardHeader>
      {failed ? (
        <ErrorState error={failed.error} onRetry={() => void failed.refetch()} />
      ) : pending ? (
        <LoadingRows rows={2} label="Checking" />
      ) : items.length === 0 && deadItems.length === 0 ? (
        <EmptyState
          icon={CircleCheck}
          title="All clear"
          description="No dead letters, failed webhooks or intents awaiting follow-up."
        />
      ) : (
        <ul>
          {deadItems.map(({ source, count }) => (
            <li key={`dead-${source}`} className="border-b border-border last:border-b-0">
              <Link
                to={`/platform/events?source=${encodeURIComponent(source)}`}
                className="flex items-center gap-3 px-4 py-3 hover:bg-surface-muted"
              >
                <span className="grid size-8 place-items-center rounded-md bg-danger-muted text-danger">
                  <Inbox aria-hidden className="size-4" />
                </span>
                <span className="flex flex-1 flex-col">
                  <span className="font-medium">
                    {count}
                    {count >= 200 ? '+' : ''} outbox event{count === 1 ? '' : 's'} dead-lettered
                  </span>
                  <span className="text-xs text-fg-subtle">{source}</span>
                </span>
                <ArrowRight aria-hidden className="size-4 text-fg-subtle" />
              </Link>
            </li>
          ))}
          {items.map(({ check, count }) => {
            const Icon = check.icon;
            return (
              <li key={check.resource} className="border-b border-border last:border-b-0">
                <Link
                  to={listHref(check.resource, check.filters)}
                  className="flex items-center gap-3 px-4 py-3 hover:bg-surface-muted"
                >
                  <span className="grid size-8 place-items-center rounded-md bg-warning-muted text-warning">
                    <Icon aria-hidden className="size-4" />
                  </span>
                  <span className="flex-1 font-medium">{check.label(count)}</span>
                  <ArrowRight aria-hidden className="size-4 text-fg-subtle" />
                </Link>
              </li>
            );
          })}
        </ul>
      )}
    </Card>
  );
}

interface AuditRow {
  id: string;
  occurred_at: string;
  actor_id: string | null;
  actor_type: string;
  action: string;
  resource_id: string | null;
}

function RecentActivity() {
  const subject = useSubject();
  const [source] = usePlatformSources('audit');
  const audit = useQuery({
    queryKey: ['dashboard', subject, 'audit', source?.name],
    enabled: Boolean(source),
    queryFn: ({ signal }) =>
      api.request<AuditRow[]>(`${source?.list.path ?? ''}?limit=8`, { signal }),
  });
  const actorName = useActorNames((audit.data ?? []).map((row) => row.actor_id));
  const items: TimelineItem[] = (audit.data ?? []).map((row) => ({
    id: row.id,
    at: row.occurred_at,
    meta: row.action,
    tone: /(blocked|revoked|deleted|failed)/.test(row.action) ? 'danger' : 'accent',
    title: (
      <>
        <span className="font-semibold">
          {row.actor_id ? actorName(row.actor_id) : row.actor_type}
        </span>
        {row.resource_id ? ` on ${shortId(row.resource_id)}` : ''}
      </>
    ),
  }));
  return (
    <Card className="flex-1 basis-96">
      <CardHeader>
        <CardTitle>Recent activity</CardTitle>
        <Link to="/platform/audit" className="ml-auto text-xs text-accent hover:underline">
          Audit log
        </Link>
      </CardHeader>
      {!source ? (
        <EmptyState title="No audit source is exposed" />
      ) : audit.isError ? (
        <ErrorState error={audit.error} onRetry={() => void audit.refetch()} />
      ) : audit.isPending ? (
        <LoadingRows rows={3} label="Loading activity" />
      ) : items.length === 0 ? (
        <EmptyState title="No activity recorded yet" />
      ) : (
        <div className="p-4">
          <Timeline items={items} />
        </div>
      )}
    </Card>
  );
}

export function DashboardPage() {
  return (
    <div className="flex flex-col gap-6">
      <PageHeader
        title="Overview"
        description="Platform-wide operations. Organization roles alone never grant access here."
      />
      <HealthTiles />
      <div className="flex flex-wrap items-start gap-4">
        <NeedsAttention />
        <RecentActivity />
      </div>
    </div>
  );
}
