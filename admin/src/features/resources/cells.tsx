import type { ReactNode } from 'react';
import { Badge, type BadgeTone } from '@/components/ui/badge';
import { CopyText } from '@/components/patterns/copy-text';
import { formatDateTime, formatNumber, formatRelative, scalarText, UUID_RE } from '@/lib/format';
import type { Field } from '@/schema/types';

const DANGER =
  /^(failed|failure|dead|error|canceled|cancelled|past_due|unpaid|blocked|lost|degraded|down|unhealthy|disputed|needs_response)$/;
const WARNING =
  /^(pending|processing|trialing|incomplete|reconnecting|retrying|warning|stale|requires_action|under_review|warning_needs_response)$/;
const SUCCESS =
  /^(ok|active|succeeded|success|processed|paid|healthy|running|complete|completed|won|applied)$/;

export function statusTone(value: string): BadgeTone {
  const normalized = value.toLowerCase();
  if (DANGER.test(normalized)) return 'danger';
  if (WARNING.test(normalized)) return 'warning';
  if (SUCCESS.test(normalized)) return 'success';
  return 'neutral';
}

const STATUS_NAMES = /(^|_)(status|state)$/;

/** Render one manifest-typed value. Unknown shapes degrade to readable text. */
export function renderValue(field: Field, value: unknown, { dense = false } = {}): ReactNode {
  if (value === null || value === undefined || value === '')
    return <span className="text-fg-subtle">—</span>;
  if (field.type === 'boolean' && typeof value === 'boolean')
    return value ? <Badge tone="success">Yes</Badge> : <Badge tone="neutral">No</Badge>;
  if ((field.type === 'integer' || field.type === 'number') && typeof value === 'number')
    return <span className="tabular-nums">{formatNumber(value)}</span>;
  if (field.type === 'json')
    return <code className="font-mono text-xs break-all">{JSON.stringify(value)}</code>;
  const text = scalarText(value);
  if (field.format === 'date-time' || field.format === 'date')
    return (
      <time dateTime={text} title={formatDateTime(text)}>
        {dense ? formatRelative(text) : formatDateTime(text)}
      </time>
    );
  if (field.format === 'uuid' || (UUID_RE.test(text) && field.name.endsWith('id')))
    return <CopyText value={text} short={dense} />;
  if (field.enum || STATUS_NAMES.test(field.name))
    return <Badge tone={statusTone(text)}>{text.replace(/_/g, ' ')}</Badge>;
  return text;
}
