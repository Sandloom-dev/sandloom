import { useMemo, type ReactNode } from 'react';
import {
  flexRender,
  getCoreRowModel,
  useReactTable,
  type ColumnDef,
  type RowSelectionState,
  type VisibilityState,
} from '@tanstack/react-table';
import { ArrowDown, ArrowUp, ArrowUpDown } from 'lucide-react';
import { Checkbox } from '@/components/ui/checkbox';
import { Button } from '@/components/ui/button';
import { NativeSelect } from '@/components/ui/input';
import { cn } from '@/lib/cn';
import { ErrorState, LoadingRows } from './states';

export type Density = 'comfortable' | 'compact';
export interface SortState {
  field: string;
  desc: boolean;
}
export interface PageState {
  page: number;
  pageSize: number;
  pageSizes: number[];
  hasNextPage: boolean;
  hasPreviousPage: boolean;
  /** Present only when the API reports it; never invented client-side. */
  total?: number;
  onPageChange: (page: number) => void;
  onPageSizeChange: (size: number) => void;
}

export interface DataTableProps<T> {
  /** Accessible name of the table. */
  label: string;
  columns: ColumnDef<T>[];
  data: T[] | undefined;
  getRowId: (row: T) => string;
  status: 'pending' | 'error' | 'success';
  error?: unknown;
  onRetry?: () => void;
  empty: ReactNode;
  /** Server-side sorting: only columns listed here get a sort control. */
  sortable?: readonly string[];
  sort?: SortState | null;
  onSortChange?: (sort: SortState | null) => void;
  pagination?: PageState;
  columnVisibility?: VisibilityState;
  density?: Density;
  /** Enables row checkboxes and the bulk bar. */
  selection?: {
    value: RowSelectionState;
    onChange: (value: RowSelectionState) => void;
    actions: (rows: T[]) => ReactNode;
  };
}

/**
 * Server-driven table: data, sorting and pagination are the API's, never
 * re-implemented in the browser. Loading, empty and error states are built in.
 */
