import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { api } from '@/api/client';
import { useSubject } from '@/app/session';
import { platformKeys, platformPath } from './api';

/** Keyset pages; a source/channel/size change starts a fresh cursor chain. */
export function useIncidentPage<T>(
  source: string | undefined,
  view: 'outbox/dead' | 'streams/dlq',
  params: Record<string, string> = {},
  enabled = true,
) {
  const subject = useSubject();
  const [size, setSize] = useState(100);
  const key = JSON.stringify([source, view, params, size]);
  const [history, setHistory] = useState<{ key: string; cursors: string[] }>({ key, cursors: [] });
  const cursors = history.key === key ? history.cursors : [];
  const cursor = cursors.at(-1);
  const queryParams = { ...params, limit: size, cursor };
  const query = useQuery({
    queryKey: platformKeys.view(subject, source ?? '', view, queryParams),
    enabled: Boolean(source) && enabled,
    queryFn: async ({ signal }) => {
      const response = await api.response<T[]>(platformPath(source ?? '', view, queryParams), {
        signal,
      });
      return { rows: response.data, next: response.headers.get('X-Next-Cursor') };
    },
  });
  return {
    ...query,
    resetPage: () => {
      setHistory({ key, cursors: [] });
    },
    data: query.data?.rows,
    pagination: {
      page: cursors.length + 1,
      pageSize: size,
      pageSizes: [50, 100, 200],
      hasNextPage: Boolean(query.data?.next),
      hasPreviousPage: cursors.length > 0,
      onPageChange: (page: number) => {
        if (page === cursors.length + 2 && query.data?.next)
          setHistory({ key, cursors: [...cursors, query.data.next] });
        else if (page >= 1 && page <= cursors.length + 1)
          setHistory({ key, cursors: cursors.slice(0, page - 1) });
      },
      onPageSizeChange: (next: number) => {
        if ([50, 100, 200].includes(next)) setSize(next);
      },
    },
  };
}
