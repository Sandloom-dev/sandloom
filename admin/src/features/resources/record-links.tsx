import { Link } from 'react-router-dom';
import { ArrowRight } from 'lucide-react';
import { Card, CardHeader, CardTitle } from '@/components/ui/card';
import { useConfig } from '@/app/config-context';
import { listHref } from './routes';

export interface RecordLink {
  label: string;
  resource: string;
  filter: Record<string, string>;
}

/** Links to other admitted resources pre-filtered on this record. */
export function RecordLinks({ title, links }: { title: string; links: RecordLink[] }) {
  const { manifest } = useConfig();
  const visible = links.filter((link) =>
    manifest.resources.some((resource) => resource.name === link.resource),
  );
  if (visible.length === 0) return null;
  return (
    <Card>
      <CardHeader>
        <CardTitle>{title}</CardTitle>
      </CardHeader>
      <ul>
        {visible.map((link) => (
          <li key={link.resource} className="border-b border-border last:border-b-0">
            <Link
              to={listHref(link.resource, link.filter)}
              className="flex items-center gap-2 px-4 py-2.5 hover:bg-surface-muted"
            >
              {link.label}
              <ArrowRight aria-hidden className="ml-auto size-4 text-fg-subtle" />
            </Link>
          </li>
        ))}
      </ul>
    </Card>
  );
}
