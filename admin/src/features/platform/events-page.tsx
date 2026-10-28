import { useId, useMemo, useState } from 'react';
import type { UseQueryResult } from '@tanstack/react-query';
import type { ColumnDef, RowSelectionState } from '@tanstack/react-table';
import { CircleCheck, Inbox } from 'lucide-react';
import { api } from '@/api/client';
import { Badge } from '@/components/ui/badge';
import { NativeSelect } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { CopyText } from '@/components/patterns/copy-text';
import { DataTable } from '@/components/patterns/data-table';
import { PageHeader } from '@/components/patterns/page-header';
import { Segmented } from '@/components/patterns/segmented';
import { EmptyState } from '@/components/patterns/states';
import { formatDateTime, formatRelative } from '@/lib/format';
import { useSubject } from '@/app/session';
import { statusTone } from '@/features/resources/cells';
import {
  platformKeys,
  platformPath,
  useOperationsSource,
  usePlatformView,
  type CommandResult,
  type Consumer,
  type DeadLetter,
  type Projection,
  type StreamDeadLetter,
} from './api';
import { useIncidentPage } from './use-incident-page';
import { ReasonCommand } from './reason-command';

function When({ value }: { value: string | null | undefined }) {
  if (!value) return <span className="text-fg-subtle">—</span>;
  return (
    <time dateTime={value} title={formatDateTime(value)}>
      {formatRelative(value)}
    </time>
  );
}

function ErrorText({ value }: { value: string | null | undefined }) {
  if (!value) return <span className="text-fg-subtle">—</span>;
  return (
    <span className="block max-w-prose truncate text-danger" title={value}>
      {value}
    </span>
  );
}

const command = (
  source: string,
  path: 'outbox/requeue' | 'streams/replay',
  body: unknown,
  reference: string,
) =>
  api.request<CommandResult>(platformPath(source, path), {
    method: 'POST',
    headers: { 'Idempotency-Key': reference },
    body: JSON.stringify(body),
  });

function OutboxDeadLetters({ source }: { source: string }) {
  const subject = useSubject();
  const rows = useIncidentPage<DeadLetter>(source, 'outbox/dead');
  const [selection, setSelection] = useState<RowSelectionState>({});
  const columns = useMemo<ColumnDef<DeadLetter>[]>(
    () => [
      {
        id: 'id',
        header: 'Envelope',
        cell: ({ row }) => <CopyText value={row.original.id} short />,
      },
      {
        id: 'channel',
        header: 'Channel',
        cell: ({ row }) => <span className="font-mono text-xs">{row.original.channel}</span>,
      },
      {
        id: 'attempts',
        header: 'Attempts',
        cell: ({ row }) => <span className="tabular-nums">{row.original.attempts}</span>,
      },
      {
        id: 'dead_at',
        header: 'Dead since',
        cell: ({ row }) => <When value={row.original.dead_at} />,
      },
      {
        id: 'last_error',
        header: 'Last error',
        cell: ({ row }) => <ErrorText value={row.original.last_error} />,
      },
    ],
    [],
  );
  return (
    <div className="flex flex-col gap-3">
      <p className="text-fg-muted">
        Events the relay could not hand to the broker after its attempt budget. Requeued rows keep
        their envelope id, so consumers that already applied an event deduplicate the redelivery.
      </p>
      <DataTable
        label="Outbox dead letters"
        columns={columns}
        data={rows.data}
        getRowId={(row) => row.id}
        status={rows.status}
        error={rows.error}
        onRetry={() => void rows.refetch()}
        pagination={{
          ...rows.pagination,
          onPageChange: (page) => {
            setSelection({});
            rows.pagination.onPageChange(page);
          },
          onPageSizeChange: (size) => {
            setSelection({});
            rows.pagination.onPageSizeChange(size);
          },
        }}
        empty={
          <EmptyState
            icon={CircleCheck}
            title="No dead letters"
            description="Every outbox event of this database reached the broker."
          />
        }
        selection={{
          value: selection,
          onChange: setSelection,
          actions: (selected) => (
            <ReasonCommand
              trigger={`Requeue ${selected.length}…`}
              title={`Requeue ${selected.length} dead letter${selected.length === 1 ? '' : 's'}?`}
              description="The relay redelivers them with a fresh attempt budget (sandloom outbox requeue). Recorded in the audit log as core.outbox.requeued."
              confirmLabel="Requeue"
              run={(reason, reference) =>
                command(
                  source,
                  'outbox/requeue',
                  { ids: selected.map((row) => row.id), reason },
                  reference,
                )
              }
              invalidate={[platformKeys.view(subject, source, 'outbox/dead')]}
              onDone={() => {
                setSelection({});
                rows.resetPage();
              }}
            />
          ),
        }}
      />
    </div>
  );
}

