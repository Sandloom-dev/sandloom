import { GrantEditor } from './grant-editor';
import { AccountSafetyActions, AccountSafetyBanner } from './account-safety';
import { RecordLinks } from '@/features/resources/record-links';
import type { WidgetDefinition, WidgetProps } from '@/features/widget-types';

function UserRoles({ record }: WidgetProps) {
  const base = `/api/v1/users/${encodeURIComponent(String(record.id))}/roles`;
  return (
    <GrantEditor
      title="Platform roles"
      currentUrl={base}
      optionsUrl="/api/v1/roles"
      commandUrl={base}
      note="Changing roles ends the user’s active sessions."
    />
  );
}

function RolePermissions({ record }: WidgetProps) {
  const id = encodeURIComponent(String(record.id));
  return (
    <GrantEditor
      title="Permissions"
      currentUrl={`/api/v1/roles/${id}`}
      field="permissions"
      optionsUrl="/api/v1/permissions"
      commandUrl={`/api/v1/roles/${id}/permissions`}
      note="Holders of this role get the change at their next sign-in or token refresh; system roles are protected by the server."
    />
  );
}

function UserDirectory({ record }: WidgetProps) {
  return (
    <RecordLinks
      title="Related"
      links={[
        {
          label: 'Organization memberships',
          resource: 'organization_members',
          filter: { user_id: String(record.id) },
        },
        {
          label: 'Billing customer',
          resource: 'billing_customers',
          filter: { user_id: String(record.id) },
        },
      ]}
    />
  );
}

export const identityWidgets: Record<string, WidgetDefinition> = {
  'identity.account-safety': { actions: AccountSafetyActions, banner: AccountSafetyBanner },
  'identity.user-roles': { body: UserRoles },
  'identity.role-permissions': { body: RolePermissions },
  'identity.directory': { body: UserDirectory },
};
