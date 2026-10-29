import { BrandLockup, BrandMark } from '@/components/brand-mark';
import { useMemo, useState, type ReactNode } from 'react';
import { NavLink, Link, useLocation } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { Dialog as DialogPrimitive } from 'radix-ui';
import { BookOpen, LogOut, Menu, Monitor, Moon, Search, Sun } from 'lucide-react';
import { api } from '@/api/client';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';
import { Kbd } from '@/components/ui/kbd';
import { useProviderInfo } from '@/features/billing/provider';
import { modKeyLabel } from '@/hooks/use-hotkey';
import { useTheme, type ThemePreference } from '@/hooks/use-theme';
import { cn } from '@/lib/cn';
import { initials } from '@/lib/format';
import { useConfig } from './config-context';
import { GlobalPalette } from './global-palette';
import { buildNavigation, type NavGroup } from './navigation';
import { useSession } from './session';

function Brand() {
  return (
    <div className="flex flex-col gap-1 px-2 py-1.5">
      <BrandLockup className="h-8 w-36 shrink-0 text-fg" />
      <span className="text-xs text-fg-subtle">Owner console</span>
    </div>
  );
}

function Navigation({ groups, onNavigate }: { groups: NavGroup[]; onNavigate?: () => void }) {
  return (
    <nav aria-label="Primary" className="flex flex-col gap-4">
      {groups.map((group) => (
        <div key={group.label ?? 'root'} className="flex flex-col gap-px">
          {group.label && (
            <span className="px-2.5 pb-1 text-xs font-semibold tracking-wide text-fg-subtle uppercase">
              {group.label}
            </span>
          )}
          {group.items.map((item) => {
            const Icon = item.icon;
            return (
              <NavLink
                key={item.to}
                to={item.to}
                end={item.to === '/'}
                onClick={onNavigate}
                className={({ isActive }) =>
                  cn(
                    'flex h-control items-center gap-2.5 rounded-md px-2.5 text-fg-muted transition-colors motion-fast hover:bg-surface-hover hover:text-fg',
                    isActive && 'bg-accent-muted font-semibold text-fg',
                  )
                }
              >
                <Icon aria-hidden className="size-4 shrink-0" />
                <span className="truncate">{item.label}</span>
              </NavLink>
            );
          })}
        </div>
      ))}
    </nav>
  );
}

function SearchButton({ onClick }: { onClick: () => void }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="flex h-control w-full items-center gap-2 rounded-md border border-border bg-surface-muted px-2.5 text-fg-subtle hover:border-border-strong"
    >
      <Search aria-hidden className="size-4" />
      <span className="flex-1 truncate text-left">Search or jump to…</span>
      <Kbd>{modKeyLabel}K</Kbd>
    </button>
  );
}

const THEMES: { value: ThemePreference; label: string; icon: typeof Sun }[] = [
  { value: 'light', label: 'Light', icon: Sun },
  { value: 'dark', label: 'Dark', icon: Moon },
  { value: 'system', label: 'System', icon: Monitor },
];