function StreamDeadLetters({ source, channels }: { source: string; channels: string[] }) {
  const subject = useSubject();
  const selectId = useId();
  const [channel, setChannel] = useState('');
  const active = channels.includes(channel) ? channel : (channels[0] ?? '');
  const [selection, setSelection] = useState<RowSelectionState>({});
  const rows = useIncidentPage<StreamDeadLetter>(
    source,
    'streams/dlq',
    { channel: active },
    Boolean(active),
  );
  const columns = useMemo<ColumnDef<StreamDeadLetter>[]>(
    () => [
      {
        id: 'id',
        header: 'Entry',
        cell: ({ row }) => <span className="font-mono text-xs">{row.original.id}</span>,
      },
      {
        id: 'group',
        header: 'Consumer group',
        cell: ({ row }) => <span className="font-mono text-xs">{row.original.group ?? '—'}</span>,
      },
      {
        id: 'event',
        header: 'Event',
        cell: ({ row }) =>
          row.original.event_id ? <CopyText value={row.original.event_id} short /> : '—',
      },
      { id: 'attempts', header: 'Attempts', cell: ({ row }) => row.original.attempts ?? '—' },
      {
        id: 'reason',
        header: 'Reason',
        cell: ({ row }) => <ErrorText value={row.original.reason} />,
      },
      {
        id: 'replayable',
        header: 'Replayable',
        cell: ({ row }) =>
          row.original.replayable ? (
            <Badge tone="success">Yes</Badge>
          ) : (
            <Badge tone="warning">Payload lost</Badge>
          ),
      },
    ],
    [],
  );
  if (channels.length === 0)
    return (
      <EmptyState
        icon={Inbox}
        title="No event consumers here"
        description="This API process consumes no channel, so it owns no Streams dead letters."
      />
    );
  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap items-end gap-3">
        <div className="flex flex-col gap-1.5">
          <Label htmlFor={selectId}>Channel</Label>
          <NativeSelect
            id={selectId}
            className="w-80"
            value={active}
            onChange={(event) => {
              setSelection({});
              setChannel(event.target.value);
            }}
          >
            {channels.map((name) => (
              <option key={name} value={name}>
                {name}
              </option>
            ))}
          </NativeSelect>
        </div>
        <p className="max-w-prose text-fg-muted">
          A replay is targeted at the consumer group that dead-lettered the entry; every other group
          skips it. Select entries of one group.
        </p>
      </div>
      <DataTable
        label="Streams dead letters"
        columns={columns}
        data={rows.data}
        getRowId={(row) => row.id}
        status={rows.status}
        error={rows.error}
        onRetry={() => void rows.refetch()}
        pagination={{
          ...rows.pagination,
          onPageChange: (page) => {
            setSelection({});
            rows.pagination.onPageChange(page);
          },
          onPageSizeChange: (size) => {
            setSelection({});
            rows.pagination.onPageSizeChange(size);
          },
        }}
        empty={
          <EmptyState
            icon={CircleCheck}
            title="Nothing dead-lettered"
            description={`No entry of ${active} is parked.`}
          />
        }
        selection={{
          value: selection,
          onChange: setSelection,
          actions: (selected) => {
            const groups = new Set(selected.map((row) => row.group));
            const [group] = [...groups];
            const valid = groups.size === 1 && group && selected.every((row) => row.replayable);
            return (
              <>
                {!valid && (
                  <span className="text-xs text-fg-muted">
                    Select replayable entries of a single consumer group.
                  </span>
                )}
                <ReasonCommand
                  trigger={`Replay ${selected.length}…`}
                  disabled={!valid}
                  title={`Replay ${selected.length} entr${selected.length === 1 ? 'y' : 'ies'} to ${group ?? 'its group'}?`}
                  description="Re-published to the stream for that group only, with a fresh delivery budget (sandloom streams dlq-replay --group). Recorded in the audit log."
                  confirmLabel="Replay"
                  run={(reason, reference) =>
                    command(
                      source,
                      'streams/replay',
                      { channel: active, group, ids: selected.map((row) => row.id), reason },
                      reference,
                    )
                  }
                  invalidate={[platformKeys.view(subject, source, 'streams/dlq')]}
                  onDone={() => {
                    setSelection({});
                    rows.resetPage();
                  }}
                />
              </>
            );
          },
        }}
      />
    </div>
  );
}

