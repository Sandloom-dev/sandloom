import { BrandLockup } from '@/components/brand-mark';
import { useEffect, useId, useRef, useState, type SubmitEvent } from 'react';
import { CircleAlert, Terminal } from 'lucide-react';
import { api, needsSecondFactor } from '@/api/client';
import { errorMessage } from '@/api/errors';
import { Button } from '@/components/ui/button';
import { Card } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { ThemeMenu } from '@/components/theme-menu';

const CREATE_COMMAND = 'uv run sandloom admin create --email you@example.com';

function Alert({ message }: { message: string }) {
  return (
    <p role="alert" className="flex gap-2 rounded-md bg-danger-muted p-3 text-danger">
      <CircleAlert aria-hidden className="mt-0.5 size-4 shrink-0" />
      {message}
    </p>
  );
}

/** Whether the app still has no administrator (outside production only; else unknown). */
function useNoAdminYet(): boolean {
  const [none, setNone] = useState(false);
  useEffect(() => {
    let live = true;
    api
      .request<{ has_admin?: unknown }>('/api/v1/identity/setup-state', {}, false)
      .then((state) => {
        if (live) setNone(state.has_admin === false);
      })
      .catch(() => undefined);
    return () => {
      live = false;
    };
  }, []);
  return none;
}

/**
 * Outside production, after a failed sign-in: the commands that show which
 * addresses are administrators and give one a new password. Production
 * keeps the plain error (it says nothing about accounts).
 */
function AccountHint({ email }: { email: string }) {
  return (
    <div className="flex flex-col gap-1 rounded-md bg-surface-muted p-3 text-xs">
      <p className="text-fg-muted">
        Forgot which address or password? In the app&apos;s directory:
      </p>
      <code className="font-mono break-all">uv run sandloom admin list</code>
      <code className="font-mono break-all">
        {`uv run sandloom admin invite --email ${email || 'you@example.com'}`}
      </code>
    </div>
  );
}

interface Credentials {
  email: string;
  password: string;
}

/**
 * Two steps: email and password, then — only when identity answers that the
 * account has two-factor authentication — the code (or a backup code). Only
 * the platform admin role is admitted.
 *
 * A first attempt without a code is not a failed login on identity's side
 * (it answers after the password verified), so sending the password again
 * with the code does not count twice towards a lockout. The password stays
 * in this component's memory only until the second step ends.
 */
export function LoginPage() {
  const noAdminYet = useNoAdminYet();
  const ids = { email: useId(), password: useId(), otp: useId(), hint: useId() };
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [email, setEmail] = useState('');
  const [pending, setPending] = useState<Credentials | null>(null);
  const codeInput = useRef<HTMLInputElement>(null);
  useEffect(() => {
    if (pending) codeInput.current?.focus();
  }, [pending]);

  async function signIn(credentials: Credentials, otp?: string) {
    setBusy(true);
    setError(null);
    try {
      await api.login({ ...credentials, ...(otp ? { otp } : {}) }, 'admin');
    } catch (failure) {
      if (!otp && needsSecondFactor(failure)) {
        setPending(credentials);
      } else {
        setError(errorMessage(failure, 'Could not sign in'));
      }
      setBusy(false);
    }
  }

  function field(event: SubmitEvent<HTMLFormElement>, name: string): string {
    const value = new FormData(event.currentTarget).get(name);
    return typeof value === 'string' ? value : '';
  }

  function back() {
    setPending(null);
    setError(null);
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
          <h1 className="text-xl font-semibold">{pending ? 'Two-factor code' : 'Sign in'}</h1>
          <p className="mt-1 text-fg-muted">
            {pending ? `Signing in as ${pending.email}.` : 'Platform administrators only.'}
          </p>
          {pending ? (
            <form
              key="code"
              className="mt-5 flex flex-col gap-4"
              onSubmit={(event) => {
                event.preventDefault();
                void signIn(pending, field(event, 'otp').trim());
              }}
            >
              {error && <Alert message={error} />}
              <div className="flex flex-col gap-1.5">
                <Label htmlFor={ids.otp}>Two-factor code</Label>
                <Input
                  id={ids.otp}
                  name="otp"
                  autoComplete="one-time-code"
                  autoCapitalize="off"
                  spellCheck={false}
                  required
                  ref={codeInput}
                  aria-describedby={ids.hint}
                />
                <p id={ids.hint} className="text-xs text-fg-subtle">
                  The code from your authenticator app. Lost it? Enter one of your backup codes
                  instead.
                </p>
              </div>
              <Button type="submit" variant="primary" size="lg" loading={busy}>
                Sign in
              </Button>
              <Button type="button" variant="ghost" disabled={busy} onClick={back}>
                Back
              </Button>
            </form>
          ) : (
            <form
              key="password"
              className="mt-5 flex flex-col gap-4"
              onSubmit={(event) => {
                event.preventDefault();
                const credentials = { email: email.trim(), password: field(event, 'password') };
                void signIn(credentials);
              }}
            >
              {error && <Alert message={error} />}
              {error && api.keepsAcrossReloads && <AccountHint email={email.trim()} />}
              <div className="flex flex-col gap-1.5">
                <Label htmlFor={ids.email}>Email</Label>
                <Input
                  id={ids.email}
                  name="email"
                  type="email"
                  autoComplete="username"
                  required
                  value={email}
                  onChange={(event) => {
                    setEmail(event.target.value);
                  }}
                />
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
              <Button type="submit" variant="primary" size="lg" loading={busy}>
                Sign in
              </Button>
            </form>
          )}
        </Card>
        {noAdminYet && (
          <Card className="flex gap-3 p-4">
            <Terminal aria-hidden className="mt-0.5 size-4 shrink-0 text-fg-muted" />
            <div className="flex min-w-0 flex-col gap-2">
              <p className="font-medium">No administrator yet</p>
              <p className="text-fg-muted">Create the first one in the app&apos;s directory:</p>
              <code className="font-mono text-xs break-all">{CREATE_COMMAND}</code>
            </div>
          </Card>
        )}
        <p className="text-xs text-fg-subtle">
          {api.keepsAcrossReloads
            ? 'Development: reloading keeps you signed in; closing the tab signs you out.'
            : 'Sessions live in this tab only: reloading or closing it signs you out.'}
        </p>
      </div>
    </main>
  );
}
