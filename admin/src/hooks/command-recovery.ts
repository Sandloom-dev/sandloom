import type { QueryClient } from '@tanstack/react-query';

export interface RecoverableCommand {
  reference: string;
  label: string;
  pending: boolean;
  retry: () => void;
}

/** In-memory, session-owned recovery. Never persist request bodies or credentials. */
class RecoveryStore {
  private rows: RecoverableCommand[] = [];
  private active = true;
  activate() {
    this.active = true;
  }
  isActive() {
    return this.active;
  }
  private listeners = new Set<() => void>();
  snapshot = () => this.rows;
  subscribe = (listener: () => void) => {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  };
  dispose() {
    this.active = false;
    this.rows = [];
    this.listeners.forEach((listener) => {
      listener();
    });
  }
  put(row: RecoverableCommand) {
    if (!this.active) return;
    this.rows = [...this.rows.filter((r) => r.reference !== row.reference), row];
    this.listeners.forEach((listener) => {
      listener();
    });
  }
  remove(reference: string) {
    this.rows = this.rows.filter((r) => r.reference !== reference);
    this.listeners.forEach((listener) => {
      listener();
    });
  }
}
const stores = new WeakMap<QueryClient, RecoveryStore>();
export function commandRecovery(client: QueryClient) {
  let store = stores.get(client);
  if (!store) {
    store = new RecoveryStore();
    stores.set(client, store);
  }
  return store;
}
