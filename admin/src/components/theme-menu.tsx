import { Monitor, Moon, Sun } from 'lucide-react';
import { useTheme, type ThemePreference } from '@/hooks/use-theme';
import { Button } from '@/components/ui/button';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';

const THEMES: { value: ThemePreference; label: string; icon: typeof Sun }[] = [
  { value: 'light', label: 'Light', icon: Sun },
  { value: 'dark', label: 'Dark', icon: Moon },
  { value: 'system', label: 'System', icon: Monitor },
];

/** Theme selection is available before signing in. */
export function ThemeMenu() {
  const { theme, setTheme } = useTheme();
  const Icon = theme === 'light' ? Sun : theme === 'dark' ? Moon : Monitor;
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button variant="ghost" size="icon" aria-label="Appearance">
          <Icon aria-hidden />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end">
        <DropdownMenuLabel>Appearance</DropdownMenuLabel>
        {THEMES.map(({ value, label, icon: ThemeIcon }) => (
          <DropdownMenuItem
            onSelect={() => {
              setTheme(value);
            }}
            key={value}
          >
            <ThemeIcon aria-hidden />
            {label}
            {theme === value && <span className="ml-auto text-xs text-fg-subtle">current</span>}
          </DropdownMenuItem>
        ))}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
