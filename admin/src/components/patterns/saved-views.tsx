import { useId, useState } from 'react';
import { Bookmark, X } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover';
import { cn } from '@/lib/cn';

export interface SavedView {
  name: string;
  filters: Record<string, string>;
}

const same = (a: Record<string, string>, b: Record<string, string>) =>
  JSON.stringify(Object.entries(a).sort()) === JSON.stringify(Object.entries(b).sort());

/** "All" plus the operator's named filter sets for this list. */
export function SavedViews({
  views,
  current,
  onSelect,
  onSave,
  onDelete,
}: {
  views: SavedView[];
  current: Record<string, string>;
  onSelect: (filters: Record<string, string>) => void;
  onSave: (name: string) => void;
  onDelete: (name: string) => void;
}) {
  const inputId = useId();
  const [open, setOpen] = useState(false);
  const [name, setName] = useState('');
  const all = Object.keys(current).length === 0;
  const matching = views.find((view) => same(view.filters, current));
  const tab = (active: boolean) =>
    cn(
      '-mb-px inline-flex h-control-lg items-center gap-1.5 border-b-2 px-3 text-sm',
      active ? 'border-fg font-semibold text-fg' : 'border-transparent text-fg-muted hover:text-fg',
    );
  return (
    <nav
      aria-label="Saved views"
      className="flex flex-wrap items-center gap-1 border-b border-border"
    >
      <button
        type="button"
        aria-current={all ? 'page' : undefined}
        className={tab(all)}
        onClick={() => {
          onSelect({});
        }}
      >
        All
      </button>
      {views.map((view) => (
        <span key={view.name} className="group inline-flex items-center">
          <button
            type="button"
            aria-current={matching === view ? 'page' : undefined}
            className={tab(matching === view)}
            onClick={() => {
              onSelect(view.filters);
            }}
          >
            {view.name}
          </button>
          <button
            type="button"
            aria-label={`Delete view ${view.name}`}
            className="grid size-6 place-items-center rounded-sm text-fg-subtle opacity-60 group-hover:opacity-100 hover:bg-surface-hover"
            onClick={() => {
              onDelete(view.name);
            }}
          >
            <X aria-hidden className="size-3" />
          </button>
        </span>
      ))}
      {!all && !matching && (
        <Popover open={open} onOpenChange={setOpen}>
          <PopoverTrigger asChild>
            <Button variant="ghost" size="sm" className="text-accent">
              <Bookmark aria-hidden />
              Save view
            </Button>
          </PopoverTrigger>
          <PopoverContent>
            <form
              className="flex flex-col gap-2"
              onSubmit={(event) => {
                event.preventDefault();
                const trimmed = name.trim();
                if (!trimmed) return;
                onSave(trimmed);
                setName('');
                setOpen(false);
              }}
            >
              <Label htmlFor={inputId}>View name</Label>
              <Input
                id={inputId}
                maxLength={40}
                value={name}
                onChange={(event) => {
                  setName(event.target.value);
                }}
              />
              <Button type="submit" size="sm" variant="primary" disabled={!name.trim()}>
                Save
              </Button>
            </form>
          </PopoverContent>
        </Popover>
      )}
    </nav>
  );
}
