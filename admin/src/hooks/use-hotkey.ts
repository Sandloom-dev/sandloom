import { useEffect, useRef } from 'react';

const isMac = typeof navigator !== 'undefined' && /Mac|iPhone|iPad/.test(navigator.userAgent);

function editable(target: EventTarget | null): boolean {
  return (
    target instanceof HTMLElement &&
    (target.isContentEditable || ['INPUT', 'TEXTAREA', 'SELECT'].includes(target.tagName))
  );
}

/**
 * Bind a shortcut such as "mod+k" (⌘ on macOS, Ctrl elsewhere) or "/".
 * Plain-key shortcuts are ignored while the user types in a field.
 */
export function useHotkey(combo: string, handler: (event: KeyboardEvent) => void): void {
  const saved = useRef(handler);
  useEffect(() => {
    saved.current = handler;
  }, [handler]);
  useEffect(() => {
    const parts = combo.toLowerCase().split('+');
    const key = parts.at(-1) ?? '';
    const mod = parts.includes('mod');
    const listener = (event: KeyboardEvent) => {
      const modPressed = isMac ? event.metaKey : event.ctrlKey;
      if (event.key.toLowerCase() !== key || mod !== modPressed) return;
      if (!mod && editable(event.target)) return;
      event.preventDefault();
      saved.current(event);
    };
    document.addEventListener('keydown', listener);
    return () => {
      document.removeEventListener('keydown', listener);
    };
  }, [combo]);
}

export const modKeyLabel = isMac ? '⌘' : 'Ctrl';
