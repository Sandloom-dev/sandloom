import { Info } from 'lucide-react';
import { Badge } from '@/components/ui/badge';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { DescriptionList } from '@/components/patterns/description-list';
import { PageHeader } from '@/components/patterns/page-header';
import { Segmented } from '@/components/patterns/segmented';
import { EmptyState, ErrorState, LoadingRows } from '@/components/patterns/states';
import { humanize } from '@/lib/format';
import { statusTone } from '@/features/resources/cells';
import { useOperationsSource, usePlatformView, type RuntimeSettings } from './api';

/** Read-only, allowlisted runtime configuration and infrastructure health. */
export function SettingsPage() {
  const { sources, source, setSource } = useOperationsSource();
  const view = usePlatformView<RuntimeSettings>(source, 'settings');
  if (!source) return <EmptyState title="No operations source is exposed" />;
  return (
    <div className="flex max-w-page flex-col gap-4">
      <PageHeader
        title="Runtime settings"
        description="An allowlist of non-secret settings of the selected API process. Change configuration through deployment, never here."
        actions={
          <Segmented
            label="Source"
            options={sources.map((name) => ({ value: name, label: name }))}
            value={source}
            onChange={setSource}
          />
        }
      />
      {view.isError ? (
        <ErrorState error={view.error} onRetry={() => void view.refetch()} />
      ) : view.isPending ? (
        <LoadingRows rows={6} label="Loading settings" />
      ) : (
        <div className="flex flex-wrap items-start gap-4">
          <Card className="min-w-0 flex-1 basis-96">
            <CardHeader>
              <CardTitle>Configuration</CardTitle>
            </CardHeader>
            <CardContent>
              <DescriptionList
                items={view.data.settings.map((setting) => ({
                  key: setting.key,
                  label: humanize(setting.key),
                  value: <span className="font-mono text-xs">{setting.value}</span>,
                }))}
              />
            </CardContent>
          </Card>
          <div className="flex min-w-0 flex-1 basis-80 flex-col gap-4">
            <Card>
              <CardHeader>
                <CardTitle>Infrastructure</CardTitle>
              </CardHeader>
              <ul>
                {view.data.infrastructure.map((instance) => (
                  <li
                    key={`${instance.capability}:${instance.name}`}
                    className="flex items-center gap-3 border-b border-border px-4 py-2.5 last:border-b-0"
                  >
                    <span className="font-medium">{instance.capability}</span>
                    <span className="font-mono text-xs text-fg-subtle">
                      {instance.name}
                      {instance.driver ? ` · ${instance.driver}` : ''}
                    </span>
                    <Badge className="ml-auto" tone={statusTone(instance.status)} dot>
                      {instance.status}
                    </Badge>
                  </li>
                ))}
              </ul>
            </Card>
            <p className="flex gap-2 rounded-lg border border-border bg-surface-muted p-4 text-fg-muted">
              <Info aria-hidden className="mt-0.5 size-4 shrink-0" />
              Feature flags: not configured. Sandloom ships no feature-flag capability; add one as
              an infrastructure plugin before exposing toggles here.
            </p>
          </div>
        </div>
      )}
    </div>
  );
}
