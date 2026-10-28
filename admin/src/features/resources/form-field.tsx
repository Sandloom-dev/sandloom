import { useId } from 'react';
import { Input, NativeSelect, Textarea } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import type { Field } from '@/schema/types';
import { scalarText } from '@/lib/format';

/** Datetime inputs speak local "YYYY-MM-DDTHH:mm"; the API speaks ISO. */
function toLocalInput(value: unknown): string {
  if (typeof value !== 'string' || !value) return '';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '';
  const offset = date.getTimezoneOffset() * 60_000;
  return new Date(date.getTime() - offset).toISOString().slice(0, 16);
}

/**
 * Renders one manifest input with its declared type and constraints. The
 * value is typed (number, boolean, null) exactly as the API expects.
 */
export function FormField({
  field,
  value,
  error,
  onChange,
}: {
  field: Field;
  value: unknown;
  error?: string | undefined;
  onChange: (value: unknown) => void;
}) {
  const id = useId();
  const hintId = `${id}-hint`;
  const errorId = `${id}-error`;
  const describedBy = [field.description ? hintId : null, error ? errorId : null]
    .filter(Boolean)
    .join(' ');
  const common = {
    id,
    'aria-invalid': error ? true : undefined,
    'aria-describedby': describedBy || undefined,
    required: field.required,
  };
  let control: React.ReactNode;
  if (field.enum || field.type === 'boolean') {
    const options = field.enum
      ? field.enum.map((option) => ({ value: String(option), label: String(option), raw: option }))
      : [
          { value: 'true', label: 'Yes', raw: true },
          { value: 'false', label: 'No', raw: false },
        ];
    control = (
      <NativeSelect
        {...common}
        value={scalarText(value)}
        onChange={(event) => {
          const match = options.find((option) => option.value === event.target.value);
          onChange(match ? match.raw : field.nullable ? null : undefined);
        }}
      >
        <option value="">{field.nullable ? 'None' : 'Select…'}</option>
        {options.map((option) => (
          <option key={option.value} value={option.value}>
            {option.label}
          </option>
        ))}
      </NativeSelect>
    );
  } else if (field.type === 'integer' || field.type === 'number') {
    control = (
      <Input
        {...common}
        type="number"
        inputMode={field.type === 'integer' ? 'numeric' : 'decimal'}
        step={field.type === 'integer' ? 1 : 'any'}
        min={field.minimum}
        max={field.maximum}
        value={typeof value === 'number' ? value : ''}
        onChange={(event) => {
          onChange(event.target.value === '' ? undefined : Number(event.target.value));
        }}
      />
    );
  } else if (field.format === 'date-time') {
    control = (
      <Input
        {...common}
        type="datetime-local"
        value={toLocalInput(value)}
        onChange={(event) => {
          onChange(event.target.value ? new Date(event.target.value).toISOString() : undefined);
        }}
      />
    );
  } else if ((field.maxLength ?? 0) > 200 || field.name === 'reason') {
    control = (
      <Textarea
        {...common}
        maxLength={field.maxLength}
        value={typeof value === 'string' ? value : ''}
        onChange={(event) => {
          onChange(event.target.value);
        }}
      />
    );
  } else {
    control = (
      <Input
        {...common}
        type={field.format === 'email' ? 'email' : field.format === 'date' ? 'date' : 'text'}
        maxLength={field.maxLength}
        value={typeof value === 'string' ? value : ''}
        onChange={(event) => {
          onChange(event.target.value);
        }}
      />
    );
  }
  return (
    <div className="flex flex-col gap-1.5">
      <Label htmlFor={id}>
        {field.label}
        {!field.required && <span className="font-normal text-fg-subtle"> (optional)</span>}
      </Label>
      {control}
      {field.description && (
        <p id={hintId} className="text-xs text-fg-subtle">
          {field.description}
        </p>
      )}
      {error && (
        <p id={errorId} className="text-xs text-danger">
          {error}
        </p>
      )}
    </div>
  );
}
