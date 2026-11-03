import { BrandLockup } from '@/components/brand-mark';
import { useId, useState, type SubmitEvent } from 'react';
import { CircleAlert } from 'lucide-react';
import { api, HttpError } from '@/api/client';
import { errorMessage } from '@/api/errors';
import { Button } from '@/components/ui/button';
import { Card } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { ThemeMenu } from '@/components/theme-menu';

/** The token and the address of a `sandloom admin create --invite` link (URL fragment). */
export function readFragment(fragment: string): { token: string; email: string } {
  const values = new URLSearchParams(fragment.replace(/^#/, ''));
  return { token: values.get('token') ?? '', email: values.get('email') ?? '' };
}

function inviteCommand(email: string) {
  return `uv run sandloom admin invite --email ${email || 'you@example.com'}`;
}

interface Problem {
  message: string;
  expired?: boolean;
}

/**
 * Set the first password from a single-use link, then sign in.
 *
 * The token travels in the fragment, so it never reaches a server or proxy log;
 * it is sent once, in the body of `POST /auth/reset-password`.
 */
export function SetPasswordPage({ fragment }: { fragment: string }) {
  const ids = { email: useId(), password: useId(), again: useId() };
  const [link] = useState(() => readFragment(fragment));
  const [email, setEmail] = useState(link.email);
  const [problem, setProblem] = useState<Problem | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(event: SubmitEvent<HTMLFormElement>) {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    const text = (name: string) => {
      const value = data.get(name);
      return typeof value === 'string' ? value : '';
    };
    const password = text('password');
    if (password !== text('again')) {
      setProblem({ message: 'The passwords do not match.' });
      return;
    }
    setBusy(true);
    setProblem(null);
    try {
      await api.request(
        '/auth/reset-password',
        { method: 'POST', body: JSON.stringify({ token: link.token, new_password: password }) },
        false,
      );
    } catch (failure) {
      const expired = failure instanceof HttpError && failure.status === 400;
      setProblem(
        expired
          ? { message: 'This link has expired or was used. Ask for a new one:', expired }
          : { message: errorMessage(failure, 'Could not set the password') },
      );
      setBusy(false);
      return;
    }
    try {
      await api.login({ email: email.trim(), password }, 'admin');
    } catch (failure) {
      setProblem({
        message: `The password is set, but signing in failed: ${errorMessage(failure, 'try the sign-in page')}`,
      });
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
          <h1 className="text-xl font-semibold">Set your password</h1>
          {link.token ? (
            <>
              <p className="mt-1 text-fg-muted">The link works once, for 15 minutes.</p>
              <form className="mt-5 flex flex-col gap-4" onSubmit={(event) => void submit(event)}>
                {problem && (
                  <div
                    role="alert"
                    className="flex gap-2 rounded-md bg-danger-muted p-3 text-danger"
                  >
                    <CircleAlert aria-hidden className="mt-0.5 size-4 shrink-0" />
                    <div className="flex flex-col gap-2">
                      <span>{problem.message}</span>
                      {problem.expired && (
                        <code className="font-mono text-xs break-all">{inviteCommand(email)}</code>
                      )}
                    </div>
                  </div>
                )}
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
                  <Label htmlFor={ids.password}>New password</Label>
                  <Input
                    id={ids.password}
                    name="password"
                    type="password"
                    autoComplete="new-password"
                    minLength={8}
                    required
                  />
                </div>
                <div className="flex flex-col gap-1.5">
                  <Label htmlFor={ids.again}>New password again</Label>
                  <Input
                    id={ids.again}
                    name="again"
                    type="password"
                    autoComplete="new-password"
                    minLength={8}
                    required
                  />
                </div>
                <Button type="submit" variant="primary" size="lg" loading={busy}>
                  Set password and sign in
                </Button>
              </form>
            </>
          ) : (
            <div className="mt-3 flex flex-col gap-2 text-fg-muted">
              <p>This page needs the link the command printed. For a new one, run:</p>
              <code className="font-mono text-xs break-all text-fg">{inviteCommand('')}</code>
            </div>
          )}
        </Card>
      </div>
    </main>
  );
}
