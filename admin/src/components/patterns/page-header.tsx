import type { ReactNode } from 'react';

export function PageHeader({
  title,
  description,
  actions,
}: {
  title: ReactNode;
  description?: ReactNode;
  actions?: ReactNode;
}) {
  return (
    <div className="flex flex-wrap items-end gap-3">
      <div className="flex min-w-0 flex-col gap-0.5">
        <h1 className="text-xl font-semibold tracking-tight">{title}</h1>
        {description && <p className="text-fg-muted">{description}</p>}
      </div>
      {actions && <div className="ml-auto flex flex-wrap items-center gap-2">{actions}</div>}
    </div>
  );
}
