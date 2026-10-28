import { useState } from 'react';
import type { Meta, StoryObj } from '@storybook/react-vite';
import type { ColumnDef, RowSelectionState } from '@tanstack/react-table';
import { Home, Users } from 'lucide-react';
import { HttpError } from '@/api/client';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import type { CommandState } from '@/hooks/use-command';
import type { Field } from '@/schema/types';
import { CommandPalette } from './command-palette';
import { ConfirmDialog } from './confirm-dialog';
import { CopyText } from './copy-text';
import { DataTable, type SortState } from './data-table';
import { DescriptionList } from './description-list';
import { EntityHeader } from './entity-header';
import { FilterBar, type FilterValues } from './filter-bar';
import { PageHeader } from './page-header';
import { SavedViews, type SavedView } from './saved-views';
import { Segmented } from './segmented';
import { EmptyState, ErrorState, LoadingRows, UnknownOutcome } from './states';
import { Timeline } from './timeline';

const meta: Meta = { title: 'Patterns' };
export default meta;
type Story = StoryObj;

interface Person {
  id: string;
  name: string;
  email: string;
  status: string;
}
const people: Person[] = [
  {
    id: '5f2c8a1d-0000-4000-8000-0000000091ab',
    name: 'Avery Chen',
    email: 'avery@northwind.example',
    status: 'active',
  },
  {
    id: 'a8104c07-0000-4000-8000-000000004c07',
    name: 'Riley Moss',
    email: 'riley@northwind.example',
    status: 'blocked',
  },
];
const columns: ColumnDef<Person>[] = [
  { id: 'name', header: 'Name', accessorFn: (p) => p.name },
  { id: 'email', header: 'Email', accessorFn: (p) => p.email },
  {
    id: 'status',
    header: 'Status',
    cell: ({ row }) => (
      <Badge tone={row.original.status === 'blocked' ? 'danger' : 'success'} dot>
        {row.original.status}
      </Badge>
    ),
  },
  { id: 'id', header: 'ID', cell: ({ row }) => <CopyText value={row.original.id} short /> },
];

function TableDemo({ status }: { status: 'pending' | 'error' | 'success' }) {
  const [selection, setSelection] = useState<RowSelectionState>({});
  const [sort, setSort] = useState<SortState | null>({ field: 'name', desc: false });
  return (
    <DataTable
      label="Users"
      columns={columns}
      data={status === 'success' ? people : undefined}
      getRowId={(row) => row.id}
      status={status}
      error={new HttpError('Billing is unreachable', 503)}
      onRetry={() => undefined}
      empty={<EmptyState title="No users" />}
      sortable={['name']}
      sort={sort}
      onSortChange={setSort}
      pagination={{
        page: 1,
        pageSize: 25,
        pageSizes: [10, 25, 50],
        hasNextPage: true,
        hasPreviousPage: false,
        onPageChange: () => undefined,
        onPageSizeChange: () => undefined,
      }}
      selection={{
        value: selection,
        onChange: setSelection,
        actions: () => (
          <Button size="sm" variant="danger-outline">
            Block…
          </Button>
        ),
      }}
    />
  );
}

export const DataTableLoaded: Story = { render: () => <TableDemo status="success" /> };
export const DataTableLoading: Story = { render: () => <TableDemo status="pending" /> };
export const DataTableError: Story = { render: () => <TableDemo status="error" /> };
export const DataTableEmpty: Story = {
  render: () => (
    <DataTable
      label="Users"
      columns={columns}
      data={[]}
      getRowId={(row) => row.id}
      status="success"
      empty={<EmptyState title="No matches" description="Nothing matches these filters." />}
    />
  ),
};

const filterFields: Field[] = [
  { name: 'q', label: 'Search', type: 'string', required: false, nullable: true },
  { name: 'is_active', label: 'Active', type: 'boolean', required: false, nullable: true },
  {
    name: 'status',
    label: 'Status',
    type: 'string',
    required: false,
    nullable: true,
    enum: ['active', 'past_due', 'canceled'],
  },
];

export const Filters: Story = {
  render: function Render() {
    const [values, setValues] = useState<FilterValues>({ is_active: 'true' });
    const [views, setViews] = useState<SavedView[]>([
      { name: 'Active', filters: { is_active: 'true' } },
    ]);
    return (
      <div className="flex flex-col gap-3">
        <SavedViews
          views={views}
          current={values}
          onSelect={setValues}
          onSave={(name) => {
            setViews([...views, { name, filters: values }]);
          }}
          onDelete={(name) => {
            setViews(views.filter((view) => view.name !== name));
          }}
        />
        <FilterBar fields={filterFields} values={values} onChange={setValues} />
        <pre className="font-mono text-xs text-fg-subtle">{JSON.stringify(values)}</pre>
      </div>
    );
  },
};

