import type { HTMLAttributes } from 'react';
import { cn } from '@/lib/cn';

/** Decorative placeholder; the loading container carries aria-busy. */
export function Skeleton({ className, ...props }: HTMLAttributes<HTMLSpanElement>) {
  return (
    <span
      aria-hidden
      className={cn('block animate-pulse rounded-sm bg-surface-hover', className)}
      {...props}
    />
  );
}
