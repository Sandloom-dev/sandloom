import { createContext, useContext, type ReactNode } from 'react';
import type { AdminConfig } from './config';

const ConfigContext = createContext<AdminConfig | null>(null);

export function ConfigProvider({ config, children }: { config: AdminConfig; children: ReactNode }) {
  return <ConfigContext.Provider value={config}>{children}</ConfigContext.Provider>;
}

export function useConfig(): AdminConfig {
  const config = useContext(ConfigContext);
  if (!config) throw new Error('useConfig must be used inside <ConfigProvider>');
  return config;
}
