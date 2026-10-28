import { RecordLinks } from '@/features/resources/record-links';
import type { WidgetDefinition, WidgetProps } from '@/features/widget-types';
import { ProviderCard } from './provider';

function CustomerBilling({ record }: WidgetProps) {
  const id = String(record.id);
  const psp = typeof record.stripe_customer_id === 'string' ? record.stripe_customer_id : null;
  return (
    <>
      <RecordLinks
        title="Billing history"
        links={[
          {
            label: 'Subscriptions',
            resource: 'billing_subscriptions',
            filter: { customer_id: id },
          },
          { label: 'Credit balance', resource: 'billing_balances', filter: { customer_id: id } },
          { label: 'Credit ledger', resource: 'billing_ledger', filter: { customer_id: id } },
          { label: 'Credit purchases', resource: 'billing_grants', filter: { customer_id: id } },
          { label: 'Operation intents', resource: 'billing_intents', filter: { customer_id: id } },
          ...(psp
            ? [
                {
                  label: 'Refunds and disputes',
                  resource: 'billing_payment_states',
                  filter: { psp_customer_id: psp },
                },
              ]
            : []),
        ]}
      />
      <ProviderCard customerId={psp} />
    </>
  );
}

export const billingWidgets: Record<string, WidgetDefinition> = {
  'billing.customer': { body: CustomerBilling },
};
