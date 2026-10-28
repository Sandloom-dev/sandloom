import { useState } from 'react';
import { Check, Copy } from 'lucide-react';
import { shortId } from '@/lib/format';
import { cn } from '@/lib/cn';

/** Monospace identifier with a copy button; dense tables show the short form. */
export function CopyText({
  value,
  short = false,
  className,
}: {
  value: string;
  short?: boolean;
  className?: string;
}) {
  const [copied, setCopied] = useState(false);
  return (
    <span className={cn('inline-flex items-center gap-1.5 font-mono text-xs', className)}>
      <span title={short ? value : undefined}>{short ? shortId(value) : value}</span>
      <button
        type="button"
        aria-label={copied ? 'Copied' : `Copy ${value}`}
        className="grid size-6 place-items-center rounded-sm text-fg-subtle hover:bg-surface-hover hover:text-fg"
        onClick={(event) => {
          event.stopPropagation();
          void navigator.clipboard.writeText(value).then(
            () => {
              setCopied(true);
              window.setTimeout(() => {
                setCopied(false);
              }, 1500);
            },
            () => undefined,
          );
        }}
      >
        {copied ? (
          <Check aria-hidden className="size-3.5" />
        ) : (
          <Copy aria-hidden className="size-3.5" />
        )}
      </button>
    </span>
  );
}
