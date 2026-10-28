import { RefreshCw } from 'lucide-react';
import { useQueryClient } from '@tanstack/react-query';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardHeader, CardTitle } from '@/components/ui/card';
import { PageHeader } from '@/components/patterns/page-header';
import { EmptyState, ErrorState, LoadingRows } from '@/components/patterns/states';
import { statusTone } from '@/features/resources/cells';
import { useSubject } from '@/app/session';
import { sourceName, useServiceHealth } from './sources';

/** Live health sample of every API process the gateway routes to. */
export function HealthPage() {
  const subject = useSubject();
  const queryClient = useQueryClient();
  const services = useServiceHealth();
  return (
    <div className="flex flex-col gap-4">
      <PageHeader
        title="Service health"
        description="A live sample per API process: infrastructure, peer contracts and required consumers. Not a historic monitor."
        actions={
          <Button
            onClick={() => {
              void queryClient.invalidateQueries({ queryKey: ['platform', subject, 'services'] });
            }}
          >
            <RefreshCw aria-hidden />
            Refresh
          </Button>
        }
      />
      {services.length === 0 && <EmptyState title="No health source is exposed" />}
      <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
        {services.map(({ source, result }) => (
          <Card key={source.name}>
            <CardHeader>
              <CardTitle>{sourceName(source)}</CardTitle>
            </CardHeader>
            {!result || result.isPending ? (
              <LoadingRows rows={3} label={`Loading ${sourceName(source)} health`} />
            ) : result.isError ? (
              <ErrorState error={result.error} onRetry={() => void result.refetch()} />
            ) : (
              <table className="w-full text-left" aria-label={`${sourceName(source)} checks`}>
                <thead>
                  <tr className="border-b border-border text-xs text-fg-subtle">
                    <th scope="col" className="px-4 py-2 font-medium">
                      Check
                    </th>
                    <th scope="col" className="px-4 py-2 font-medium">
                      Category
                    </th>
                    <th scope="col" className="px-4 py-2 font-medium">
                      Status
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {result.data.map((row) => (
                    <tr key={row.id} className="border-b border-border last:border-b-0">
                      <td className="px-4 py-2.5 font-mono text-xs">{row.id}</td>
                      <td className="px-4 py-2.5 text-fg-muted">{row.category}</td>
                      <td className="px-4 py-2.5">
                        <Badge tone={statusTone(row.status)} dot>
                          {row.status}
                        </Badge>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </Card>
        ))}
      </div>
    </div>
  );
}
