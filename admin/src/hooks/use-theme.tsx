import { createContext, useContext, useEffect, useMemo, type ReactNode } from 'react';
import { useStoredState } from './use-stored-state';

export type ThemePreference = 'system' | 'light' | 'dark';
const PREFERENCES: readonly ThemePreference[] = ['system', 'light', 'dark'];

interface ThemeContextValue {
  theme: ThemePreference;
  setTheme: (theme: ThemePreference) => void;
}
const ThemeContext = createContext<ThemeContextValue | null>(null);

/** Applies the preference as data-theme on <html>; tokens.css does the rest. */
export function ThemeProvider({ children }: { children: ReactNode }) {
  const [theme, setTheme] = useStoredState<ThemePreference>('sandloom.theme', 'system', (raw) =>
    PREFERENCES.find((p) => p === raw),
  );
  useEffect(() => {
    const root = document.documentElement;
    if (theme === 'system') root.removeAttribute('data-theme');
    else root.setAttribute('data-theme', theme);
  }, [theme]);
  const value = useMemo(() => ({ theme, setTheme }), [theme, setTheme]);
  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>;
}

export function useTheme(): ThemeContextValue {
  const value = useContext(ThemeContext);
  if (!value) throw new Error('useTheme must be used inside <ThemeProvider>');
  return value;
}
