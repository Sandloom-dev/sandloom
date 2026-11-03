import { fireEvent, render as renderRaw, screen, waitFor } from '@testing-library/react';
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

const PASSWORD = 'Owner-Passw0rd!';

function twoFactorRequired() {
  return new HttpError(
    '2FA code required',
    401,
    { detail: '2FA code required' },
    new Headers({ 'X-2FA-Required': 'true' }),
  );
}

function signIn(email: string, password: string) {
  fireEvent.change(screen.getByLabelText('Email'), { target: { value: email } });
  fireEvent.change(screen.getByLabelText('Password'), { target: { value: password } });
  fireEvent.click(screen.getByRole('button', { name: 'Sign in' }));
}

describe('LoginPage sign-in steps', () => {
  it('shows only email and password first', () => {
    vi.spyOn(api, 'request').mockResolvedValue({ has_admin: true });
    render(<LoginPage />);
    expect(screen.getByLabelText('Email')).toBeVisible();
    expect(screen.getByLabelText('Password')).toBeVisible();
    expect(screen.queryByLabelText('Two-factor code')).toBeNull();
  });

  it('signs in at once without two-factor authentication', async () => {
    vi.spyOn(api, 'request').mockResolvedValue({ has_admin: true });
    const login = vi.spyOn(api, 'login').mockResolvedValue(undefined);
    render(<LoginPage />);
    signIn('owner@example.com', PASSWORD);
    await waitFor(() => {
      expect(login).toHaveBeenCalledTimes(1);
    });
    expect(login).toHaveBeenCalledWith({ email: 'owner@example.com', password: PASSWORD }, 'admin');
    expect(screen.queryByLabelText('Two-factor code')).toBeNull();
  });

  it('asks for the code only when identity requires it, then signs in', async () => {
    vi.spyOn(api, 'request').mockResolvedValue({ has_admin: true });
    const login = vi
      .spyOn(api, 'login')
      .mockRejectedValueOnce(twoFactorRequired())
      .mockResolvedValueOnce(undefined);
    render(<LoginPage />);
    signIn('owner@example.com', PASSWORD);
    const code = await screen.findByLabelText('Two-factor code');
    expect(screen.queryByLabelText('Password')).toBeNull();
    expect(screen.queryByRole('alert')).toBeNull();
    expect(screen.getByText(/backup codes/)).toBeVisible();
    await waitFor(() => {
      expect(code).toHaveFocus();
    });
    fireEvent.change(code, { target: { value: ' 123456 ' } });
    fireEvent.click(screen.getByRole('button', { name: 'Sign in' }));
    await waitFor(() => {
      expect(login).toHaveBeenCalledTimes(2);
    });
    expect(login).toHaveBeenNthCalledWith(
      2,
      { email: 'owner@example.com', password: PASSWORD, otp: '123456' },
      'admin',
    );
  });

  it('keeps the code step on a wrong code and goes back on request', async () => {
    vi.spyOn(api, 'request').mockResolvedValue({ has_admin: true });
    vi.spyOn(api, 'login')
      .mockRejectedValueOnce(twoFactorRequired())
      .mockRejectedValueOnce(new HttpError('Invalid 2FA code', 401));
    render(<LoginPage />);
    signIn('owner@example.com', PASSWORD);
    fireEvent.change(await screen.findByLabelText('Two-factor code'), {
      target: { value: '000000' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Sign in' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Invalid 2FA code');
    expect(screen.getByLabelText('Two-factor code')).toBeVisible();
    fireEvent.click(screen.getByRole('button', { name: 'Back' }));
    expect(screen.getByLabelText('Email')).toHaveValue('owner@example.com');
    expect(screen.getByLabelText('Password')).toHaveValue('');
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('shows a wrong password on the first step', async () => {
    vi.spyOn(api, 'request').mockResolvedValue({ has_admin: true });
    vi.spyOn(api, 'login').mockRejectedValue(new HttpError('Invalid credentials', 401));
    render(<LoginPage />);
    signIn('owner@example.com', 'nope');
    expect(await screen.findByRole('alert')).toHaveTextContent('Invalid credentials');
    expect(screen.queryByLabelText('Two-factor code')).toBeNull();
  });
});

describe('LoginPage outside production', () => {
  it('says a reload keeps the session and, after a failed sign-in, how to find the account', async () => {
    vi.spyOn(api, 'request').mockResolvedValue({ has_admin: true });
    vi.spyOn(api, 'keepsAcrossReloads', 'get').mockReturnValue(true);
    vi.spyOn(api, 'login').mockRejectedValue(new HttpError('Incorrect email or password', 401));
    render(<LoginPage />);
    expect(screen.getByText(/reloading keeps you signed in/)).toBeVisible();
    expect(screen.queryByText(/admin list/)).toBeNull();
    signIn('owner@example.com', 'nope');
    expect(await screen.findByRole('alert')).toHaveTextContent('Incorrect email or password');
    expect(screen.getByText('uv run sandloom admin list')).toBeVisible();
    expect(
      screen.getByText('uv run sandloom admin invite --email owner@example.com'),
    ).toBeVisible();
  });

  it('production keeps the session in this tab only and the plain error', async () => {
    vi.spyOn(api, 'request').mockResolvedValue({ has_admin: true });
    vi.spyOn(api, 'keepsAcrossReloads', 'get').mockReturnValue(false);
    vi.spyOn(api, 'login').mockRejectedValue(new HttpError('Incorrect email or password', 401));
    render(<LoginPage />);
    expect(
      screen.getByText('Sessions live in this tab only: reloading or closing it signs you out.'),
    ).toBeVisible();
    signIn('owner@example.com', 'nope');
    expect(await screen.findByRole('alert')).toHaveTextContent('Incorrect email or password');
    expect(screen.queryByText(/admin list/)).toBeNull();
  });
});
