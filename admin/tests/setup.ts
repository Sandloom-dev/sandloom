import '@testing-library/jest-dom/vitest';
import { cleanup } from '@testing-library/react';
import { afterEach, beforeEach } from 'vitest';
import { api } from '../src/api/client';
import { identityAuth } from './auth';

beforeEach(() => {
  api.configureAuth(identityAuth);
});

afterEach(() => {
  cleanup();
});