export function DataTable<T>({
  label,
  columns,
  data,
  getRowId,
  status,
  error,
  onRetry,
  empty,
  sortable = [],
  sort = null,
  onSortChange,
  pagination,
  columnVisibility,
  density = 'comfortable',
  selection,
}: DataTableProps<T>) {
  const allColumns = useMemo<ColumnDef<T>[]>(
    () =>
      selection
        ? [
            {
              id: '__select',
              header: ({ table }) => (
                <Checkbox
                  aria-label="Select all rows on this page"
                  checked={
                    table.getIsAllRowsSelected()
                      ? true
                      : table.getIsSomeRowsSelected()
                        ? 'indeterminate'
                        : false
                  }
                  onCheckedChange={(value) => {
                    table.toggleAllRowsSelected(value === true);
                  }}
                />
              ),
              cell: ({ row }) => (
                <Checkbox
                  aria-label={`Select row ${row.index + 1}`}
                  checked={row.getIsSelected()}
                  onCheckedChange={(value) => {
                    row.toggleSelected(value === true);
                  }}
                />
              ),
            },
            ...columns,
          ]
        : columns,
    [columns, selection],
  );
  // TanStack Table v8 returns fresh functions each render, so React Compiler skips
  // memoizing this component; the table is cheap (one server page at a time).
  // eslint-disable-next-line react-hooks/incompatible-library
  const table = useReactTable({
    data: data ?? [],
    columns: allColumns,
    getRowId,
    getCoreRowModel: getCoreRowModel(),
    manualPagination: true,
    manualSorting: true,
    enableRowSelection: Boolean(selection),
    state: {
      rowSelection: selection?.value ?? {},
      columnVisibility: columnVisibility ?? {},
    },
    onRowSelectionChange: (updater) => {
      if (!selection) return;
      selection.onChange(typeof updater === 'function' ? updater(selection.value) : updater);
    },
  });
  const selectedRows = table.getSelectedRowModel().rows.map((row) => row.original);
  const cellPadding = density === 'compact' ? 'px-3 py-1.5' : 'px-3 py-2.5';

  return (
    <div className="flex flex-col gap-3">
      {selection && selectedRows.length > 0 && (
        <div
          role="region"
          aria-label="Bulk actions"
          className="flex flex-wrap items-center gap-3 rounded-lg bg-accent-muted px-3 py-2"
        >
          <span className="font-semibold">{selectedRows.length} selected</span>
          {selection.actions(selectedRows)}
          <Button
            variant="ghost"
            size="sm"
            className="ml-auto"
            onClick={() => {
              selection.onChange({});
            }}
          >
            Clear selection
          </Button>
        </div>
      )}
      <div className="overflow-hidden rounded-lg border border-border bg-surface shadow-sm">
        {status === 'error' ? (
          <ErrorState error={error} onRetry={onRetry} />
        ) : (
          <div className="overflow-x-auto">
            <table aria-label={label} className="w-full border-collapse text-left">
              <thead>
                {table.getHeaderGroups().map((group) => (
                  <tr key={group.id} className="border-b border-border">
                    {group.headers.map((header) => {
                      const id = header.column.id;
                      const canSort = sortable.includes(id) && onSortChange;
                      const active = sort?.field === id;
                      return (
                        <th
                          key={header.id}
                          scope="col"
                          aria-sort={active ? (sort.desc ? 'descending' : 'ascending') : undefined}
                          className={cn(
                            'text-xs font-medium whitespace-nowrap text-fg-subtle',
                            cellPadding,
                            id === '__select' && 'w-10',
                          )}
                        >
                          {header.isPlaceholder ? null : canSort ? (
                            <button
                              type="button"
                              className="-mx-1 inline-flex items-center gap-1 rounded-sm px-1 hover:text-fg"
                              onClick={() => {
                                onSortChange(
                                  !active
                                    ? { field: id, desc: false }
                                    : sort.desc
                                      ? null
                                      : { field: id, desc: true },
                                );
                              }}
                            >
                              {flexRender(header.column.columnDef.header, header.getContext())}
                              {active ? (
                                sort.desc ? (
                                  <ArrowDown aria-hidden className="size-3" />
                                ) : (
                                  <ArrowUp aria-hidden className="size-3" />
                                )
                              ) : (
                                <ArrowUpDown aria-hidden className="size-3 opacity-50" />
                              )}
                            </button>
                          ) : (
                            flexRender(header.column.columnDef.header, header.getContext())
                          )}
                        </th>
                      );
                    })}
                  </tr>
                ))}
              </thead>
              {status === 'success' && (
                <tbody>
                  {table.getRowModel().rows.map((row) => (
                    <tr
                      key={row.id}
                      data-selected={row.getIsSelected() || undefined}
                      className="border-b border-border last:border-b-0 hover:bg-surface-muted data-[selected]:bg-accent-muted"
                    >
                      {row.getVisibleCells().map((cell) => (
                        <td
                          key={cell.id}
                          className={cn('align-middle whitespace-nowrap', cellPadding)}
                        >
                          {flexRender(cell.column.columnDef.cell, cell.getContext())}
                        </td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              )}
            </table>
            {status === 'pending' && <LoadingRows label={`Loading ${label}`} />}
            {status === 'success' && (data?.length ?? 0) === 0 && empty}
          </div>
        )}
        {pagination && status !== 'error' && (
          <Pagination {...pagination} count={data?.length ?? 0} />
        )}
      </div>
    </div>
  );
}

function Pagination({
  page,
  pageSize,
  pageSizes,
  hasNextPage,
  hasPreviousPage,
  total,
  onPageChange,
  onPageSizeChange,
  count,
}: PageState & { count: number }) {
  const from = count === 0 ? 0 : (page - 1) * pageSize + 1;
  const to = (page - 1) * pageSize + count;
  return (
    <nav
      aria-label="Pagination"
      className="flex flex-wrap items-center gap-3 border-t border-border px-3 py-2.5 text-fg-muted"
    >
      <span aria-live="polite">
        {count === 0
          ? 'No rows'
          : total === undefined
            ? `Rows ${from}–${to}${hasNextPage ? ' · more available' : ''}`
            : `Rows ${from}–${to} of ${total}`}
      </span>
      <label className="ml-auto flex items-center gap-2">
        Rows per page
        <NativeSelect
          className="h-control-sm w-20"
          value={pageSize}
          onChange={(event) => {
            onPageSizeChange(Number(event.target.value));
          }}
        >
          {pageSizes.map((size) => (
            <option key={size} value={size}>
              {size}
            </option>
          ))}
        </NativeSelect>
      </label>
      <Button
        size="sm"
        disabled={!hasPreviousPage}
        onClick={() => {
          onPageChange(page - 1);
        }}
      >
        Previous
      </Button>
      <Button
        size="sm"
        disabled={!hasNextPage}
        onClick={() => {
          onPageChange(page + 1);
        }}
      >
        Next
      </Button>
    </nav>
  );
}
