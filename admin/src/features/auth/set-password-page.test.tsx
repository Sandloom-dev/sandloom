import { fireEvent, render as renderRaw, screen, waitFor } from '@testing-library/react';
import type { ReactElement } from 'react';
import { describe, expect, it, vi } from 'vitest';
import { ThemeProvider } from '@/hooks/use-theme';
import { api, HttpError } from '@/api/client';
import { readFragment, SetPasswordPage } from './set-password-page';

function render(ui: ReactElement) {
  return renderRaw(<ThemeProvider>{ui}</ThemeProvider>);
}

const PASSWORD = 'Fresh-Admin-Passw0rd!';

function fill(email: string | null, first: string, second: string) {
  if (email !== null)
    fireEvent.change(screen.getByLabelText('Email'), { target: { value: email } });
  fireEvent.change(screen.getByLabelText('New password'), { target: { value: first } });
  fireEvent.change(screen.getByLabelText('New password again'), { target: { value: second } });
  fireEvent.click(screen.getByRole('button', { name: 'Set password and sign in' }));
}

describe('readFragment', () => {
  it('reads the token and the address from the fragment', () => {
    expect(readFragment('#token=a.b-c&email=owner%40example.com')).toEqual({
      token: 'a.b-c',
      email: 'owner@example.com',
    });
    expect(readFragment('')).toEqual({ token: '', email: '' });
  });
});

describe('SetPasswordPage', () => {
  it('sets the password with the token, then signs in', async () => {
    const request = vi.spyOn(api, 'request').mockResolvedValue(null);
    const login = vi.spyOn(api, 'login').mockResolvedValue(undefined);
    render(<SetPasswordPage fragment="#token=tok&email=owner%40example.com" />);
    expect(screen.getByLabelText('Email')).toHaveValue('owner@example.com');
    fill(null, PASSWORD, PASSWORD);
    await waitFor(() => {
      expect(login).toHaveBeenCalledWith(
        { email: 'owner@example.com', password: PASSWORD },
        'admin',
      );
    });
    expect(request).toHaveBeenCalledWith(
      '/auth/reset-password',
      { method: 'POST', body: JSON.stringify({ token: 'tok', new_password: PASSWORD }) },
      false,
    );
  });

  it('refuses two different passwords without calling the server', () => {
    const request = vi.spyOn(api, 'request').mockResolvedValue(null);
    render(<SetPasswordPage fragment="#token=tok&email=owner%40example.com" />);
    fill(null, PASSWORD, 'Something-else-1!');
    expect(screen.getByRole('alert')).toHaveTextContent('The passwords do not match');
    expect(request).not.toHaveBeenCalled();
  });

  it('names the command for a new link when the link expired or was used', async () => {
    vi.spyOn(api, 'request').mockRejectedValue(
      new HttpError('Invalid or expired reset token', 400),
    );
    const login = vi.spyOn(api, 'login');
    render(<SetPasswordPage fragment="#token=tok&email=owner%40example.com" />);
    fill(null, PASSWORD, PASSWORD);
    expect(await screen.findByRole('alert')).toHaveTextContent('This link has expired or was used');
    expect(
      screen.getByText('uv run sandloom admin invite --email owner@example.com'),
    ).toBeVisible();
    expect(login).not.toHaveBeenCalled();
  });

  it('shows the password policy reason', async () => {
    vi.spyOn(api, 'request').mockRejectedValue(new HttpError('Password is too common', 422));
    render(<SetPasswordPage fragment="#token=tok&email=owner%40example.com" />);
    fill(null, PASSWORD, PASSWORD);
    expect(await screen.findByRole('alert')).toHaveTextContent('Password is too common');
  });

  it('without a token, says how to get a link', () => {
    render(<SetPasswordPage fragment="" />);
    expect(screen.getByText(/This page needs the link/)).toBeVisible();
    expect(screen.queryByRole('button', { name: 'Set password and sign in' })).toBeNull();
  });
});
