import type { ReactNode } from 'react';
import { cn } from '@/lib/cn';
import { formatDateTime, formatRelative } from '@/lib/format';

export interface TimelineItem {
  id: string;
  title: ReactNode;
  /** Machine detail: action code, request id … (monospace). */
  meta?: string;
  at: string;
  tone?: 'neutral' | 'accent' | 'danger' | 'success';
}

const DOT: Record<NonNullable<TimelineItem['tone']>, string> = {
  neutral: 'bg-fg-subtle',
  accent: 'bg-accent',
  danger: 'bg-danger',
  success: 'bg-success',
};

export function Timeline({ items }: { items: TimelineItem[] }) {
  return (
    <ol className="flex flex-col">
      {items.map((item, index) => (
        <li key={item.id} className="flex gap-3">
          <span aria-hidden className="flex flex-col items-center pt-1.5">
            <span className={cn('size-2 rounded-full', DOT[item.tone ?? 'neutral'])} />
            {index < items.length - 1 && <span className="mt-1 w-px flex-1 bg-border" />}
          </span>
          <div className="flex min-w-0 flex-col gap-0.5 pb-4">
            <div>{item.title}</div>
            <div className="flex flex-wrap gap-x-2 text-xs text-fg-subtle">
              {item.meta && <span className="font-mono break-all">{item.meta}</span>}
              <time dateTime={item.at} title={formatDateTime(item.at)}>
                {formatRelative(item.at)}
              </time>
            </div>
          </div>
        </li>
      ))}
    </ol>
  );
}
