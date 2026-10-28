import { useCallback, useMemo } from 'react';
import { useSearchParams } from 'react-router-dom';
import type { AdminResource } from '@/schema/types';
import type { ListParams } from './data';

const FILTER_PREFIX = 'f.';

export function pageSizesFor(resource: AdminResource): number[] {
  return [10, 25, 50, 100].filter((size) => size <= resource.pagination.max_page_size);
}

/**
 * List state lives in the URL (?page=2&size=25&sort=-name&f.is_active=true) so
 * views are shareable and survive reloads; unknown keys are ignored.
 */
export function useListUrlState(resource: AdminResource, scope: string) {
  const [search, setSearch] = useSearchParams();
  const params = useMemo<ListParams>(() => {
    const sizes = pageSizesFor(resource);
    const fallbackSize = sizes.includes(25)
      ? 25
      : (sizes.at(-1) ?? resource.pagination.max_page_size);
    const size = Number(search.get('size'));
    const page = Number(search.get('page'));
    const sortRaw = search.get('sort') ?? '';
    const sortField = sortRaw.replace(/^-/, '');
    const filters: Record<string, string> = {};
    for (const field of resource.filters) {
      const value = search.get(FILTER_PREFIX + field.name);
      if (value !== null && value !== '') filters[field.name] = value;
    }
    return {
      page: Number.isSafeInteger(page) && page > 0 ? page : 1,
      pageSize: sizes.includes(size) ? size : fallbackSize,
      sort: resource.sort_fields.includes(sortField)
        ? { field: sortField, desc: sortRaw.startsWith('-') }
        : null,
      filters,
      scope,
    };
  }, [resource, search, scope]);

  const update = useCallback(
    (patch: Partial<Omit<ListParams, 'scope'>>) => {
      setSearch(
        (current) => {
          const next = new URLSearchParams(current);
          if (patch.filters) {
            for (const key of [...next.keys()]) if (key.startsWith(FILTER_PREFIX)) next.delete(key);
            for (const [name, value] of Object.entries(patch.filters))
              next.set(FILTER_PREFIX + name, value);
          }
          if (patch.sort !== undefined) {
            if (patch.sort) next.set('sort', `${patch.sort.desc ? '-' : ''}${patch.sort.field}`);
            else next.delete('sort');
          }
          if (patch.pageSize !== undefined) next.set('size', String(patch.pageSize));
          // Any change other than paging restarts from page 1 (cursors are per query).
          const page = patch.page ?? 1;
          if (page > 1) next.set('page', String(page));
          else next.delete('page');
          return next;
        },
        { replace: patch.page === undefined },
      );
    },
    [setSearch],
  );
  return { params, update };
}