function DialogDemo({ state, typed }: { state: CommandState; typed?: string }) {
  const [open, setOpen] = useState(true);
  return (
    <>
      <Button
        onClick={() => {
          setOpen(true);
        }}
      >
        Open
      </Button>
      <ConfirmDialog
        open={open}
        onOpenChange={setOpen}
        title="Block user"
        description="Emergency stop: every request is rejected and all sessions end."
        confirmLabel="Block user"
        tone="danger"
        typedConfirmation={typed ?? null}
        reference="8c41f0e2-6b7d-4f5e-9a10-3d2b7c1e5a90"
        state={state}
        errorMessage="Operators cannot apply this to their own account"
        onConfirm={() => undefined}
      />
    </>
  );
}
export const ConfirmTyped: Story = {
  render: () => <DialogDemo state="idle" typed="riley@northwind.example" />,
};
export const ConfirmPending: Story = { render: () => <DialogDemo state="pending" /> };
export const ConfirmError: Story = { render: () => <DialogDemo state="error" /> };
export const ConfirmUnknownOutcome: Story = { render: () => <DialogDemo state="unknown" /> };

export const States: Story = {
  render: () => (
    <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
      <div className="rounded-lg border border-border bg-surface">
        <EmptyState
          title="No blocked users"
          description="Blocks appear here the moment they are applied."
          action={<Button>Clear filters</Button>}
        />
      </div>
      <div className="rounded-lg border border-border bg-surface">
        <LoadingRows rows={4} />
      </div>
      <div className="rounded-lg border border-border bg-surface">
        <ErrorState
          error={new HttpError('The billing service did not answer', 503)}
          onRetry={() => undefined}
        />
      </div>
      <UnknownOutcome
        what="the credit adjustment was applied"
        reference="2d9c1a70-0000-4000-8000-000000004be8"
        onRetry={() => undefined}
      />
    </div>
  ),
};

export const EntityAndTimeline: Story = {
  render: () => (
    <div className="flex flex-col gap-6">
      <PageHeader
        title="Users"
        description="Customers"
        actions={<Button variant="primary">New</Button>}
      />
      <EntityHeader
        title="Riley Moss"
        subtitle={
          <>
            <span>riley@northwind.example</span>
            <CopyText value="a8104c07-0000-4000-8000-000000004c07" />
          </>
        }
        badges={<Badge tone="danger">Blocked</Badge>}
        actions={<Button variant="primary">Unblock</Button>}
      />
      <DescriptionList
        items={[
          { key: 'email', label: 'Email', value: 'riley@northwind.example' },
          { key: 'created', label: 'Created', value: 'Aug 30, 2026' },
        ]}
      />
      <Timeline
        items={[
          {
            id: '1',
            title: 'Morgan Ortiz blocked this user',
            meta: 'auth.user.blocked',
            at: new Date(Date.now() - 7_200_000).toISOString(),
            tone: 'danger',
          },
          {
            id: '2',
            title: 'Role Support assigned',
            meta: 'identity.role.assigned',
            at: new Date(Date.now() - 86_400_000).toISOString(),
            tone: 'success',
          },
          {
            id: '3',
            title: 'Account created',
            at: new Date(Date.now() - 864_000_000).toISOString(),
          },
        ]}
      />
    </div>
  ),
};

export const Palette: Story = {
  render: function Render() {
    const [open, setOpen] = useState(true);
    const [query, setQuery] = useState('');
    return (
      <>
        <Button
          onClick={() => {
            setOpen(true);
          }}
        >
          Open palette
        </Button>
        <CommandPalette
          open={open}
          onOpenChange={setOpen}
          query={query}
          onQueryChange={setQuery}
          groups={[
            {
              heading: 'Go to',
              items: [
                { id: 'home', label: 'Overview', icon: Home, onSelect: () => undefined },
                { id: 'users', label: 'Customers › Users', icon: Users, onSelect: () => undefined },
              ],
            },
          ]}
          footer={<span>↵ open</span>}
        />
      </>
    );
  },
};

export const SegmentedControl: Story = {
  render: function Render() {
    const [value, setValue] = useState<'identity' | 'tenancy' | 'billing'>('billing');
    return (
      <Segmented
        label="Source"
        options={[
          { value: 'identity', label: 'identity' },
          { value: 'tenancy', label: 'tenancy' },
          { value: 'billing', label: 'billing' },
        ]}
        value={value}
        onChange={setValue}
      />
    );
  },
};
