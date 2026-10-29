import { BrandLockup } from '@/components/brand-mark';
import { useId, useState, type SubmitEvent } from 'react';
import { CircleAlert } from 'lucide-react';
import { api } from '@/api/client';
import { errorMessage } from '@/api/errors';
import { Button } from '@/components/ui/button';
import { Card } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { ThemeMenu } from '@/components/theme-menu';

/** Password (+ optional 2FA) sign-in; only the platform admin role is admitted. */
export function LoginPage() {
  const ids = { email: useId(), password: useId(), otp: useId(), hint: useId() };
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  async function submit(event: SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    const text = (name: string) => {
      const value = data.get(name);
      return typeof value === 'string' ? value : '';
    };
    const otp = text('otp').trim();
    setBusy(true);
    setError(null);
    try {
      await api.login(
        {
          email: text('email'),
          password: text('password'),
          ...(otp ? { otp } : {}),
        },
        'admin',
      );
    } catch (failure) {
      setError(errorMessage(failure, 'Could not sign in'));
      setBusy(false);
    }
  }
  return (
    <main className="grid min-h-dvh place-items-center bg-bg p-4">
      <div className="flex w-full max-w-field flex-col gap-6">
        <div className="flex items-center justify-between gap-4">
          <div className="flex flex-col gap-1">
            <BrandLockup className="h-9 w-44 text-fg" />
            <span className="text-xs text-fg-subtle">Owner console</span>
          </div>
          <ThemeMenu />
        </div>
        <Card className="p-6">
          <h1 className="text-xl font-semibold">Sign in</h1>
          <p className="mt-1 text-fg-muted">Platform administrators only.</p>
          <form className="mt-5 flex flex-col gap-4" onSubmit={(event) => void submit(event)}>
            {error && (
              <p role="alert" className="flex gap-2 rounded-md bg-danger-muted p-3 text-danger">
                <CircleAlert aria-hidden className="mt-0.5 size-4 shrink-0" />
                {error}
              </p>
            )}
            <div className="flex flex-col gap-1.5">
              <Label htmlFor={ids.email}>Email</Label>
              <Input id={ids.email} name="email" type="email" autoComplete="username" required />
            </div>
            <div className="flex flex-col gap-1.5">
              <Label htmlFor={ids.password}>Password</Label>
              <Input
                id={ids.password}
                name="password"
                type="password"
                autoComplete="current-password"
                required
              />
            </div>
            <div className="flex flex-col gap-1.5">
              <Label htmlFor={ids.otp}>Two-factor code</Label>
              <Input
                id={ids.otp}
                name="otp"
                inputMode="numeric"
                autoComplete="one-time-code"
                aria-describedby={ids.hint}
              />
              <p id={ids.hint} className="text-xs text-fg-subtle">
                Only if two-factor authentication is enabled.
              </p>
            </div>
            <Button type="submit" variant="primary" size="lg" loading={busy}>
              Sign in
            </Button>
          </form>
        </Card>
        <p className="text-xs text-fg-subtle">
          Sessions live in this tab only: reloading or closing it signs you out.
        </p>
      </div>
    </main>
  );
}
