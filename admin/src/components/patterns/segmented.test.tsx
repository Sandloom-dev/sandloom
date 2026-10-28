import { useState } from 'react';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import { Segmented } from './segmented';

function Harness() {
  const [value, setValue] = useState<'a' | 'b'>('a');
  return (
    <Segmented
      label="Source"
      options={[
        { value: 'a', label: 'identity' },
        { value: 'b', label: 'billing' },
      ]}
      value={value}
      onChange={setValue}
    />
  );
}

describe('Segmented', () => {
  it('is a labelled group of pressed-state buttons', async () => {
    render(<Harness />);
    const group = screen.getByRole('group', { name: 'Source' });
    expect(group).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'identity' })).toHaveAttribute(
      'aria-pressed',
      'true',
    );
    await userEvent.click(screen.getByRole('button', { name: 'billing' }));
    expect(screen.getByRole('button', { name: 'billing' })).toHaveAttribute('aria-pressed', 'true');
    expect(screen.getByRole('button', { name: 'identity' })).toHaveAttribute(
      'aria-pressed',
      'false',
    );
  });
});
