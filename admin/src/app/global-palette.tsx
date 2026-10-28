import { useDeferredValue, useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { LogOut, Monitor, Moon, Sun, UserRound } from 'lucide-react';
import { api } from '@/api/client';
import { typedApi } from '@/api/typed';
import { CommandPalette, type PaletteGroup } from '@/components/patterns/command-palette';
import { Kbd } from '@/components/ui/kbd';
import { useHotkey, modKeyLabel } from '@/hooks/use-hotkey';
import { useTheme } from '@/hooks/use-theme';
import { iconFor } from '@/lib/icons';
import { UUID_RE } from '@/lib/format';
import { allowed } from '@/schema/types';
import { useConfig } from './config-context';
import { useSession } from './session';
import type { NavGroup } from './navigation';

const client = typedApi(api);

/** The app's ⌘K: pages, live user lookup, UUID jump, theme and sign-out. */
export function GlobalPalette({
  open,
  onOpenChange,
  navigation,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  navigation: NavGroup[];
}) {
  const navigate = useNavigate();
  const { setTheme } = useTheme();
  const { manifest } = useConfig();
  const { session } = useSession();
  const [query, setQuery] = useState('');
  const term = useDeferredValue(query.trim());
  useHotkey('mod+k', () => {
    onOpenChange(!open);
  });
  const canSearchUsers =
    session !== undefined &&
    manifest.resources.some((r) => r.name === 'users' && allowed(r.access, session.roles));
  const users = useQuery({
    queryKey: ['palette', session?.subject, 'users', term],
    enabled: open && canSearchUsers && term.length >= 2 && !UUID_RE.test(term),
    queryFn: ({ signal }) =>
      client.get('/api/v1/users', { query: { q: term, limit: 8, include_total: false }, signal }),
    staleTime: 10_000,
  });
  const groups = useMemo<PaletteGroup[]>(() => {
    const go = (to: string) => () => {
      setQuery('');
      void navigate(to);
    };
    const result: PaletteGroup[] = [];
    if (UUID_RE.test(term)) {
      result.push({
        heading: 'Open record',
        items: manifest.resources
          .filter((r) => r.detail && !r.scope && session && allowed(r.access, session.roles))
          .map((r) => ({
            id: `open-${r.name}`,
            label: `Open as ${r.label}`,
            icon: iconFor(r.icon),
            keywords: [term],
            onSelect: go(`/${r.name}/${term}`),
          })),
      });
    }
    if (users.data?.length)
      result.push({
        heading: 'Users',
        items: users.data.map((user) => ({
          id: `user-${user.id}`,
          label: `${user.full_name} · ${user.email}`,
          icon: UserRound,
          keywords: [term],
          onSelect: go(`/users/${user.id}`),
        })),
      });
    result.push({
      heading: 'Go to',
      items: navigation.flatMap((group) =>
        group.items.map((item) => ({
          id: `nav-${item.to}`,
          label: group.label ? `${group.label} › ${item.label}` : item.label,
          icon: item.icon,
          ...(item.keywords ? { keywords: item.keywords } : {}),
          onSelect: go(item.to),
        })),
      ),
    });
    result.push({
      heading: 'Preferences',
      items: [
        {
          id: 'theme-light',
          label: 'Theme: light',
          icon: Sun,
          onSelect: () => {
            setTheme('light');
          },
        },
        {
          id: 'theme-dark',
          label: 'Theme: dark',
          icon: Moon,
          onSelect: () => {
            setTheme('dark');
          },
        },
        {
          id: 'theme-system',
          label: 'Theme: system',
          icon: Monitor,
          onSelect: () => {
            setTheme('system');
          },
        },
        {
          id: 'sign-out',
          label: 'Sign out',
          icon: LogOut,
          tone: 'danger',
          onSelect: () => {
            void api.logout().catch(() => undefined);
          },
        },
      ],
    });
    return result;
  }, [term, users.data, navigation, manifest, session, navigate, setTheme]);
  return (
    <CommandPalette
      open={open}
      onOpenChange={(next) => {
        if (!next) setQuery('');
        onOpenChange(next);
      }}
      query={query}
      onQueryChange={setQuery}
      groups={groups}
      loading={users.isFetching}
      footer={
        <>
          <span>
            <Kbd>↑</Kbd> <Kbd>↓</Kbd> navigate
          </span>
          <span>
            <Kbd>↵</Kbd> open
          </span>
          <span className="ml-auto">
            <Kbd>{modKeyLabel}</Kbd> <Kbd>K</Kbd> toggle
          </span>
        </>
      }
    />
  );
}
