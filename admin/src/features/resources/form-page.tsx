import { useState, type SubmitEvent } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { CircleAlert } from 'lucide-react';
import { api } from '@/api/client';
import { Button } from '@/components/ui/button';
import { Card, CardContent } from '@/components/ui/card';
import { PageHeader } from '@/components/patterns/page-header';
import { ErrorState, UnknownOutcome } from '@/components/patterns/states';
import { useCommand } from '@/hooks/use-command';
import type { AdminResource, FormOperation } from '@/schema/types';
import { endpoint, payload } from '@/schema/values';
import { useSubject } from '@/app/session';
import { FormField } from './form-field';
import { widget } from '@/features/widgets';
import { auditKeys } from './activity';
import { fieldErrors } from './form-errors';
import { resourceKeys, scopeHeaders, useResourceRecord, type ResourceRecord } from './data';
import { listHref, recordHref } from './routes';

function initialValues(op: FormOperation, record?: ResourceRecord): Record<string, unknown> {
  return Object.fromEntries(
    op.inputs.map((field) => [field.name, record ? record[field.name] : field.default]),
  );
}

/** Create (no id) or edit (id) form generated from the admitted operation. */
export function ResourceFormPage({
  resource,
  id,
  scope,
}: {
  resource: AdminResource;
  id?: string;
  scope: string;
}) {
  const record = useResourceRecord(resource, id ?? '', scope);
  const op = id ? resource.edit : resource.create;
  if (!op) return <ErrorState error={new Error('This operation is not available.')} />;
  if (id && record.isError) return <ErrorState error={record.error} />;
  if (id && !record.data) return null;
  return (
    <Form
      key={`${id ?? 'new'}:${scope}`}
      resource={resource}
      op={op}
      record={record.data}
      scope={scope}
    />
  );
}

function Form({
  resource,
  op,
  record,
  scope,
}: {
  resource: AdminResource;
  op: FormOperation;
  record: ResourceRecord | undefined;
  scope: string;
}) {
  const subject = useSubject();
  const navigate = useNavigate();
  const creating = record === undefined;
  const [values, setValues] = useState(() => initialValues(op, record));
  const command = useCommand<Record<string, unknown>, ResourceRecord>({
    label: `${creating ? 'Create' : 'Save'} · ${resource.label}${record ? ` · ${record.id}` : ''}`,
    run: (input, reference) =>
      api.request<ResourceRecord>(endpoint(op, resource, record?.id, scope), {
        method: op.method,
        headers: { ...scopeHeaders(resource, scope), 'Idempotency-Key': reference },
        body: JSON.stringify(payload(op, input, creating)),
      }),
    success: creating ? 'Created' : 'Saved',
    invalidate: [resourceKeys.resource(subject, resource.name)],
    onSuccess: (saved) => {
      void navigate(
        resource.detail
          ? recordHref(resource.name, saved.id, scope)
          : listHref(resource.name, {}, scope),
      );
    },
  });
  const errors = command.state === 'error' ? fieldErrors(command.error) : {};
  const cancel = record
    ? recordHref(resource.name, record.id, scope)
    : listHref(resource.name, {}, scope);
  const submit = (event: SubmitEvent) => {
    event.preventDefault();
    command.submit(values);
  };
  return (
    <div className="flex max-w-prose flex-col gap-4">
      <PageHeader title={creating ? `New · ${resource.label}` : `Edit · ${resource.label}`} />
      <Card>
        <CardContent>
          <form className="flex flex-col gap-4" onSubmit={submit} noValidate>
            <fieldset
              disabled={command.pending || command.state === 'unknown'}
              className="flex flex-col gap-4"
            >
              {op.inputs.map((field) => (
                <FormField
                  key={field.name}
                  field={field}
                  value={values[field.name]}
                  error={errors[field.name]}
                  onChange={(value) => {
                    setValues((current) => ({ ...current, [field.name]: value }));
                  }}
                />
              ))}
            </fieldset>
            {command.state === 'error' && Object.keys(errors).length === 0 && (
              <p role="alert" className="flex gap-2 rounded-md bg-danger-muted p-3 text-danger">
                <CircleAlert aria-hidden className="mt-0.5 size-4 shrink-0" />
                {command.message}
              </p>
            )}
            {command.state === 'unknown' && (
              <UnknownOutcome
                what="the change was saved"
                reference={command.reference}
                onRetry={() => {
                  command.retry();
                }}
              />
            )}
            <div className="flex gap-2">
              <Button
                type="submit"
                variant="primary"
                loading={command.pending}
                disabled={command.state === 'unknown'}
              >
                {creating ? 'Create' : 'Save changes'}
              </Button>
              <Button asChild variant="ghost">
                <Link to={cancel}>Cancel</Link>
              </Button>
            </div>
          </form>
        </CardContent>
      </Card>
      {record &&
        resource.edit_widgets.map((name) => {
          const { actions: Actions, banner: Banner, body: Body } = widget(name);
          const props = {
            resource,
            record,
            scope,
            invalidate: [resourceKeys.resource(subject, resource.name), auditKeys.all(subject)],
          };
          return (
            <div key={name} className="flex flex-col gap-3">
              {Actions && <Actions {...props} />}
              {Banner && <Banner {...props} />}
              {Body && <Body {...props} />}
            </div>
          );
        })}
    </div>
  );
}
