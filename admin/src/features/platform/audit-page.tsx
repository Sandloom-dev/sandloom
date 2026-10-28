import { useMemo, useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import { keepPreviousData, useQuery } from '@tanstack/react-query';
import type { ColumnDef } from '@tanstack/react-table';
import { SearchX } from 'lucide-react';
import { api } from '@/api/client';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { CopyText } from '@/components/patterns/copy-text';
import { DataTable } from '@/components/patterns/data-table';
import { FilterBar } from '@/components/patterns/filter-bar';
import { PageHeader } from '@/components/patterns/page-header';
import { Segmented } from '@/components/patterns/segmented';
import { EmptyState } from '@/components/patterns/states';
import { formatDateTime, formatRelative } from '@/lib/format';
import { useSubject } from '@/app/session';
import { useConfig } from '@/app/config-context';
import { recordHref } from '@/features/resources/routes';
import type { Field } from '@/schema/types';
import { platformPath, type AuditRow } from './api';
import { useActorNames } from './actors';
import { sourceName, usePlatformSources } from './sources';

const RANGES = [
  { value: '1h', label: 'Last hour', ms: 3_600_000 },
  { value: '24h', label: '24 hours', ms: 86_400_000 },
  { value: '7d', label: '7 days', ms: 604_800_000 },
  { value: '30d', label: '30 days', ms: 2_592_000_000 },
  { value: 'all', label: 'All time', ms: 0 },
] as const;
type Range = (typeof RANGES)[number]['value'];

const FILTERS: Field[] = [
  {
    name: 'action_prefix',
    label: 'Action starts with',
    type: 'string',
    required: false,
    nullable: true,
    maxLength: 200,
  },
  {
    name: 'actor_id',
    label: 'Actor ID',
    type: 'string',
    format: 'uuid',
    required: false,
    nullable: true,
  },
  {
    name: 'resource_type',
    label: 'Resource type',
    type: 'string',
    required: false,
    nullable: true,
    maxLength: 100,
  },
  {
    name: 'resource_id',
    label: 'Resource ID',
    type: 'string',
    required: false,
    nullable: true,
    maxLength: 200,
  },
];
const PAGE_SIZE = 50;

/** Resource types the console can open as a record page. */
const RECORD_PAGES: Record<string, string> = {
  user: 'users',
  organization: 'organizations',
  role: 'roles',
};

function tone(action: string) {
  if (/(blocked|revoked|deleted|removed|deactivated|failed|refused)/.test(action))
    return 'danger' as const;
  if (/(unblocked|created|assigned|added|registered|requeued|replayed)/.test(action))
    return 'success' as const;
  return 'neutral' as const;
}

/**
 * One audit explorer over every module source. Keyset-paged newest first
 * (the API returns X-Next-Cursor, never a total); filters live in the URL.
 */
export function AuditPage() {
  const subject = useSubject();
  const { manifest } = useConfig();
  const sources = usePlatformSources('audit').map(sourceName);
  const [search, setSearch] = useSearchParams();
  const source = sources.find((name) => name === search.get('source')) ?? sources[0];
  const range = (RANGES.find((r) => r.value === search.get('range'))?.value ??
    '24h') satisfies Range;
  const filters = Object.fromEntries(
    FILTERS.flatMap((field) => {
      const value = search.get(`f.${field.name}`);
      return value ? [[field.name, value]] : [];
    }),
  );
  // Cursor stack for Previous/Next; reset whenever the query changes.
  const queryKey = JSON.stringify([source, range, filters]);
  const [pages, setPages] = useState<{ key: string; cursors: string[] }>({
    key: queryKey,
    cursors: [],
  });
  const cursors = pages.key === queryKey ? pages.cursors : [];
  const cursor = cursors.at(-1);
  // A stable window start per query so paging does not shift the time range.
  const [anchor] = useState(() => Date.now());
  const since =
    range === 'all'
      ? undefined
      : new Date(anchor - (RANGES.find((r) => r.value === range)?.ms ?? 0)).toISOString();

  const result = useQuery({
    queryKey: ['audit', subject, 'explorer', source, range, filters, cursor],
    enabled: Boolean(source),
    placeholderData: keepPreviousData,
    queryFn: ({ signal }) =>
      api.response<AuditRow[]>(
        platformPath(source ?? '', 'audit', { ...filters, since, cursor, limit: PAGE_SIZE }),
        { signal },
      ),
  });
  const rows = result.data?.data;
  const next = result.data?.headers.get('X-Next-Cursor') ?? null;
  const actorName = useActorNames((rows ?? []).map((row) => row.actor_id));
  const pagesAvailable = useMemo(
    () => new Set(manifest.resources.filter((r) => r.detail).map((r) => r.name)),
    [manifest],
  );

  const update = (patch: Record<string, string | undefined>) => {
    setSearch((current) => {
      const params = new URLSearchParams(current);
      for (const [key, value] of Object.entries(patch)) {
        if (value) params.set(key, value);
        else params.delete(key);
      }
      return params;
    });
  };

  const columns = useMemo<ColumnDef<AuditRow>[]>(
    () => [
      {
        id: 'when',
        header: 'When',
        cell: ({ row }) => (
          <time
            dateTime={row.original.occurred_at}
            title={formatDateTime(row.original.occurred_at)}
          >
            {formatRelative(row.original.occurred_at)}
          </time>
        ),
      },
      {
        id: 'actor',
        header: 'Actor',
        cell: ({ row }) =>
          row.original.actor_id && pagesAvailable.has('users') ? (
            <Link
              className="font-medium hover:underline"
              to={recordHref('users', row.original.actor_id)}
            >
              {actorName(row.original.actor_id)}
            </Link>
          ) : (
            <span className="font-medium">{actorName(row.original.actor_id)}</span>
          ),
      },
      {
        id: 'action',
        header: 'Action',
        cell: ({ row }) => (
          <Badge tone={tone(row.original.action)} className="font-mono">
            {row.original.action}
          </Badge>
        ),
      },
      {
        id: 'resource',
        header: 'Resource',
        cell: ({ row }) => {
          const { resource_type: type, resource_id: id } = row.original;
          if (!id) return <span className="text-fg-subtle">—</span>;
          const page = type ? RECORD_PAGES[type] : undefined;
          return (
            <span className="flex items-center gap-2">
              {type && <span className="text-fg-muted">{type}</span>}
              {page && pagesAvailable.has(page) ? (
                <Link
                  className="font-mono text-xs text-accent hover:underline"
                  to={recordHref(page, id)}
                >
                  {id.length > 12 ? `${id.slice(0, 4)}…${id.slice(-4)}` : id}
                </Link>
              ) : (
                <CopyText value={id} short />
              )}
            </span>
          );
        },
      },
      {
        id: 'request',
        header: 'Request',
        cell: ({ row }) =>
          row.original.request_id ? (
            <CopyText value={row.original.request_id} short />
          ) : (
            <span className="text-fg-subtle">—</span>
          ),
      },
    ],
    [actorName, pagesAvailable],
  );

  if (!source) return <EmptyState title="No audit source is exposed" />;
  return (
    <div className="flex flex-col gap-4">
      <PageHeader
        title="Audit log"
        description="Committed audit metadata of the selected module's database. Free-form audit payloads never reach the browser."
        actions={
          <Segmented
            label="Audit source"
            options={sources.map((name) => ({ value: name, label: name }))}
            value={source}
            onChange={(value) => {
              update({ source: value });
            }}
          />
        }
      />
      <Segmented
        label="Time range"
        options={RANGES}
        value={range}
        onChange={(value) => {
          update({ range: value });
        }}
      />
      <FilterBar
        fields={FILTERS}
        values={filters}
        searchField="action_prefix"
        onChange={(values) => {
          update(
            Object.fromEntries(FILTERS.map((field) => [`f.${field.name}`, values[field.name]])),
          );
        }}
      />
      <DataTable
        label="Audit log"
        columns={columns}
        data={rows}
        getRowId={(row) => row.id}
        status={result.status}
        error={result.error}
        onRetry={() => void result.refetch()}
        empty={
          <EmptyState
            icon={SearchX}
            title="No audit entries"
            description="Nothing matches this source, time range and filters."
          />
        }
      />
      <nav aria-label="Audit pages" className="flex items-center gap-2">
        <span className="text-fg-muted">Page {cursors.length + 1}</span>
        <Button
          size="sm"
          className="ml-auto"
          disabled={cursors.length === 0}
          onClick={() => {
            setPages({ key: queryKey, cursors: cursors.slice(0, -1) });
          }}
        >
          Newer
        </Button>
        <Button
          size="sm"
          disabled={!next}
          onClick={() => {
            if (next) setPages({ key: queryKey, cursors: [...cursors, next] });
          }}
        >
          Older
        </Button>
      </nav>
    </div>
  );
}
