import { useCallback, useState } from 'react';

/**
 * Per-viewer convenience state in localStorage (theme, density, saved views).
 * Storage can be missing or throw (private mode, blocked site data): the UI
 * then keeps working with the in-memory value. Never use it for security
 * state — tokens live in memory only.
 */
export function useStoredState<T>(
  key: string,
  initial: T,
  parse: (raw: unknown) => T | undefined,
): [T, (value: T) => void] {
  const [value, setValue] = useState<T>(() => {
    try {
      const raw = window.localStorage.getItem(key);
      return raw === null ? initial : (parse(JSON.parse(raw)) ?? initial);
    } catch {
      return initial;
    }
  });
  const update = useCallback(
    (next: T) => {
      setValue(next);
      try {
        window.localStorage.setItem(key, JSON.stringify(next));
      } catch {
        // Storage unavailable: keep the in-memory value only.
      }
    },
    [key],
  );
  return [value, update];
}
