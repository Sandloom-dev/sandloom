import { act, renderHook, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import type { ReactNode } from 'react';
import { describe, expect, it, vi } from 'vitest';
import { HttpError } from '@/api/client';
import { useCommand } from './use-command';

function setup(run: (input: string, reference: string) => Promise<string>) {
  const client = new QueryClient();
  const invalidate = vi.spyOn(client, 'invalidateQueries');
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
  const hook = renderHook(
    () => useCommand<string, string>({ run, success: 'Done', invalidate: [['things']] }),
    { wrapper },
  );
  return { hook, invalidate };
}

describe('useCommand', () => {
  it('passes a stable reference and renews it only after a confirmed success', async () => {
    const references: string[] = [];
    const { hook, invalidate } = setup(async (_input, reference) => {
      references.push(reference);
      return 'ok';
    });
    const first = hook.result.current.reference;
    act(() => {
      hook.result.current.submit('a');
    });
    await waitFor(() => {
      expect(hook.result.current.state).toBe('success');
    });
    expect(references).toEqual([first]);
    expect(hook.result.current.reference).not.toBe(first);
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ['things'] });
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ['audit'] });
  });

  it('keeps the reference after a lost response and refuses to start a new operation', async () => {
    const references: string[] = [];
    const { hook } = setup(async (_input, reference) => {
      references.push(reference);
      throw new HttpError('Bad gateway', 502);
    });
    act(() => {
      hook.result.current.submit('a');
    });
    await waitFor(() => {
      expect(hook.result.current.state).toBe('unknown');
    });
    const kept = hook.result.current.reference;
    act(() => {
      hook.result.current.reset();
      hook.result.current.dismiss();
    });
    expect(hook.result.current.reference).toBe(kept);
    expect(hook.result.current.state).toBe('unknown');
    act(() => {
      hook.result.current.submit('a');
    });
    await waitFor(() => {
      expect(references).toEqual([kept, kept]);
    });
  });

  it('reports a definite failure with its message and never retries by itself', async () => {
    const run = vi.fn(async () => {
      throw new HttpError('Operators cannot block themselves', 409);
    });
    const { hook } = setup(run);
    act(() => {
      hook.result.current.submit('a');
    });
    await waitFor(() => {
      expect(hook.result.current.state).toBe('error');
    });
    expect(hook.result.current.message).toBe('Operators cannot block themselves');
    expect(run).toHaveBeenCalledOnce();
  });
  it('retries the original input and endpoint even after callers change them', async () => {
    const calls: string[] = [];
    const client = new QueryClient();
    const wrapper = ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    );
    const hook = renderHook(
      ({ endpoint }) =>
        useCommand<{ amount: number }, string>({
          run: async (input, reference) => {
            calls.push(`${endpoint}:${input.amount}:${reference}`);
            if (calls.length === 1) throw new HttpError('Bad gateway', 502);
            return 'ok';
          },
          success: 'Done',
        }),
      { wrapper, initialProps: { endpoint: '/a' } },
    );
    const input = { amount: 10 };
    const reference = hook.result.current.reference;
    act(() => {
      hook.result.current.submit(input);
    });
    await waitFor(() => {
      expect(hook.result.current.state).toBe('unknown');
    });
    input.amount = 999;
    hook.rerender({ endpoint: '/b' });
    act(() => {
      hook.result.current.submit({ amount: 20 });
    });
    await waitFor(() => {
      expect(hook.result.current.state).toBe('success');
    });
    expect(calls).toEqual([`/a:10:${reference}`, `/a:10:${reference}`]);
  });

  it('ignores a second submit before the first request settles', async () => {
    const run = vi.fn(async () => 'ok');
    const { hook } = setup(run);
    act(() => {
      hook.result.current.submit('a');
      hook.result.current.submit('b');
    });
    await waitFor(() => {
      expect(run).toHaveBeenCalledOnce();
    });
  });
});

it('preserves the exact retry after its originating screen unmounts', async () => {
  const client = new QueryClient();
  const calls: string[] = [];
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
  const { commandRecovery } = await import('./command-recovery');
  const onSuccess = vi.fn();
  const hook = renderHook(
    () =>
      useCommand<string, string>({
        run: async (input) => {
          calls.push(input);
          if (calls.length === 1) throw new HttpError('Lost', 502);
          return 'ok';
        },
        success: 'Saved',
        onSuccess,
      }),
    { wrapper },
  );
  act(() => {
    hook.result.current.submit('original');
  });
  await waitFor(() => {
    expect(hook.result.current.state).toBe('unknown');
  });
  hook.unmount();
  act(() => {
    commandRecovery(client).snapshot()[0]!.retry();
  });
  await waitFor(() => {
    expect(commandRecovery(client).snapshot()).toHaveLength(0);
  });
  expect(calls).toEqual(['original', 'original']);
  expect(onSuccess).not.toHaveBeenCalled();
  expect(commandRecovery(new QueryClient()).snapshot()).toHaveLength(0);
});

it('cannot replay a captured operation after its session has been disposed', async () => {
  const client = new QueryClient();
  const { commandRecovery } = await import('./command-recovery');
  const run = vi.fn(async () => {
    throw new HttpError('Lost', 502);
  });
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
  const hook = renderHook(() => useCommand({ run, success: 'Done' }), { wrapper });
  act(() => {
    hook.result.current.submit(null);
  });
  await waitFor(() => {
    expect(hook.result.current.state).toBe('unknown');
  });
  const retry = commandRecovery(client).snapshot()[0]!.retry;
  commandRecovery(client).dispose();
  act(() => {
    retry();
  });
  expect(run).toHaveBeenCalledOnce();
  expect(commandRecovery(client).snapshot()).toEqual([]);
});

it('does not turn a confirmed write into a retryable failure when its screen callback throws', async () => {
  const client = new QueryClient();
  const run = vi.fn(async () => 'saved');
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
  const hook = renderHook(
    () =>
      useCommand({
        run,
        success: 'Saved',
        onSuccess: () => {
          throw new TypeError('Screen update failed');
        },
      }),
    { wrapper },
  );
  act(() => {
    hook.result.current.submit(null);
  });
  await waitFor(() => {
    expect(hook.result.current.state).toBe('success');
  });
  expect(run).toHaveBeenCalledOnce();
});

it('completes local selection/dialog state before refreshed results remove its screen', async () => {
  const client = new QueryClient();
  const onSuccess = vi.fn();
  const screen: { close?: () => void } = {};
  const invalidate = vi.spyOn(client, 'invalidateQueries').mockImplementation(async () => {
    screen.close?.();
  });
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
  const hook = renderHook(
    () =>
      useCommand({
        run: async () => 'ok',
        success: 'Saved',
        invalidate: [['filtered-list']],
        onSuccess,
      }),
    { wrapper },
  );
  screen.close = hook.unmount;
  act(() => {
    hook.result.current.submit(null);
  });
  await waitFor(() => {
    expect(invalidate).toHaveBeenCalled();
  });
  expect(onSuccess).toHaveBeenCalledOnce();
});
