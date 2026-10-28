import { useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import type { ColumnDef, RowSelectionState, VisibilityState } from '@tanstack/react-table';
import { Columns3, Plus, Rows3, SearchX } from 'lucide-react';
import { Button } from '@/components/ui/button';
import {
  DropdownMenu,
  DropdownMenuCheckboxItem,
  DropdownMenuContent,
  DropdownMenuLabel,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';
import { DataTable, type Density } from '@/components/patterns/data-table';
import { FilterBar } from '@/components/patterns/filter-bar';
import { PageHeader } from '@/components/patterns/page-header';
import { SavedViews, type SavedView } from '@/components/patterns/saved-views';
import { EmptyState } from '@/components/patterns/states';
import { useStoredState } from '@/hooks/use-stored-state';
import { allowed, type AdminResource } from '@/schema/types';
import { useSession, useSubject } from '@/app/session';
import { BulkActionDialog } from './bulk-action-dialog';
import { renderValue } from './cells';
import { resourceKeys, useResourceList, type ResourceRecord } from './data';
import { formHref, recordHref } from './routes';
import { pageSizesFor, useListUrlState } from './url-state';

const parseViews = (raw: unknown): SavedView[] | undefined =>
  Array.isArray(raw)
    ? raw.filter(
        (view): view is SavedView =>
          typeof view === 'object' &&
          view !== null &&
          typeof (view as SavedView).name === 'string' &&
          typeof (view as SavedView).filters === 'object',
      )
    : undefined;
const parseVisibility = (raw: unknown): VisibilityState | undefined =>
  raw && typeof raw === 'object' ? (raw as VisibilityState) : undefined;
const parseDensity = (raw: unknown): Density | undefined =>
  raw === 'compact' || raw === 'comfortable' ? raw : undefined;

/** Generated list screen for any admitted resource. */
export function ResourceListPage({ resource, scope }: { resource: AdminResource; scope: string }) {
  const subject = useSubject();
  const { session } = useSession();
  const { params, update } = useListUrlState(resource, scope);
  const list = useResourceList(resource, params);
  const [views, setViews] = useStoredState<SavedView[]>(
    `sandloom.views.${resource.name}`,
    [],
    parseViews,
  );
  const [visibility, setVisibility] = useStoredState<VisibilityState>(
    `sandloom.columns.${resource.name}`,
    {},
    parseVisibility,
  );
  const [density, setDensity] = useStoredState<Density>(
    'sandloom.density',
    'comfortable',
    parseDensity,
  );
  const [selection, setSelection] = useState<RowSelectionState>({});

  const columns = useMemo<ColumnDef<ResourceRecord>[]>(
    () =>
      resource.columns.map((field, index) => ({
        id: field.name,
        header: field.label,
        accessorFn: (row) => row[field.name],
        cell: ({ row }) => {
          const content = renderValue(field, row.original[field.name], { dense: true });
          return index === 0 && resource.detail ? (
            <Link
              to={recordHref(resource.name, row.original.id, scope)}
              className="font-medium text-fg hover:underline"
            >
              {content}
            </Link>
          ) : (
            content
          );
        },
      })),
    [resource, scope],
  );
  const bulkActions = resource.actions.filter(
    (action) =>
      !action.idempotency_field &&
      session !== undefined &&
      allowed(action.access ?? resource.access, session.roles),
  );
  const filtered = Object.keys(params.filters).length > 0;

  return (
    <div className="flex flex-col gap-4">
      <PageHeader
        title={resource.label}
        description={resource.group ?? undefined}
        actions={
          resource.create && (
            <Button asChild variant="primary">
              <Link to={formHref(resource.name, undefined, scope)}>
                <Plus aria-hidden />
                New
              </Link>
            </Button>
          )
        }
      />
      {resource.filters.length > 0 && (
        <SavedViews
          views={views}
          current={params.filters}
          onSelect={(filters) => {
            update({ filters });
          }}
          onSave={(name) => {
            setViews([
              ...views.filter((view) => view.name !== name),
              { name, filters: params.filters },
            ]);
          }}
          onDelete={(name) => {
            setViews(views.filter((view) => view.name !== name));
          }}
        />
      )}
      <FilterBar
        fields={resource.filters}
        values={params.filters}
        onChange={(filters) => {
          update({ filters });
        }}
        trailing={
          <>
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <Button size="sm">
                  <Columns3 aria-hidden />
                  Columns
                </Button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end">
                <DropdownMenuLabel>Visible columns</DropdownMenuLabel>
                {resource.columns.map((field, index) => (
                  <DropdownMenuCheckboxItem
                    key={field.name}
                    disabled={index === 0}
                    checked={visibility[field.name] !== false}
                    onCheckedChange={(checked) => {
                      setVisibility({ ...visibility, [field.name]: checked });
                    }}
                    onSelect={(event) => {
                      event.preventDefault();
                    }}
                  >
                    {field.label}
                  </DropdownMenuCheckboxItem>
                ))}
              </DropdownMenuContent>
            </DropdownMenu>
            <Button
              size="sm"
              aria-pressed={density === 'compact'}
              onClick={() => {
                setDensity(density === 'compact' ? 'comfortable' : 'compact');
              }}
            >
              <Rows3 aria-hidden />
              {density === 'compact' ? 'Compact' : 'Comfortable'}
            </Button>
          </>
        }
      />
      <DataTable
        label={resource.label}
        columns={columns}
        data={list.data?.rows}
        getRowId={(row) => String(row.id)}
        status={list.status}
        error={list.error}
        onRetry={() => void list.refetch()}
        density={density}
        columnVisibility={visibility}
        sortable={resource.sort_fields}
        sort={params.sort}
        onSortChange={(sort) => {
          update({ sort });
        }}
        empty={
          filtered ? (
            <EmptyState
              icon={SearchX}
              title="No matches"
              description="Nothing matches these filters."
              action={
                <Button
                  onClick={() => {
                    update({ filters: {} });
                  }}
                >
                  Clear filters
                </Button>
              }
            />
          ) : (
            <EmptyState title={`No ${resource.label.toLowerCase()} yet`} />
          )
        }
        pagination={{
          page: params.page,
          pageSize: params.pageSize,
          pageSizes: pageSizesFor(resource),
          hasNextPage: list.data?.hasNextPage ?? false,
          hasPreviousPage: list.data?.hasPreviousPage ?? false,
          ...(list.data?.total === undefined ? {} : { total: list.data.total }),
          onPageChange: (page) => {
            setSelection({});
            update({ page });
          },
          onPageSizeChange: (pageSize) => {
            setSelection({});
            update({ pageSize });
          },
        }}
        {...(bulkActions.length > 0 && resource.detail
          ? {
              selection: {
                value: selection,
                onChange: setSelection,
                actions: (rows: ResourceRecord[]) =>
                  bulkActions.map((action) => (
                    <BulkActionDialog
                      key={action.name}
                      action={action}
                      resource={resource}
                      records={rows}
                      scope={scope}
                      invalidate={[resourceKeys.resource(subject, resource.name)]}
                      onDone={(failedIds) => {
                        setSelection(Object.fromEntries(failedIds.map((id) => [id, true])));
                      }}
                    />
                  )),
              },
            }
          : {})}
      />
    </div>
  );
}
