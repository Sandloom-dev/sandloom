import type { ReactNode } from 'react';

export interface DescriptionItem {
  key: string;
  label: string;
  value: ReactNode;
}

export function DescriptionList({ items }: { items: DescriptionItem[] }) {
  return (
    <dl className="grid grid-cols-1 gap-x-4 gap-y-2.5 sm:grid-cols-label">
      {items.map((item) => (
        <div key={item.key} className="contents">
          <dt className="text-fg-subtle">{item.label}</dt>
          <dd className="min-w-0 break-words">{item.value}</dd>
        </div>
      ))}
    </dl>
  );
}
