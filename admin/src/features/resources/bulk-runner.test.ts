import { expect, it, vi } from 'vitest';
import { HttpError } from '@/api/client';
import { bulkRunner } from './bulk-runner';
it('retries unknown records without resending confirmed successes', async () => {
  const execute = bulkRunner();
  const run = vi.fn(async (id: string) => {
    if (id === 'b' && run.mock.calls.length === 2) throw new HttpError('Lost', 502);
  });
  await expect(execute('key', ['a', 'b', 'c'], run)).rejects.toThrow();
  expect(await execute('key', ['a', 'b', 'c'], run)).toEqual({ applied: 3, failures: [] });
  expect(run.mock.calls.map(([id]) => id)).toEqual(['a', 'b', 'c', 'b']);
});
it('reports definite per-record failures rather than treating partial success as a wholly failed operation', async () => {
  const result = await bulkRunner()('key', ['a', 'b'], async (id) => {
    if (id === 'b') throw new HttpError('Protected', 409);
  });
  expect(result).toEqual({ applied: 1, failures: [{ id: 'b', message: 'Protected' }] });
});
