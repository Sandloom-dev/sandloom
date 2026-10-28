import { useSyncExternalStore } from 'react';
import { api, type Session } from '@/api/client';

/** Re-renders on login, logout, revocation and authority changes. */
export function useSession(): { epoch: number; session: Session | undefined } {
  const epoch = useSyncExternalStore(api.subscribe, () => api.epoch);
  return { epoch, session: api.snapshot };
}

export function useSubject(): string {
  const { session } = useSession();
  if (!session) throw new Error('No session');
  return session.subject;
}
