import { RecordLinks } from '@/features/resources/record-links';
import type { WidgetDefinition, WidgetProps } from '@/features/widget-types';
import { OrganizationWorkspace } from './workspace';

function OrganizationDirectory({ record }: WidgetProps) {
  const id = String(record.id);
  return (
    <RecordLinks
      title="Related"
      links={[
        { label: 'Members', resource: 'organization_members', filter: { organization_id: id } },
        {
          label: 'Billing customer',
          resource: 'billing_customers',
          filter: { organization_id: id },
        },
      ]}
    />
  );
}

export const tenancyWidgets: Record<string, WidgetDefinition> = {
  'tenancy.directory': { body: OrganizationDirectory },
  'tenancy.workspace': { body: OrganizationWorkspace },
};
