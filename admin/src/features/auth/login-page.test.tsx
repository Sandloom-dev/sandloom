import { render as renderRaw, screen, waitFor } from '@testing-library/react';
import type { ReactElement } from 'react';
import { describe, expect, it, vi } from 'vitest';
import { ThemeProvider } from '@/hooks/use-theme';
import { api, HttpError } from '@/api/client';
import { LoginPage } from './login-page';

function render(ui: ReactElement) {
  return renderRaw(<ThemeProvider>{ui}</ThemeProvider>);
}

const COMMAND = 'uv run sandloom admin create --email you@example.com';

describe('LoginPage first-run hint', () => {
  it('shows the create command while no administrator exists', async () => {
    const request = vi.spyOn(api, 'request').mockResolvedValue({ has_admin: false });
    render(<LoginPage />);
    expect(await screen.findByText(COMMAND)).toBeVisible();
    expect(request).toHaveBeenCalledWith('/api/v1/identity/setup-state', {}, false);
  });

  it('shows nothing once an administrator exists', async () => {
    const request = vi.spyOn(api, 'request').mockResolvedValue({ has_admin: true });
    render(<LoginPage />);
    await waitFor(() => {
      expect(request).toHaveBeenCalled();
    });
    expect(screen.queryByText(COMMAND)).toBeNull();
  });

  it('shows nothing when the server does not tell (production)', async () => {
    const request = vi.spyOn(api, 'request').mockRejectedValue(new HttpError('Not found', 404));
    render(<LoginPage />);
    await waitFor(() => {
      expect(request).toHaveBeenCalled();
    });
    expect(screen.queryByText(COMMAND)).toBeNull();
  });
});
