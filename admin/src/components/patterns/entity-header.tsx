import type { ReactNode } from 'react';
import { initials } from '@/lib/format';

export function EntityHeader({
  title,
  subtitle,
  badges,
  actions,
  avatar = true,
}: {
  title: string;
  subtitle?: ReactNode;
  badges?: ReactNode;
  actions?: ReactNode;
  avatar?: boolean;
}) {
  return (
    <div className="flex flex-wrap items-start gap-4">
      {avatar && (
        <span
          aria-hidden
          className="grid size-12 shrink-0 place-items-center rounded-full bg-surface-muted text-lg font-semibold"
        >
          {initials(title)}
        </span>
      )}
      <div className="flex min-w-0 flex-1 flex-col gap-1.5">
        <div className="flex flex-wrap items-center gap-2">
          <h1 className="text-xl font-semibold tracking-tight break-words">{title}</h1>
          {badges}
        </div>
        {subtitle && (
          <div className="flex flex-wrap items-center gap-3 text-fg-muted">{subtitle}</div>
        )}
      </div>
      {actions && <div className="flex flex-wrap gap-2">{actions}</div>}
    </div>
  );
}
