import { useState } from 'react';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import type { CommandState } from '@/hooks/use-command';
import { ConfirmDialog } from './confirm-dialog';

function Harness({
  state = 'idle',
  onConfirm = vi.fn(),
  typed = null,
  error = null,
}: {
  state?: CommandState;
  onConfirm?: () => void;
  typed?: string | null;
  error?: string | null;
}) {
  const [open, setOpen] = useState(true);
  return (
    <ConfirmDialog
      open={open}
      onOpenChange={setOpen}
      title="Block user"
      description="Emergency stop."
      confirmLabel="Block"
      tone="danger"
      typedConfirmation={typed}
      reference="ref-1"
      state={state}
      errorMessage={error}
      onConfirm={onConfirm}
    />
  );
}

describe('ConfirmDialog', () => {
  it('is an accessible alert dialog showing the operation reference', () => {
    render(<Harness />);
    expect(screen.getByRole('alertdialog', { name: 'Block user' })).toBeInTheDocument();
    expect(screen.getByText('Emergency stop.')).toBeInTheDocument();
    expect(screen.getByText('ref-1')).toBeInTheDocument();
  });

  it('enables the action only after the exact value is retyped', async () => {
    const onConfirm = vi.fn();
    render(<Harness typed="riley@example.test" onConfirm={onConfirm} />);
    const confirm = screen.getByRole('button', { name: 'Block' });
    expect(confirm).toBeDisabled();
    await userEvent.type(screen.getByLabelText(/to confirm/), 'riley@example');
    expect(confirm).toBeDisabled();
    await userEvent.type(screen.getByLabelText(/to confirm/), '.test');
    expect(confirm).toBeEnabled();
    await userEvent.click(confirm);
    expect(onConfirm).toHaveBeenCalledOnce();
  });

  it('cannot be dismissed while the command is in flight', async () => {
    render(<Harness state="pending" />);
    await userEvent.keyboard('{Escape}');
    expect(screen.getByRole('alertdialog')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Cancel' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Block' })).toHaveAttribute('aria-busy', 'true');
  });

  it('renders server errors inline', () => {
    render(<Harness state="error" error="Operators cannot block themselves" />);
    expect(screen.getByRole('alert')).toHaveTextContent('Operators cannot block themselves');
  });

  it('offers only a same-reference retry after an unknown outcome', async () => {
    const onConfirm = vi.fn();
    render(<Harness state="unknown" onConfirm={onConfirm} />);
    expect(screen.queryByRole('button', { name: 'Block' })).not.toBeInTheDocument();
    expect(screen.getByRole('alert')).toHaveTextContent(/could not confirm/);
    await userEvent.click(screen.getByRole('button', { name: 'Retry with same reference' }));
    expect(onConfirm).toHaveBeenCalledOnce();
  });
});
