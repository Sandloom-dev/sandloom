import { useEffect, useId, useState } from 'react';
import { ListFilter, Search, X } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Input, NativeSelect } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover';
import { describeFilter, filterError } from '@/schema/filters';
import type { Field } from '@/schema/types';

export type FilterValues = Record<string, string>;

export interface FilterBarProps {
  fields: Field[];
  values: FilterValues;
  onChange: (values: FilterValues) => void;
  /** Name of the free-text search filter rendered as the search box. */
  searchField?: string;
  trailing?: React.ReactNode;
}

const SEARCH_DEBOUNCE = 300;

export function FilterBar({
  fields,
  values,
  onChange,
  searchField = 'q',
  trailing,
}: FilterBarProps) {
  const search = fields.find((field) => field.name === searchField);
  const others = fields.filter((field) => field.name !== searchField);
  const active = others.filter((field) => values[field.name] !== undefined);
  const inactive = others.filter((field) => values[field.name] === undefined);
  const set = (name: string, value: string | undefined) => {
    const next = Object.fromEntries(Object.entries(values).filter(([key]) => key !== name));
    onChange(value === undefined || value === '' ? next : { ...next, [name]: value });
  };
  return (
    <div className="flex flex-wrap items-center gap-2" role="search">
      {search && (
        <SearchBox
          label={search.label}
          value={values[search.name] ?? ''}
          onChange={(value) => {
            set(search.name, value);
          }}
        />
      )}
      {active.map((field) => (
        <FilterEditor
          key={field.name}
          field={field}
          value={values[field.name] ?? ''}
          onApply={(value) => {
            set(field.name, value);
          }}
          trigger={
            <>
              {field.label} is{' '}
              <span className="font-semibold">
                {describeFilter(field, values[field.name] ?? '')}
              </span>
            </>
          }
          onRemove={() => {
            set(field.name, undefined);
          }}
        />
      ))}
      {inactive.length > 0 && (
        <AddFilter
          fields={inactive}
          onApply={(name, value) => {
            set(name, value);
          }}
        />
      )}
      {active.length > 0 && (
        <Button
          variant="ghost"
          size="sm"
          onClick={() => {
            onChange(
              search && values[search.name] ? { [search.name]: values[search.name] ?? '' } : {},
            );
          }}
        >
          Clear filters
        </Button>
      )}
      {trailing && <div className="ml-auto flex items-center gap-2">{trailing}</div>}
    </div>
  );
}

function SearchBox({
  label,
  value,
  onChange,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
}) {
  const [draft, setDraft] = useState(value);
  const [synced, setSynced] = useState(value);
  if (value !== synced) {
    // The URL changed elsewhere (cleared, saved view): adopt it during render.
    setSynced(value);
    setDraft(value);
  }
  useEffect(() => {
    if (draft === value) return;
    const timer = window.setTimeout(() => {
      onChange(draft.trim());
    }, SEARCH_DEBOUNCE);
    return () => {
      window.clearTimeout(timer);
    };
  }, [draft, value, onChange]);
  return (
    <div className="relative w-full max-w-field">
      <Search
        aria-hidden
        className="pointer-events-none absolute top-1/2 left-2.5 size-4 -translate-y-1/2 text-fg-subtle"
      />
      <Input
        type="search"
        aria-label={label}
        placeholder={`${label}…`}
        className="pl-8"
        value={draft}
        onChange={(event) => {
          setDraft(event.target.value);
        }}
      />
    </div>
  );
}

function ValueInput({
  field,
  id,
  value,
  onChange,
}: {
  field: Field;
  id: string;
  value: string;
  onChange: (value: string) => void;
}) {
  if (field.type === 'boolean' || field.enum) {
    const options = field.enum
      ? field.enum.map((option) => [String(option), String(option)] as const)
      : ([
          ['true', 'Yes'],
          ['false', 'No'],
        ] as const);
    return (
      <NativeSelect
        id={id}
        value={value}
        onChange={(event) => {
          onChange(event.target.value);
        }}
      >
        <option value="">Select…</option>
        {options.map(([option, text]) => (
          <option key={option} value={option}>
            {text}
          </option>
        ))}
      </NativeSelect>
    );
  }
  return (
    <Input
      id={id}
      value={value}
      type={field.type === 'integer' || field.type === 'number' ? 'number' : 'text'}
      placeholder={field.format === 'uuid' ? '00000000-0000-…' : undefined}
      onChange={(event) => {
        onChange(event.target.value);
      }}
    />
  );
}