function UserMenu() {
  const { theme, setTheme } = useTheme();
  const me = useQuery({
    queryKey: ['me', api.snapshot?.subject],
    queryFn: () => api.me(),
    staleTime: 60_000,
  });
  const name = me.data?.full_name ?? 'Operator';
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <button
          type="button"
          aria-label={`${initials(name)} — account menu for ${name}`}
          className="grid size-control place-items-center rounded-full bg-accent-muted text-xs font-semibold text-accent"
        >
          {initials(name)}
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end">
        <DropdownMenuLabel>
          <span className="block text-sm text-fg">{name}</span>
          <span className="block font-normal">{me.data?.email}</span>
        </DropdownMenuLabel>
        <DropdownMenuSeparator />
        {THEMES.map(({ value, label, icon: Icon }) => (
          <DropdownMenuItem
            key={value}
            onSelect={() => {
              setTheme(value);
            }}
          >
            <Icon aria-hidden />
            {label}
            {theme === value && <span className="ml-auto text-xs text-fg-subtle">current</span>}
          </DropdownMenuItem>
        ))}
        <DropdownMenuSeparator />
        <DropdownMenuItem asChild>
          <Link to="/help">
            <BookOpen aria-hidden />
            Operator guide
          </Link>
        </DropdownMenuItem>
        <DropdownMenuItem
          tone="danger"
          onSelect={() => {
            void api.logout().catch(() => undefined);
          }}
        >
          <LogOut aria-hidden />
          Sign out
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

function EnvironmentBadge() {
  const { modules } = useConfig();
  const info = useProviderInfo();
  if (!modules.has('billing') || !info.data) return null;
  const live = info.data.mode === 'live';
  return (
    <Badge tone={live ? 'warning' : 'outline'} dot>
      {live ? 'Live payments' : info.data.mode === 'test' ? 'Test payments' : 'Demo payments'}
    </Badge>
  );
}

/** Sidebar + topbar + ⌘K. Navigation and grouping are derived from the manifest. */
export function AppShell({ children }: { children: ReactNode }) {
  const { manifest } = useConfig();
  const { session } = useSession();
  const location = useLocation();
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const navigation = useMemo(
    () => buildNavigation(manifest, session?.roles ?? []),
    [manifest, session],
  );
  const current = navigation
    .flatMap((group) => group.items.map((item) => ({ group: group.label, item })))
    .filter(({ item }) => item.to !== '/' && location.pathname.startsWith(item.to))
    .sort((a, b) => b.item.to.length - a.item.to.length)[0];
  return (
    <div className="flex min-h-dvh">
      <a
        href="#main"
        className="sr-only z-toast rounded-md bg-surface px-3 py-2 shadow-md focus:not-sr-only focus:fixed focus:top-2 focus:left-2"
      >
        Skip to content
      </a>
      <aside className="sticky top-0 hidden h-dvh w-sidebar shrink-0 flex-col gap-4 overflow-y-auto border-r border-border bg-surface p-3 lg:flex">
        <Brand />
        <SearchButton
          onClick={() => {
            setPaletteOpen(true);
          }}
        />
        <Navigation groups={navigation} />
      </aside>
      <div className="flex min-w-0 flex-1 flex-col">
        <header className="sticky top-0 z-sticky flex h-topbar items-center gap-2 border-b border-border bg-surface px-4 lg:px-6">
          <DialogPrimitive.Root open={drawerOpen} onOpenChange={setDrawerOpen}>
            <DialogPrimitive.Trigger asChild>
              <Button
                variant="ghost"
                size="icon"
                aria-label="Open navigation"
                className="lg:hidden"
              >
                <Menu aria-hidden />
              </Button>
            </DialogPrimitive.Trigger>
            <DialogPrimitive.Portal>
              <DialogPrimitive.Overlay className="fixed inset-0 z-overlay bg-overlay" />
              <DialogPrimitive.Content
                aria-describedby={undefined}
                className="fixed inset-y-0 left-0 z-dialog flex w-sidebar animate-fade-in flex-col gap-4 overflow-y-auto bg-surface p-3 shadow-lg"
              >
                <DialogPrimitive.Title className="sr-only">Navigation</DialogPrimitive.Title>
                <Brand />
                <Navigation
                  groups={navigation}
                  onNavigate={() => {
                    setDrawerOpen(false);
                  }}
                />
              </DialogPrimitive.Content>
            </DialogPrimitive.Portal>
          </DialogPrimitive.Root>
          <BrandMark className="size-6 shrink-0 text-fg lg:hidden" />
          <nav aria-label="Breadcrumb" className="min-w-0">
            <ol className="flex items-center gap-1.5 truncate">
              {current?.group && (
                <li className="hidden text-fg-subtle sm:block">{current.group}</li>
              )}
              {current?.group && (
                <li aria-hidden className="hidden text-fg-subtle sm:block">
                  /
                </li>
              )}
              <li className="truncate font-medium">{current?.item.label ?? 'Overview'}</li>
            </ol>
          </nav>
          <div className="ml-auto flex items-center gap-2">
            <EnvironmentBadge />
            <Button
              variant="ghost"
              size="icon"
              aria-label="Search"
              className="lg:hidden"
              onClick={() => {
                setPaletteOpen(true);
              }}
            >
              <Search aria-hidden />
            </Button>
            <UserMenu />
          </div>
        </header>
        <main
          id="main"
          tabIndex={-1}
          className="mx-auto w-full max-w-page flex-1 px-4 py-6 lg:px-6"
        >
          {children}
        </main>
      </div>
      <GlobalPalette open={paletteOpen} onOpenChange={setPaletteOpen} navigation={navigation} />
    </div>
  );
}