function Consumers({ rows }: { rows: UseQueryResult<Consumer[]> }) {
  const columns = useMemo<ColumnDef<Consumer>[]>(
    () => [
      {
        id: 'group',
        header: 'Group',
        cell: ({ row }) => (
          <span className="font-mono text-xs">{row.original.group ?? row.original.name}</span>
        ),
      },
      {
        id: 'channel',
        header: 'Channel',
        cell: ({ row }) => <span className="font-mono text-xs">{row.original.channel}</span>,
      },
      { id: 'kind', header: 'Kind', cell: ({ row }) => row.original.kind },
      {
        id: 'required',
        header: 'Required',
        cell: ({ row }) => (row.original.required ? 'Yes' : 'No'),
      },
      {
        id: 'state',
        header: 'State',
        cell: ({ row }) => (
          <Badge tone={statusTone(row.original.state)} dot>
            {row.original.state}
          </Badge>
        ),
      },
      { id: 'lag', header: 'Lag', cell: ({ row }) => row.original.lag ?? '—' },
      { id: 'pending', header: 'Pending', cell: ({ row }) => row.original.pending ?? '—' },
      {
        id: 'connected',
        header: 'Connected',
        cell: ({ row }) => <When value={row.original.last_connected_at} />,
      },
      {
        id: 'error',
        header: 'Last error',
        cell: ({ row }) => <ErrorText value={row.original.last_error} />,
      },
    ],
    [],
  );
  return (
    <DataTable
      label="Event consumers"
      columns={columns}
      data={rows.data}
      getRowId={(row) => row.name}
      status={rows.status}
      error={rows.error}
      onRetry={() => void rows.refetch()}
      empty={<EmptyState title="No event consumers in this process" />}
    />
  );
}

function Projections({ source }: { source: string }) {
  const rows = usePlatformView<Projection[]>(source, 'projections');
  const columns = useMemo<ColumnDef<Projection>[]>(
    () => [
      {
        id: 'name',
        header: 'Projection',
        cell: ({ row }) => <span className="font-medium">{row.original.name}</span>,
      },
      { id: 'owner', header: 'Owner', cell: ({ row }) => row.original.owner_module },
      { id: 'version', header: 'Version', cell: ({ row }) => row.original.version ?? '—' },
      { id: 'storage', header: 'Storage', cell: ({ row }) => row.original.storage ?? '—' },
      {
        id: 'rebuild',
        header: 'Rebuild',
        cell: ({ row }) =>
          row.original.rebuildable && row.original.has_rebuilder ? (
            <Badge tone="success">sandloom projection rebuild</Badge>
          ) : (
            <Badge tone="warning">Not rebuildable</Badge>
          ),
      },
      {
        id: 'sources',
        header: 'Fed by',
        cell: ({ row }) => (
          <span className="flex flex-wrap gap-1">
            {row.original.consumers.map((consumer) => (
              <Badge key={consumer.name} tone="outline" className="font-mono">
                {consumer.source_channel}
              </Badge>
            ))}
          </span>
        ),
      },
    ],
    [],
  );
  return (
    <DataTable
      label="Query projections"
      columns={columns}
      data={rows.data}
      getRowId={(row) => row.name}
      status={rows.status}
      error={rows.error}
      onRetry={() => void rows.refetch()}
      empty={<EmptyState title="No query projections registered" />}
    />
  );
}

/** Outbox and Streams dead letters, consumers and projections of one API process. */
export function EventsPage() {
  const { sources, source, setSource } = useOperationsSource();
  const consumers = usePlatformView<Consumer[]>(
    source,
    'consumers',
    {},
    { refetchInterval: 30_000 },
  );
  const dead = useIncidentPage<DeadLetter>(source, 'outbox/dead');
  const channels = [...new Set((consumers.data ?? []).map((consumer) => consumer.channel))].sort();
  if (!source) return <EmptyState title="No operations source is exposed" />;
  const deadCount = dead.data?.length ?? 0;
  const degraded = (consumers.data ?? []).filter((consumer) => consumer.state !== 'running').length;
  return (
    <div className="flex flex-col gap-4">
      <PageHeader
        title="Event delivery"
        description="Transactional outbox, Streams dead-letter queues, consumers and projections of the selected API process."
        actions={
          <Segmented
            label="Source"
            options={sources.map((name) => ({ value: name, label: name }))}
            value={source}
            onChange={setSource}
          />
        }
      />
      <Tabs defaultValue="outbox">
        <TabsList>
          <TabsTrigger value="outbox">
            Outbox dead letters
            {deadCount > 0 && (
              <Badge tone="danger">
                {deadCount}
                {dead.pagination.hasNextPage ? '+' : ''}
              </Badge>
            )}
          </TabsTrigger>
          <TabsTrigger value="streams">Streams DLQ</TabsTrigger>
          <TabsTrigger value="consumers">
            Consumers
            {degraded > 0 && <Badge tone="warning">{degraded}</Badge>}
          </TabsTrigger>
          <TabsTrigger value="projections">Projections</TabsTrigger>
        </TabsList>
        <TabsContent value="outbox">
          <OutboxDeadLetters key={source} source={source} />
        </TabsContent>
        <TabsContent value="streams">
          <StreamDeadLetters key={source} source={source} channels={channels} />
        </TabsContent>
        <TabsContent value="consumers">
          <Consumers rows={consumers} />
        </TabsContent>
        <TabsContent value="projections">
          <Projections key={source} source={source} />
        </TabsContent>
      </Tabs>
    </div>
  );
}
