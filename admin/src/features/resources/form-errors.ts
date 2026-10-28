import { HttpError } from '@/api/client';

/** Field errors from a local validation or a FastAPI 422 body, keyed by input name. */
export function fieldErrors(error: unknown): Record<string, string> {
  if (!(error instanceof HttpError) || error.status !== 422) return {};
  const body = error.body as {
    errors?: Record<string, string>;
    detail?: { loc?: unknown[]; msg?: string }[];
  } | null;
  if (body?.errors) return body.errors;
  const result: Record<string, string> = {};
  if (Array.isArray(body?.detail))
    for (const item of body.detail)
      if (item.loc?.[0] === 'body' && typeof item.loc[1] === 'string')
        result[item.loc[1]] = item.msg ?? 'Invalid value';
  return result;
}
