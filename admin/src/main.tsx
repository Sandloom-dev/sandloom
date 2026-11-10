import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import '@/styles/index.css';
import { App } from '@/app/app';
import { loadConfig } from '@/app/config';
import { api } from '@/api/client';

const container = document.getElementById('root');
if (!container) throw new Error('Missing #root');
const root = createRoot(container);
loadConfig()
  .then((config) => {
    // Outside production a reload of this tab keeps the session (sessionStorage).
    api.configureAuth(config.auth);
    api.keepAcrossReloads(config.development);
    root.render(
      <StrictMode>
        <App config={config} />
      </StrictMode>,
    );
  })
  .catch(() => {
    root.render(
      <p role="alert" className="m-6 rounded-lg border border-danger bg-danger-muted p-4">
        Could not load the admin configuration. Contact your administrator.
      </p>,
    );
  });
