import { useId, useState, type ReactNode } from 'react';
import { Route, Routes, useParams, useSearchParams } from 'react-router-dom';
import { FileQuestion } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Card, CardContent } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { EmptyState } from '@/components/patterns/states';
import { DashboardPage } from '@/features/dashboard/dashboard-page';
import { HelpPage } from '@/features/help/help-page';
import { AuditPage } from '@/features/platform/audit-page';
import { HealthPage } from '@/features/platform/health-page';
import { EventsPage } from '@/features/platform/events-page';
import { ContractsPage } from '@/features/platform/contracts-page';
import { SettingsPage } from '@/features/platform/settings-page';
import { ResourceDetailPage } from '@/features/resources/detail-page';
import { ResourceFormPage } from '@/features/resources/form-page';
import { ResourceListPage } from '@/features/resources/list-page';
import { UUID_RE } from '@/lib/format';
import { allowed, type AdminResource } from '@/schema/types';
import { useConfig } from './config-context';
import { useSession } from './session';

function NotFound() {
  return (
    <EmptyState
      icon={FileQuestion}
      title="Page not found"
      description="It may not exist in this deployment, or your role cannot see it."
    />
  );
}

/** Tenant-scoped resources need an explicit organization/tenant UUID (?tenant=). */
function ScopeGate({
  resource,
  children,
}: {
  resource: AdminResource;
  children: (scope: string) => ReactNode;
}) {
  const id = useId();
  const [search, setSearch] = useSearchParams();
  const scope = search.get('tenant') ?? '';
  const [draft, setDraft] = useState(scope);
  if (!resource.scope) return children('');
  if (UUID_RE.test(scope)) return children(scope);
  return (
    <Card className="max-w-prose">
      <CardContent>
        <form
          className="flex flex-col gap-2"
          onSubmit={(event) => {
            event.preventDefault();
            if (UUID_RE.test(draft)) setSearch({ tenant: draft });
          }}
        >
          <Label htmlFor={id}>{resource.scope.label}</Label>
          <Input
            id={id}
            value={draft}
            placeholder="00000000-0000-0000-0000-000000000000"
            onChange={(event) => {
              setDraft(event.target.value.trim());
            }}
          />
          <Button type="submit" variant="primary" disabled={!UUID_RE.test(draft)} className="w-fit">
            Open
          </Button>
        </form>
      </CardContent>
    </Card>
  );
}

function ResourceRoute({ view }: { view: 'list' | 'detail' | 'create' | 'edit' }) {
  const { resource: name = '', id = '' } = useParams();
  const { manifest } = useConfig();
  const { session } = useSession();
  const resource = manifest.resources.find((candidate) => candidate.name === name);
  if (!resource || !session || !allowed(resource.access, session.roles)) return <NotFound />;
  if (
    (view === 'detail' && !resource.detail) ||
    (view === 'create' && !resource.create) ||
    (view === 'edit' && !resource.edit)
  )
    return <NotFound />;
  return (
    <ScopeGate resource={resource}>
      {(scope) =>
        view === 'list' ? (
          <ResourceListPage key={`${resource.name}:${scope}`} resource={resource} scope={scope} />
        ) : view === 'detail' ? (
          <ResourceDetailPage
            key={`${resource.name}:${id}:${scope}`}
            resource={resource}
            id={id}
            scope={scope}
          />
        ) : (
          <ResourceFormPage
            key={`${resource.name}:${id}:${view}:${scope}`}
            resource={resource}
            {...(view === 'edit' ? { id } : {})}
            scope={scope}
          />
        )
      }
    </ScopeGate>
  );
}

export function AppRoutes() {
  return (
    <Routes>
      <Route index element={<DashboardPage />} />
      <Route path="platform/health" element={<HealthPage />} />
      <Route path="platform/audit" element={<AuditPage />} />
      <Route path="platform/events" element={<EventsPage />} />
      <Route path="platform/contracts" element={<ContractsPage />} />
      <Route path="platform/settings" element={<SettingsPage />} />
      <Route path="help" element={<HelpPage />} />
      <Route path=":resource" element={<ResourceRoute view="list" />} />
      <Route path=":resource/new" element={<ResourceRoute view="create" />} />
      <Route path=":resource/:id" element={<ResourceRoute view="detail" />} />
      <Route path=":resource/:id/edit" element={<ResourceRoute view="edit" />} />
      <Route path="*" element={<NotFound />} />
    </Routes>
  );
}
