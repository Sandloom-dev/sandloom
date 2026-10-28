import { Link, useNavigate } from 'react-router-dom';
import { useQueryClient } from '@tanstack/react-query';
import { Pencil, Trash2 } from 'lucide-react';
import { api } from '@/api/client';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Skeleton } from '@/components/ui/skeleton';
import { ConfirmDialog } from '@/components/patterns/confirm-dialog';
import { CopyText } from '@/components/patterns/copy-text';
import { DescriptionList } from '@/components/patterns/description-list';
import { EntityHeader } from '@/components/patterns/entity-header';
import { ErrorState } from '@/components/patterns/states';
import { useCommand } from '@/hooks/use-command';
import type { AdminResource, Field } from '@/schema/types';
import { endpoint } from '@/schema/values';
import { useSubject } from '@/app/session';
import { widget } from '@/features/widgets';
import type { WidgetProps } from '@/features/widget-types';
import { useState } from 'react';
import { ActionDialog } from './action-dialog';
import { ActivityCard, auditKeys } from './activity';
import { renderValue } from './cells';
import { resourceKeys, scopeHeaders, useResourceRecord, type ResourceRecord } from './data';
import { formHref, listHref } from './routes';
import { scalarText } from '@/lib/format';

function titleField(resource: AdminResource): Field | undefined {
  return resource.show.find(
    (field) => field.type === 'string' && !field.format && !field.enum && field.name !== 'id',
  );
}

/** Generated detail screen: header, properties, widgets and the audit trail. */
export function ResourceDetailPage({
  resource,
  id,
  scope,
}: {
  resource: AdminResource;
  id: string;
  scope: string;
}) {
  const subject = useSubject();
  const record = useResourceRecord(resource, id, scope);
  if (record.isPending)
    return (
      <div aria-busy="true" aria-label="Loading record" className="flex flex-col gap-4">
        <Skeleton className="h-12 w-1/3" />
        <Skeleton className="h-40 w-full" />
      </div>
    );
  if (record.isError)
    return <ErrorState error={record.error} onRetry={() => void record.refetch()} />;
  const data = record.data;
  // A command changes the record and appends to its audit trail.
  const invalidate = [resourceKeys.resource(subject, resource.name), auditKeys.all(subject)];
  const props: WidgetProps = { resource, record: data, scope, invalidate };
  const widgets = resource.show_widgets.map((name) => ({ name, ...widget(name) }));
  const title = titleField(resource);
  const email = resource.show.find((field) => field.format === 'email');
  const flags = resource.show.filter(
    (field) => field.type === 'boolean' && typeof data[field.name] === 'boolean',
  );
  const properties = resource.show.filter((field) => field !== title && !flags.includes(field));
  const toolbar = resource.actions.filter((action) => action.placement === 'toolbar');
  return (
    <div className="flex flex-col gap-5">
      <EntityHeader
        title={scalarText(title ? data[title.name] : undefined) || scalarText(data.id)}
        subtitle={
          <>
            {email && typeof data[email.name] === 'string' && (
              <span>{String(data[email.name])}</span>
            )}
            <CopyText value={String(data.id)} className="text-fg-subtle" />
          </>
        }
        badges={flags.map((field) => (
          <Badge key={field.name} tone={data[field.name] ? 'success' : 'neutral'}>
            {data[field.name] ? field.label : `Not ${field.label.toLowerCase()}`}
          </Badge>
        ))}
        actions={
          <>
            {widgets.map(
              ({ name, actions: Actions }) => Actions && <Actions key={name} {...props} />,
            )}
            {toolbar.map((action) => (
              <ActionDialog
                key={action.name}
                action={action}
                resource={resource}
                record={data}
                scope={scope}
                invalidate={invalidate}
              />
            ))}
            {resource.edit && (
              <Button asChild>
                <Link to={formHref(resource.name, data.id, scope)}>
                  <Pencil aria-hidden />
                  Edit
                </Link>
              </Button>
            )}
            {resource.delete && <DeleteRecord resource={resource} record={data} scope={scope} />}
          </>
        }
      />
      {widgets.map(({ name, banner: Banner }) => Banner && <Banner key={name} {...props} />)}
      <div className="flex flex-wrap items-start gap-4">
        <div className="flex min-w-0 flex-1 basis-96 flex-col gap-4">
          <Card>
            <CardHeader>
              <CardTitle>Details</CardTitle>
            </CardHeader>
            <CardContent>
              <DescriptionList
                items={properties.map((field) => ({
                  key: field.name,
                  label: field.label,
                  value: renderValue(field, data[field.name]),
                }))}
              />
            </CardContent>
          </Card>
          {widgets.map(({ name, body: Body }) => Body && <Body key={name} {...props} />)}
        </div>
        <div className="flex min-w-0 flex-1 basis-96 flex-col gap-4">
          <ActivityCard resource={resource} record={data} />
        </div>
      </div>
    </div>
  );
}

function DeleteRecord({
  resource,
  record,
  scope,
}: {
  resource: AdminResource;
  record: ResourceRecord;
  scope: string;
}) {
  const subject = useSubject();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const command = useCommand<null, unknown>({
    label: `Delete · ${resource.label} · ${record.id}`,
    run: (_input, reference) => {
      if (!resource.delete) throw new Error('Delete is not admitted');
      return api.request(endpoint(resource.delete, resource, record.id, scope), {
        method: resource.delete.method,
        headers: { ...scopeHeaders(resource, scope), 'Idempotency-Key': reference },
      });
    },
    success: 'Deleted',
    onSuccess: () => {
      queryClient.removeQueries({ queryKey: resourceKeys.resource(subject, resource.name) });
      void navigate(listHref(resource.name, {}, scope));
    },
  });
  return (
    <>
      <Button
        variant="danger-outline"
        onClick={() => {
          setOpen(true);
        }}
      >
        <Trash2 aria-hidden />
        Delete
      </Button>
      <ConfirmDialog
        open={open}
        onOpenChange={setOpen}
        title={`Delete this ${resource.label.toLowerCase()} record?`}
        description="The server performs and audits the deletion; some records are soft-deleted."
        confirmLabel="Delete"
        tone="danger"
        reference={command.reference}
        state={command.state}
        errorMessage={command.message}
        onConfirm={() => {
          command.submit(null);
        }}
      />
    </>
  );
}
