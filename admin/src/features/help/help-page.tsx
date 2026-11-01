import { CircleAlert, Info } from 'lucide-react';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { PageHeader } from '@/components/patterns/page-header';

const SECTIONS: readonly (readonly [string, string])[] = [
  [
    'Overview and ⌘K',
    'The overview shows live service health, items needing attention (failed webhooks, intents awaiting follow-up) and recent audited activity. Press ⌘K (Ctrl+K) anywhere to jump to a page, find a user by name or email, or open any record by pasting its UUID.',
  ],
  [
    'Users',
    'Search accounts by name or email, filter by status, save views. A user page shows profile, platform roles, related records and the audited activity. Emergency commands: revoke sessions (ends every session) and block (rejects every request and sign-in until unblocked); both need a reason and are audited. You cannot apply them to your own account.',
  ],
  [
    'All organizations',
    'Every organization, including inactive ones and organizations you do not belong to. Search by name or slug; filter by owner or active status.',
  ],
  [
    'Organization members',
    'Global membership directory, filterable by organization or user ID. Platform access does not create membership or expose self-service impersonation.',
  ],
  [
    'Billing customers',
    'Personal and organization billing subjects. Open a customer to follow links to subscriptions, credit balance, ledger, purchases, and intents.',
  ],
  [
    'Subscriptions',
    'Provider-synchronized subscription status, price, period end, cancellation flags, and authorization origin. This view does not grant access by editing a status.',
  ],
  [
    'Products and prices',
    'Inspect active and archived catalog entries. Edit a product name or availability with a reason. Prices and purchase credit amounts are immutable here; monetary amounts use minor currency units.',
  ],
  [
    'Credit balances and ledger',
    'Inspect current balances, including debt, and the append-only history. Adjust credits from a customer card with an amount, direction, reason, and generated operation reference. Deductions cannot overdraw the balance.',
  ],
  [
    'Credit purchases / refunds and disputes',
    'Inspect verified credit grants, reversed credits, and recorded refund/dispute states. This is Sandloom’s local event-derived state, not a complete payment-provider transaction feed.',
  ],
  [
    'Webhook journal / operation intents',
    'Inspect processing status, event IDs, reconciliation attempts, and follow-up timestamps. Filter intents by Followup = true for cases needing investigation. Checkout URLs and private provider errors are withheld.',
  ],
  [
    'Audit log',
    'One explorer over each module’s hosting database: time range, action prefix, actor and resource filters, actor names, newest-first pages without totals. Shared databases show the same journal. Raw audit payloads remain in secured operational tooling.',
  ],
  [
    'Service health',
    'Live, auto-refreshing sample of infrastructure checks, configured peer contracts, and required event-consumer status for each API process. This is not a Docker inventory or a process restart control.',
  ],
  [
    'Event delivery',
    'Outbox dead letters (requeue with a reason, like sandloom outbox requeue), Streams dead-letter entries per channel (replay targeted at the failing consumer group, like sandloom streams dlq-replay), consumer state and lag, and query projections. Every command is audited.',
  ],
  [
    'Contracts & modules / Runtime settings',
    'Provided and consumed sync contracts, remote bindings with reachability and compatibility, the module load result, an allowlist of non-secret settings and infrastructure health. Read only; configuration changes go through deployment.',
  ],
];

/** Built-in operator help; the full guide is docs/admin-console/operations.md. */
export function HelpPage() {
  return (
    <div className="flex max-w-prose flex-col gap-4">
      <PageHeader
        title="Operator guide"
        description="For the SaaS owner and trusted platform operations team. Every API verifies access on the server; organization roles alone never grant entry."
      />
      <Card>
        <CardHeader>
          <CardTitle>Sections</CardTitle>
        </CardHeader>
        <dl className="divide-y divide-border">
          {SECTIONS.map(([name, description]) => (
            <div key={name} className="flex flex-col gap-1 px-4 py-3">
              <dt className="font-semibold">{name}</dt>
              <dd className="text-fg-muted">{description}</dd>
            </div>
          ))}
        </dl>
      </Card>
      <p className="flex gap-2 rounded-lg border border-warning bg-warning-muted p-4">
        <CircleAlert aria-hidden className="mt-0.5 size-4 shrink-0 text-warning" />
        Credit adjustments affect credits only; they never charge or refund money. Use the provider
        dashboard for invoices, refunds, disputes and subscription changes — verified webhooks
        update Sandloom. Demo mode never moves real money.
      </p>
      <Card>
        <CardHeader>
          <CardTitle>When a result is uncertain</CardTitle>
        </CardHeader>
        <CardContent className="text-fg-muted">
          Commands always ask for confirmation and are never retried automatically. Each carries an
          operation reference used as its idempotency key. If the response is lost, the console says
          so: inspect the current state, then retry with the same reference — the server never
          applies it twice. A new reference is issued only after a confirmed result. References live
          in this tab’s memory only.
        </CardContent>
      </Card>
      <p className="flex gap-2 rounded-lg border border-border bg-surface-muted p-4 text-fg-muted">
        <Info aria-hidden className="mt-0.5 size-4 shrink-0" />
        Tokens stay in memory: reloading the tab requires signing in again. Revocation, blocks and
        deactivation end affected sessions immediately. There is no default administrator account.
      </p>
    </div>
  );
}
