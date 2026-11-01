import { useConfig } from '@/app/config-context';
import { useQuery } from '@tanstack/react-query';
import { ExternalLink } from 'lucide-react';
import { api } from '@/api/client';
import { typedApi } from '@/api/typed';
import { Badge } from '@/components/ui/badge';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { useSubject } from '@/app/session';

const client = typedApi(api);
const DASHBOARDS = ['https://dashboard.stripe.com', 'https://dashboard.stripe.com/test'];
const CUSTOMER = /^cus_[A-Za-z0-9]+$/;

export function useProviderInfo() {
  const subject = useSubject();
  const { modules } = useConfig();
  return useQuery({
    enabled: modules.has('billing'),
    queryKey: ['billing', subject, 'provider'],
    queryFn: ({ signal }) => client.get('/api/v1/platform/billing/provider', { signal }),
    staleTime: 60_000,
  });
}

/**
 * Money moves only at the payment provider. Links go to an allowlisted
 * dashboard origin; the server-supplied URL is never followed blindly.
 */
export function ProviderCard({ customerId }: { customerId?: string | null }) {
  const info = useProviderInfo();
  if (info.isError)
    return (
      <p role="status" className="rounded-lg border border-warning bg-warning-muted p-3">
        Payment provider information is unavailable.
      </p>
    );
  if (!info.data) return null;
  const dashboard = DASHBOARDS.find((url) => url === info.data.dashboard_url);
  const links: [string, string][] = [
    [
      'Customer',
      customerId && CUSTOMER.test(customerId)
        ? `/customers/${encodeURIComponent(customerId)}`
        : '/customers',
    ],
    ['Invoices', '/invoices'],
    ['Payments and refunds', '/payments'],
    ['Disputes', '/disputes'],
  ];
  return (
    <Card>
      <CardHeader>
        <CardTitle>Payment provider</CardTitle>
        <Badge tone={info.data.mode === 'live' ? 'warning' : 'neutral'}>
          {info.data.provider} · {info.data.mode}
        </Badge>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        <p className="text-fg-muted">
          {info.data.mode === 'demo'
            ? 'Demo provider: nothing here moves real money.'
            : 'Invoices, refunds, disputes and cancellations are managed at the provider; verified webhooks synchronize Sandloom. Credit adjustments change only the credit ledger.'}
        </p>
        {dashboard && (
          <ul className="flex flex-wrap gap-x-4 gap-y-1">
            {links.map(([label, path]) => (
              <li key={label}>
                <a
                  className="inline-flex items-center gap-1 text-accent hover:underline"
                  href={`${dashboard}${path}`}
                  target="_blank"
                  rel="noopener noreferrer"
                >
                  {label}
                  <ExternalLink aria-hidden className="size-3.5" />
                  <span className="sr-only">(opens the provider dashboard)</span>
                </a>
              </li>
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  );
}