function FilterEditor({
  field,
  value,
  onApply,
  onRemove,
  trigger,
}: {
  field: Field;
  value: string;
  onApply: (value: string) => void;
  onRemove: () => void;
  trigger: React.ReactNode;
}) {
  const id = useId();
  const [open, setOpen] = useState(false);
  const [draft, setDraft] = useState(value);
  const error = draft ? filterError(field, draft) : undefined;
  return (
    <span className="inline-flex h-control-sm items-center gap-0.5 rounded-full border border-border bg-surface-muted pr-0.5 pl-3 text-xs">
      <Popover
        open={open}
        onOpenChange={(next) => {
          setOpen(next);
          if (next) setDraft(value);
        }}
      >
        <PopoverTrigger asChild>
          <button type="button" className="rounded-sm hover:text-fg">
            {trigger}
          </button>
        </PopoverTrigger>
        <PopoverContent>
          <form
            className="flex flex-col gap-2"
            onSubmit={(event) => {
              event.preventDefault();
              if (!error && draft) {
                onApply(draft);
                setOpen(false);
              }
            }}
          >
            <Label htmlFor={id}>{field.label}</Label>
            <ValueInput field={field} id={id} value={draft} onChange={setDraft} />
            {error && <p className="text-xs text-danger">{error}</p>}
            <Button type="submit" size="sm" variant="primary" disabled={!draft || Boolean(error)}>
              Apply
            </Button>
          </form>
        </PopoverContent>
      </Popover>
      <button
        type="button"
        aria-label={`Remove ${field.label} filter`}
        className="grid size-6 place-items-center rounded-full text-fg-subtle hover:bg-surface-hover hover:text-fg"
        onClick={onRemove}
      >
        <X aria-hidden className="size-3" />
      </button>
    </span>
  );
}

function AddFilter({
  fields,
  onApply,
}: {
  fields: Field[];
  onApply: (name: string, value: string) => void;
}) {
  const fieldId = useId();
  const valueId = useId();
  const [open, setOpen] = useState(false);
  const [name, setName] = useState(fields[0]?.name ?? '');
  const [draft, setDraft] = useState('');
  const field = fields.find((candidate) => candidate.name === name) ?? fields[0];
  if (!field) return null;
  const error = draft ? filterError(field, draft) : undefined;
  return (
    <Popover
      open={open}
      onOpenChange={(next) => {
        setOpen(next);
        if (next) {
          setName(fields[0]?.name ?? '');
          setDraft('');
        }
      }}
    >
      <PopoverTrigger asChild>
        <Button size="sm" variant="ghost" className="border border-dashed border-border-strong">
          <ListFilter aria-hidden />
          Filter
        </Button>
      </PopoverTrigger>
      <PopoverContent>
        <form
          className="flex flex-col gap-2"
          onSubmit={(event) => {
            event.preventDefault();
            if (!error && draft) {
              onApply(field.name, draft);
              setOpen(false);
            }
          }}
        >
          <Label htmlFor={fieldId}>Field</Label>
          <NativeSelect
            id={fieldId}
            value={field.name}
            onChange={(event) => {
              setName(event.target.value);
              setDraft('');
            }}
          >
            {fields.map((candidate) => (
              <option key={candidate.name} value={candidate.name}>
                {candidate.label}
              </option>
            ))}
          </NativeSelect>
          <Label htmlFor={valueId}>Value</Label>
          <ValueInput field={field} id={valueId} value={draft} onChange={setDraft} />
          {error && <p className="text-xs text-danger">{error}</p>}
          <Button type="submit" size="sm" variant="primary" disabled={!draft || Boolean(error)}>
            Apply filter
          </Button>
        </form>
      </PopoverContent>
    </Popover>
  );
}
