import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/vite';
import { fileURLToPath } from 'node:url';

export default defineConfig({
  // The Python package serves the console at /admin/; a standalone host serves it at the root.
  base: process.env.SANDLOOM_ADMIN_BASE ?? '/',
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) },
  },
  build: {
    // The console ships as static files behind the admin nginx; no source maps leak.
    sourcemap: false,
    rollupOptions: {
      output: {
        manualChunks: (id) =>
          id.includes('node_modules/react') || id.includes('node_modules/scheduler')
            ? 'react'
            : id.includes('node_modules/@tanstack')
              ? 'tanstack'
              : id.includes('node_modules/radix-ui') || id.includes('node_modules/@radix-ui')
                ? 'radix'
                : undefined,
      },
    },
  },
  server: {
    proxy: Object.fromEntries(
      ['/auth', '/api'].map((path) => [
        path,
        {
          target: process.env.SANDLOOM_ADMIN_API_TARGET ?? 'http://127.0.0.1:2222',
          changeOrigin: true,
        },
      ]),
    ),
  },
  test: {
    environment: 'jsdom',
    globals: true,
    restoreMocks: true,
    include: ['tests/*.test.ts', 'tests/*.test.tsx', 'src/**/*.test.tsx', 'src/**/*.test.ts'],
    setupFiles: ['tests/setup.ts'],
    css: false,
  },
});
