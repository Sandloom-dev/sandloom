import { useMemo } from 'react';
import type { ColumnDef } from '@tanstack/react-table';
import { Badge, type BadgeTone } from '@/components/ui/badge';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { DataTable } from '@/components/patterns/data-table';
import { PageHeader } from '@/components/patterns/page-header';
import { Segmented } from '@/components/patterns/segmented';
import { EmptyState, ErrorState, LoadingRows } from '@/components/patterns/states';
import type { Schema } from '@/api/client';
import { useOperationsSource, usePlatformView, type Contracts, type Module } from './api';

type Binding = Schema<'BindingView'>;
type Consumed = Schema<'ConsumedContractView'>;

const BINDING_TONE: Record<Binding['status'], BadgeTone> = {
  ok: 'success',
  unreachable: 'danger',
  incompatible: 'danger',
  unverified: 'warning',
  not_probed: 'neutral',
};
const RESOLUTION_TONE: Record<string, BadgeTone> = {
  local: 'success',
  remote: 'accent',
  missing: 'danger',
};

/** Sync contracts, their bindings and the loaded modules of one API process. */
export function ContractsPage() {
  const { sources, source, setSource } = useOperationsSource();
  const contracts = usePlatformView<Contracts>(source, 'contracts');
  const modules = usePlatformView<Module[]>(source, 'modules');
  const bindingColumns = useMemo<ColumnDef<Binding>[]>(
    () => [
      {
        id: 'contract',
        header: 'Contract',
        cell: ({ row }) => <span className="font-mono text-xs">{row.original.contract}</span>,
      },
      {
        id: 'required',
        header: 'Required',
        cell: ({ row }) => (row.original.required ? 'Yes' : 'No'),
      },
      {
        id: 'status',
        header: 'Status',
        cell: ({ row }) => (
          <Badge tone={BINDING_TONE[row.original.status]} dot>
            {row.original.status.replace('_', ' ')}
          </Badge>
        ),
      },
      {
        id: 'detail',
        header: 'Detail',
        cell: ({ row }) => <span className="text-fg-muted">{row.original.detail ?? '—'}</span>,
      },
    ],
    [],
  );
  const consumedColumns = useMemo<ColumnDef<Consumed>[]>(
    () => [
      { id: 'module', header: 'Module', cell: ({ row }) => row.original.module },
      {
        id: 'contract',
        header: 'Contract',
        cell: ({ row }) => <span className="font-mono text-xs">{row.original.contract}</span>,
      },
      {
        id: 'required',
        header: 'Required',
        cell: ({ row }) => (row.original.required ? 'Yes' : 'No'),
      },
      {
        id: 'resolution',
        header: 'Resolved',
        cell: ({ row }) => (
          <Badge tone={RESOLUTION_TONE[row.original.resolution] ?? 'neutral'}>
            {row.original.resolution}
          </Badge>
        ),
      },
    ],
    [],
  );
  const moduleColumns = useMemo<ColumnDef<Module>[]>(
    () => [
      {
        id: 'name',
        header: 'Module',
        cell: ({ row }) => <span className="font-medium">{row.original.name}</span>,
      },
      { id: 'version', header: 'Version', cell: ({ row }) => row.original.version ?? '—' },
      {
        id: 'status',
        header: 'Status',
        cell: ({ row }) => (
          <Badge
            tone={
              row.original.status === 'loaded'
                ? 'success'
                : row.original.status === 'failed'
                  ? 'danger'
                  : 'neutral'
            }
            dot
          >
            {row.original.status}
          </Badge>
        ),
      },
      {
        id: 'required',
        header: 'Required',
        cell: ({ row }) => (row.original.required ? 'Yes' : 'No'),
      },
      {
        id: 'detail',
        header: 'Detail',
        cell: ({ row }) => <span className="text-fg-muted">{row.original.detail ?? '—'}</span>,
      },
    ],
    [],
  );
  if (!source) return <EmptyState title="No operations source is exposed" />;
  return (
    <div className="flex flex-col gap-4">
      <PageHeader
        title="Contracts & modules"
        description="Sync service contracts this process provides and consumes, remote bindings with reachability and compatibility, and the module load result."
        actions={
          <Segmented
            label="Source"
            options={sources.map((name) => ({ value: name, label: name }))}
            value={source}
            onChange={setSource}
          />
        }
      />
      <Card>
        <CardHeader>
          <CardTitle>Provided contracts</CardTitle>
          {contracts.data && (
            <span className="ml-auto text-xs text-fg-subtle">
              Compatibility check: {contracts.data.compat_check}
            </span>
          )}
        </CardHeader>
        {contracts.isError ? (
          <ErrorState error={contracts.error} onRetry={() => void contracts.refetch()} />
        ) : contracts.isPending ? (
          <LoadingRows rows={2} label="Loading contracts" />
        ) : (
          <CardContent className="flex flex-wrap gap-1.5">
            {contracts.data.provided.length === 0 && <span className="text-fg-subtle">None</span>}
            {contracts.data.provided.map((name) => (
              <Badge key={name} tone="outline" className="font-mono">
                {name}
              </Badge>
            ))}
          </CardContent>
        )}
      </Card>
      <section aria-labelledby="bindings" className="flex flex-col gap-2">
        <h2 id="bindings" className="font-semibold">
          Remote bindings
        </h2>
        <DataTable
          label="Remote bindings"
          columns={bindingColumns}
          data={contracts.data?.bindings}
          getRowId={(row) => row.contract}
          status={contracts.status}
          error={contracts.error}
          empty={
            <EmptyState
              title="No remote bindings"
              description="Every consumed contract is provided in this process (monolith)."
            />
          }
        />
      </section>
      <section aria-labelledby="consumed" className="flex flex-col gap-2">
        <h2 id="consumed" className="font-semibold">
          Consumed contracts
        </h2>
        <DataTable
          label="Consumed contracts"
          columns={consumedColumns}
          data={contracts.data?.consumed}
          getRowId={(row) => `${row.module}:${row.contract}`}
          status={contracts.status}
          error={contracts.error}
          empty={<EmptyState title="No consumed contracts declared" />}
        />
      </section>
      <section aria-labelledby="modules" className="flex flex-col gap-2">
        <h2 id="modules" className="font-semibold">
          Modules
        </h2>
        <DataTable
          label="Modules"
          columns={moduleColumns}
          data={modules.data}
          getRowId={(row) => row.name}
          status={modules.status}
          error={modules.error}
          onRetry={() => void modules.refetch()}
          empty={<EmptyState title="No modules loaded" />}
        />
      </section>
    </div>
  );
}
