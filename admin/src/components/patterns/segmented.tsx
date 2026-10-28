import { cn } from '@/lib/cn';

export interface SegmentedOption<T extends string> {
  value: T;
  label: string;
}

/** A small exclusive choice (source, time range). Buttons with aria-pressed. */
export function Segmented<T extends string>({
  label,
  options,
  value,
  onChange,
}: {
  label: string;
  options: readonly SegmentedOption<T>[];
  value: T;
  onChange: (value: T) => void;
}) {
  return (
    <div
      role="group"
      aria-label={label}
      className="flex w-fit overflow-hidden rounded-md border border-border bg-surface"
    >
      {options.map((option) => (
        <button
          key={option.value}
          type="button"
          aria-pressed={option.value === value}
          className={cn(
            'h-control border-l border-border px-3 text-sm first:border-l-0',
            option.value === value
              ? 'bg-surface-hover font-semibold text-fg'
              : 'text-fg-muted hover:text-fg',
          )}
          onClick={() => {
            onChange(option.value);
          }}
        >
          {option.label}
        </button>
      ))}
    </div>
  );
}
