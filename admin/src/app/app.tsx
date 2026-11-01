import { useEffect, useState } from 'react';
import { BrowserRouter } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { commandRecovery } from '@/hooks/command-recovery';
import { CommandRecovery } from '@/components/patterns/command-recovery';
import { Toaster } from '@/components/ui/toast';
import { TooltipProvider } from '@/components/ui/tooltip';
import { LoginPage } from '@/features/auth/login-page';
import { ThemeProvider } from '@/hooks/use-theme';
import type { AdminConfig } from './config';
import { ConfigProvider } from './config-context';
import { AppRoutes } from './routes';
import { useSession } from './session';
import { AppShell } from './shell';

/**
 * Everything server-derived lives under one QueryClient per session epoch:
 * login, logout, revocation and authority changes discard every cached record
 * and cancel in-flight requests (late responses are rejected by ApiClient).
 */
function SessionScope({ signedIn }: { signedIn: boolean }) {
  const [queryClient] = useState(
    () =>
      new QueryClient({
        defaultOptions: {
          queries: { retry: false, refetchOnWindowFocus: false, staleTime: 5_000 },
          mutations: { retry: false },
        },
      }),
  );
  useEffect(() => {
    commandRecovery(queryClient).activate();
    return () => {
      commandRecovery(queryClient).dispose();
      void queryClient.cancelQueries();
      queryClient.clear();
    };
  }, [queryClient]);
  if (!signedIn) return <LoginPage />;
  return (
    <QueryClientProvider client={queryClient}>
      <AppShell>
        <CommandRecovery />
        <AppRoutes />
      </AppShell>
    </QueryClientProvider>
  );
}

function SessionRoot() {
  const { epoch, session } = useSession();
  return <SessionScope key={epoch} signedIn={session !== undefined} />;
}

export function App({ config }: { config: AdminConfig }) {
  return (
    <ConfigProvider config={config}>
      <ThemeProvider>
        <TooltipProvider delayDuration={300}>
          <BrowserRouter basename={import.meta.env.BASE_URL}>
            <SessionRoot />
          </BrowserRouter>
          <Toaster />
        </TooltipProvider>
      </ThemeProvider>
    </ConfigProvider>
  );
}
