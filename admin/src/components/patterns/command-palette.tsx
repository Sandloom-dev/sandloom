import type { ReactNode } from 'react';
import { Command } from 'cmdk';
import { Dialog as DialogPrimitive } from 'radix-ui';
import { Search } from 'lucide-react';
import type { LucideIcon } from 'lucide-react';
import { Kbd } from '@/components/ui/kbd';
import { cn } from '@/lib/cn';

export interface PaletteItem {
  id: string;
  label: string;
  icon?: LucideIcon;
  hint?: string;
  keywords?: string[];
  tone?: 'default' | 'danger';
  onSelect: () => void;
}
export interface PaletteGroup {
  heading: string;
  items: PaletteItem[];
}

/**
 * ⌘K palette (cmdk on a Radix dialog): fuzzy navigation, record lookup and
 * commands. Commands only OPEN their confirmation; nothing runs from here.
 */
export function CommandPalette({
  open,
  onOpenChange,
  query,
  onQueryChange,
  groups,
  footer,
  loading = false,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  query: string;
  onQueryChange: (query: string) => void;
  groups: PaletteGroup[];
  footer?: ReactNode;
  loading?: boolean;
}) {
  return (
    <DialogPrimitive.Root open={open} onOpenChange={onOpenChange}>
      <DialogPrimitive.Portal>
        <DialogPrimitive.Overlay className="fixed inset-0 z-overlay animate-fade-in bg-overlay" />
        <DialogPrimitive.Content
          aria-describedby={undefined}
          className="fixed inset-x-4 top-24 z-dialog mx-auto max-w-palette animate-pop-in overflow-hidden rounded-xl border border-border bg-surface shadow-lg"
        >
          <DialogPrimitive.Title className="sr-only">Command palette</DialogPrimitive.Title>
          <Command label="Command palette" shouldFilter loop>
            <div className="flex h-13 items-center gap-2.5 border-b border-border px-4">
              <Search aria-hidden className="size-4 text-fg-subtle" />
              <Command.Input
                value={query}
                onValueChange={onQueryChange}
                placeholder="Type a command, page, email or UUID…"
                className="h-full flex-1 bg-transparent text-base outline-none placeholder:text-fg-subtle"
              />
              <Kbd>Esc</Kbd>
            </div>
            <Command.List className="max-h-96 overflow-y-auto p-1.5">
              {loading && (
                <Command.Loading>
                  <p className="px-3 py-2 text-fg-subtle">Searching…</p>
                </Command.Loading>
              )}
              <Command.Empty className="p-8 text-center text-fg-muted">
                No matches. Paste a full UUID to open a record directly.
              </Command.Empty>
              {groups.map((group) => (
                <Command.Group
                  key={group.heading}
                  heading={group.heading}
                  className="[&_[cmdk-group-heading]]:px-2.5 [&_[cmdk-group-heading]]:pt-2 [&_[cmdk-group-heading]]:pb-1 [&_[cmdk-group-heading]]:text-xs [&_[cmdk-group-heading]]:font-semibold [&_[cmdk-group-heading]]:text-fg-subtle"
                >
                  {group.items.map((item) => {
                    const Icon = item.icon;
                    return (
                      <Command.Item
                        key={item.id}
                        value={`${group.heading} ${item.label} ${item.id}`}
                        keywords={item.keywords}
                        onSelect={() => {
                          onOpenChange(false);
                          item.onSelect();
                        }}
                        className={cn(
                          'flex h-control-lg cursor-pointer items-center gap-2.5 rounded-md px-2.5 data-[selected=true]:bg-accent-muted',
                          item.tone === 'danger' && 'text-danger',
                        )}
                      >
                        {Icon && <Icon aria-hidden className="size-4 shrink-0 text-fg-subtle" />}
                        <span className="flex-1 truncate">{item.label}</span>
                        {item.hint && (
                          <span className="font-mono text-xs text-fg-subtle">{item.hint}</span>
                        )}
                      </Command.Item>
                    );
                  })}
                </Command.Group>
              ))}
            </Command.List>
          </Command>
          {footer && (
            <div className="flex flex-wrap gap-4 border-t border-border px-4 py-2.5 text-xs text-fg-subtle">
              {footer}
            </div>
          )}
        </DialogPrimitive.Content>
      </DialogPrimitive.Portal>
    </DialogPrimitive.Root>
  );
}
