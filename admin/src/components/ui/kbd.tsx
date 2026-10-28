import type { HTMLAttributes } from 'react';
import { cn } from '@/lib/cn';

export function Kbd({ className, ...props }: HTMLAttributes<HTMLElement>) {
  return (
    <kbd
      className={cn(
        'rounded-sm border border-border bg-surface px-1.5 font-mono text-xs text-fg-subtle',
        className,
      )}
      {...props}
    />
  );
}
