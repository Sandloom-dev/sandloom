import { useRef } from 'react';
import { keepPreviousData, useQuery } from '@tanstack/react-query';
import { api, HttpError, type ApiClient } from '@/api/client';
import { parseFilterValue } from '@/schema/filters';
import type { AdminResource } from '@/schema/types';
import { endpoint, validateValue } from '@/schema/values';
import { useSubject } from '@/app/session';

export type ResourceRecord = Record<string, unknown> & { id: string | number };

export interface ListParams {
  page: number;
  pageSize: number;
  filters: Record<string, string>;
  sort: { field: string; desc: boolean } | null;
  scope: string;
}

export interface ListPage {
  rows: ResourceRecord[];
  hasNextPage: boolean;
  hasPreviousPage: boolean;
  total?: number;
}

/** Query keys always include the session subject: no cross-session cache reuse. */
export const resourceKeys = {
  all: (subject: string) => ['resource', subject] as const,
  resource: (subject: string, name: string) => ['resource', subject, name] as const,
  list: (subject: string, name: string, params: ListParams) =>
    ['resource', subject, name, 'list', params] as const,
  record: (subject: string, name: string, scope: string, id: string) =>
    ['resource', subject, name, 'record', scope, id] as const,
};

function readRecord(value: unknown): ResourceRecord {
  if (
    !value ||
    typeof value !== 'object' ||
    !('id' in value) ||
    !['string', 'number'].includes(typeof value.id)
  )
    throw new HttpError('The API returned a record without an ID', 502);
  return value as ResourceRecord;
}

function readRows(data: unknown, resource: AdminResource): ResourceRecord[] {
  const value =
    resource.items_field && data && typeof data === 'object'
      ? (data as Record<string, unknown>)[resource.items_field]
      : data;
  if (!Array.isArray(value)) throw new HttpError('The API returned an invalid list', 502);
  return value.map(readRecord);
}

export const scopeHeaders = (resource: AdminResource, scope: string) =>
  resource.scope ? { 'X-Tenant-Id': scope } : undefined;

/**
 * One page of an admitted resource. Pagination follows the declared protocol
 * exactly: `all` slices locally, `offset` asks for one extra row (lookahead),
 * `cursor` walks opaque cursors and refuses to jump to an unseen page.
 */
export async function fetchListPage(
  client: ApiClient,
  resource: AdminResource,
  params: ListParams,
  cursors: Map<number, string>,
  signal?: AbortSignal,
): Promise<ListPage> {
  const { page, pageSize, filters, sort, scope } = params;
  const paging = resource.pagination;
  if (
    !Number.isSafeInteger(page) ||
    page < 1 ||
    !Number.isSafeInteger(pageSize) ||
    pageSize < 1 ||
    pageSize > paging.max_page_size
  )
    throw new HttpError('Invalid page', 400);
  const query = new URLSearchParams();
  for (const [name, raw] of Object.entries(filters).sort(([a], [b]) => a.localeCompare(b))) {
    const field = resource.filters.find((candidate) => candidate.name === name);
    if (!field) throw new HttpError('This API does not support that filter', 400);
    if (raw === '') continue;
    const value = parseFilterValue(field, raw);
    if (validateValue({ ...field, required: false }, value))
      throw new HttpError('Invalid filter', 400);
    query.set(name, String(value));
  }
  if (sort && resource.sort_parameter) {
    if (!resource.sort_fields.includes(sort.field)) throw new HttpError('Invalid sort order', 400);
    query.set(resource.sort_parameter, `${sort.desc ? '-' : ''}${sort.field}`);
  }
  if (paging.mode === 'offset') {
    query.set(paging.offset, String((page - 1) * pageSize));
    query.set(paging.limit, String(pageSize + 1));
  } else if (paging.mode === 'cursor') {
    query.set(paging.limit, String(pageSize));
    if (page > 1) {
      const cursor = cursors.get(page);
      if (!cursor) throw new HttpError('Start from the first page', 400);
      query.set(paging.cursor, cursor);
    }
  }
  const path = endpoint(resource.list, resource, undefined, scope);
  const { data, headers } = await client.response(path + (query.size ? `?${query}` : ''), {
    ...(signal ? { signal } : {}),
    headers: scopeHeaders(resource, scope),
  });
  const rows = readRows(data, resource);
  if (paging.mode === 'all')
    return {
      rows: rows.slice((page - 1) * pageSize, page * pageSize),
      hasPreviousPage: page > 1,
      hasNextPage: rows.length > page * pageSize,
      total: rows.length,
    };
  if (paging.mode === 'offset')
    return {
      rows: rows.slice(0, pageSize),
      hasPreviousPage: page > 1,
      hasNextPage: rows.length > pageSize,
    };
  const next = headers.get(paging.next_header);
  for (const known of [...cursors.keys()]) if (known > page) cursors.delete(known);
  if (next) cursors.set(page + 1, next);
  return { rows, hasPreviousPage: page > 1, hasNextPage: Boolean(next) };
}

export function useResourceList(resource: AdminResource, params: ListParams) {
  const subject = useSubject();
  // Cursor chains are per filter/size/scope: never reuse another query's cursor.
  const chains = useRef(new Map<string, Map<number, string>>());
  const chainKey = JSON.stringify([
    resource.name,
    params.filters,
    params.pageSize,
    params.sort,
    params.scope,
  ]);
  return useQuery({
    queryKey: resourceKeys.list(subject, resource.name, params),
    queryFn: ({ signal }) => {
      let chain = chains.current.get(chainKey);
      if (!chain) {
        chain = new Map();
        chains.current.set(chainKey, chain);
      }
      return fetchListPage(api, resource, params, chain, signal);
    },
    placeholderData: (previous, query) => {
      const prior = query?.queryKey[4];
      return prior && typeof prior === 'object' && 'scope' in prior && prior.scope === params.scope
        ? keepPreviousData(previous)
        : undefined;
    },
    enabled: !resource.scope || Boolean(params.scope),
  });
}

export function useResourceRecord(resource: AdminResource, id: string, scope: string) {
  const subject = useSubject();
  const detail = resource.detail;
  return useQuery({
    queryKey: resourceKeys.record(subject, resource.name, scope, id),
    enabled: Boolean(detail) && id !== '' && (!resource.scope || Boolean(scope)),
    queryFn: async ({ signal }) => {
      if (!detail) throw new HttpError('Operation not supported', 405);
      return readRecord(
        await api.request(endpoint(detail, resource, id, scope), {
          signal,
          headers: scopeHeaders(resource, scope),
        }),
      );
    },
  });
}
