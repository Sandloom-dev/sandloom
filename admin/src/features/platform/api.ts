import { useQuery } from '@tanstack/react-query';
import { useSearchParams } from 'react-router-dom';
import { api, type Schema } from '@/api/client';
import { queryString } from '@/api/typed';
import { useSubject } from '@/app/session';
import { sourceName, usePlatformSources } from './sources';

export type DeadLetter = Schema<'DeadLetterView'>;
export type StreamDeadLetter = Schema<'StreamDeadLetterView'>;
export type Consumer = Schema<'ConsumerView'>;
export type Projection = Schema<'ProjectionView'>;
export type Contracts = Schema<'ContractsView'>;
export type Module = Schema<'ModuleView'>;
export type RuntimeSettings = Schema<'SettingsView'>;
export type AuditRow = Schema<'AuditView'>;
export type CommandResult = Schema<'CommandResult'>;

/** Kernel operations views served per module source (one per API process when split). */
export type PlatformView =
  | 'outbox/dead'
  | 'streams/dlq'
  | 'consumers'
  | 'projections'
  | 'contracts'
  | 'modules'
  | 'settings';

export const platformKeys = {
  all: (subject: string) => ['platform', subject] as const,
  source: (subject: string, source: string) => ['platform', subject, source] as const,
  view: (subject: string, source: string, view: string, params: Record<string, unknown> = {}) =>
    ['platform', subject, source, view, params] as const,
};

/**
 * The source the operator is looking at (`?source=`), restricted to the
 * module sources the manifest admits; defaults to the first.
 */
export function useOperationsSource(): {
  sources: string[];
  source: string | undefined;
  setSource: (source: string) => void;
} {
  const sources = usePlatformSources('services').map(sourceName);
  const [search, setSearch] = useSearchParams();
  const requested = search.get('source');
  const source = sources.find((name) => name === requested) ?? sources[0];
  return {
    sources,
    source,
    setSource: (next) => {
      setSearch((current) => {
        const params = new URLSearchParams(current);
        params.set('source', next);
        return params;
      });
    },
  };
}

export function platformPath(source: string, view: string, params: Record<string, unknown> = {}) {
  if (!/^[a-z][a-z0-9_]*$/.test(source)) throw new Error('Invalid platform source');
  return `/api/v1/platform/${source}/${view}${queryString(params)}`;
}

export function usePlatformView<T>(
  source: string | undefined,
  view: PlatformView,
  params: Record<string, unknown> = {},
  options: { enabled?: boolean; refetchInterval?: number } = {},
) {
  const subject = useSubject();
  return useQuery({
    queryKey: platformKeys.view(subject, source ?? '', view, params),
    enabled: Boolean(source) && (options.enabled ?? true),
    queryFn: ({ signal }) => api.request<T>(platformPath(source ?? '', view, params), { signal }),
    ...(options.refetchInterval ? { refetchInterval: options.refetchInterval } : {}),
  });
}
