import { useState } from 'react';
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { ColumnDef, RowSelectionState } from '@tanstack/react-table';
import { describe, expect, it, vi } from 'vitest';
import { Button } from '@/components/ui/button';
import { DataTable, type SortState } from './data-table';

interface Row {
  id: string;
  name: string;
}
const columns: ColumnDef<Row>[] = [{ id: 'name', header: 'Name', accessorFn: (row) => row.name }];
const rows: Row[] = [
  { id: 'a', name: 'Avery' },
  { id: 'b', name: 'Blake' },
];

function Harness({ onBulk = vi.fn() }: { onBulk?: (rows: Row[]) => void }) {
  const [selection, setSelection] = useState<RowSelectionState>({});
  const [sort, setSort] = useState<SortState | null>(null);
  return (
    <DataTable
      label="People"
      columns={columns}
      data={rows}
      getRowId={(row) => row.id}
      status="success"
      empty={<p>Nothing</p>}
      sortable={['name']}
      sort={sort}
      onSortChange={setSort}
      selection={{
        value: selection,
        onChange: setSelection,
        actions: (selected) => (
          <Button
            onClick={() => {
              onBulk(selected);
            }}
          >
            Act
          </Button>
        ),
      }}
    />
  );
}

describe('DataTable', () => {
  it('cycles server sort and exposes aria-sort', async () => {
    render(<Harness />);
    const header = screen.getByRole('columnheader', { name: /Name/ });
    expect(header).not.toHaveAttribute('aria-sort');
    await userEvent.click(within(header).getByRole('button'));
    expect(header).toHaveAttribute('aria-sort', 'ascending');
    await userEvent.click(within(header).getByRole('button'));
    expect(header).toHaveAttribute('aria-sort', 'descending');
    await userEvent.click(within(header).getByRole('button'));
    expect(header).not.toHaveAttribute('aria-sort');
  });

  it('selects rows and hands the selected records to bulk actions', async () => {
    const onBulk = vi.fn();
    render(<Harness onBulk={onBulk} />);
    expect(screen.queryByRole('region', { name: 'Bulk actions' })).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole('checkbox', { name: 'Select row 2' }));
    const bar = screen.getByRole('region', { name: 'Bulk actions' });
    expect(bar).toHaveTextContent('1 selected');
    await userEvent.click(within(bar).getByRole('button', { name: 'Act' }));
    expect(onBulk).toHaveBeenCalledWith([rows[1]]);
    await userEvent.click(screen.getByRole('checkbox', { name: 'Select all rows on this page' }));
    expect(bar).toHaveTextContent('2 selected');
  });

  it('shows loading, empty and error states', () => {
    const { rerender } = render(
      <DataTable
        label="People"
        columns={columns}
        data={undefined}
        getRowId={(r) => r.id}
        status="pending"
        empty={<p>Nothing</p>}
      />,
    );
    expect(screen.getByLabelText('Loading People')).toHaveAttribute('aria-busy', 'true');
    rerender(
      <DataTable
        label="People"
        columns={columns}
        data={[]}
        getRowId={(r) => r.id}
        status="success"
        empty={<p>Nothing</p>}
      />,
    );
    expect(screen.getByText('Nothing')).toBeInTheDocument();
    rerender(
      <DataTable
        label="People"
        columns={columns}
        data={undefined}
        getRowId={(r) => r.id}
        status="error"
        error={new Error('boom')}
        empty={<p>Nothing</p>}
      />,
    );
    expect(screen.getByRole('alert')).toHaveTextContent('Could not load this data');
  });
});
