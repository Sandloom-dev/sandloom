import { describe, expect, it } from 'vitest';
import { platformPath } from './api';

describe('platformPath', () => {
  it('builds per-source operations paths with encoded, sorted query parameters', () => {
    expect(platformPath('billing', 'streams/dlq', { limit: 10, channel: 'a.b.v1' })).toBe(
      '/api/v1/platform/billing/streams/dlq?channel=a.b.v1&limit=10',
    );
    expect(platformPath('identity', 'audit', { cursor: undefined })).toBe(
      '/api/v1/platform/identity/audit',
    );
  });
  it('refuses a source that is not a module name', () => {
    expect(() => platformPath('../internal', 'settings')).toThrow('Invalid platform source');
  });
});
