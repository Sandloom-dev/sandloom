import { useCallback, useEffect, useRef, useState } from 'react';
import { useMutation, useQueryClient, type QueryKey } from '@tanstack/react-query';
import { errorMessage, isUnknownOutcome } from '@/api/errors';
import { toast } from '@/components/ui/toast';
import { commandRecovery } from './command-recovery';

export type CommandState = 'idle' | 'pending' | 'success' | 'error' | 'unknown';

export interface CommandOptions<TInput, TResult> {
  label?: string;
  run: (input: TInput, reference: string) => Promise<TResult>;
  success: string | ((result: TResult, input: TInput) => string);
  invalidate?: readonly QueryKey[];
  onSuccess?: (result: TResult, input: TInput) => void;
}
interface Attempt<TInput, TResult> {
  input: TInput;
  reference: string;
  options: CommandOptions<TInput, TResult>;
}

/** One immutable attempt: its input, endpoint closure and reference survive retries. */
export function useCommand<TInput, TResult>(options: CommandOptions<TInput, TResult>) {
  const client = useQueryClient();
  const recovery = commandRecovery(client);
  const [reference, setReference] = useState(() => crypto.randomUUID());
  const [outcome, setOutcome] = useState<CommandState>('idle');
  const attempt = useRef<Attempt<TInput, TResult> | null>(null);
  const busy = useRef(false);
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);
  const refresh = async (value: Attempt<TInput, TResult>) => {
    await Promise.all(
      [['audit'], ...(value.options.invalidate ?? [])].map((queryKey) =>
        client.invalidateQueries({ queryKey }),
      ),
    );
  };
  const mutation = useMutation<TResult, unknown, Attempt<TInput, TResult>>({
    mutationFn: (value) => value.options.run(structuredClone(value.input), value.reference),
    retry: false,
    onSuccess: async (result, value) => {
      if (!recovery.isActive()) return;
      recovery.remove(value.reference);
      busy.current = false;
      attempt.current = null;
      if (mounted.current) {
        setOutcome('success');
        setReference(crypto.randomUUID());
      }
      // The server already confirmed the write. A rendering/navigation failure
      // must never be reclassified as an unknown write with a fresh reference.
      let screenFailed = false;
      try {
        const title = value.options.success;
        toast({
          tone: 'success',
          title: typeof title === 'string' ? title : title(result, value.input),
        });
        if (mounted.current) value.options.onSuccess?.(result, value.input);
      } catch {
        screenFailed = true;
      }
      // Clear selection/dialog state before a filtered refetch can unmount it.
      // Refresh is still required if a local callback failed.
      try {
        await refresh(value);
      } catch {
        screenFailed = true;
      }
      if (screenFailed && recovery.isActive()) {
        toast({
          tone: 'info',
          title: 'The change was applied, but the screen could not update. Refresh to inspect it.',
        });
      }
    },
    onError: async (error, value) => {
      if (!recovery.isActive()) return;
      busy.current = false;
      if (isUnknownOutcome(error)) {
        if (mounted.current) setOutcome('unknown');
        recovery.put({
          reference: value.reference,
          label:
            value.options.label ??
            (typeof value.options.success === 'string' ? value.options.success : 'Change'),
          pending: false,
          retry: () => {
            execute(value);
          },
        });
        await refresh(value);
      } else {
        recovery.remove(value.reference);
        attempt.current = null;
        if (mounted.current) {
          setOutcome('error');
          setReference(crypto.randomUUID());
        }
      }
    },
  });
  const execute = (value: Attempt<TInput, TResult>) => {
    if (!recovery.isActive() || busy.current) return;
    busy.current = true;
    if (mounted.current) setOutcome('pending');
    recovery.put({
      reference: value.reference,
      label:
        value.options.label ??
        (typeof value.options.success === 'string' ? value.options.success : 'Change'),
      pending: true,
      retry: () => {
        execute(value);
      },
    });
    mutation.mutate(value);
  };
  const retry = () => {
    if (attempt.current) execute(attempt.current);
  };
  const submit = (input: TInput) => {
    if (!recovery.isActive() || busy.current) return;
    if (attempt.current) {
      retry();
      return;
    }
    if (recovery.snapshot().length) {
      toast({
        tone: 'error',
        title: 'Resolve the previous operation before starting another change',
      });
      return;
    }
    const value = {
      input: structuredClone(input),
      reference,
      options: { ...options, invalidate: structuredClone(options.invalidate ?? []) },
    };
    attempt.current = value;
    execute(value);
  };
  const dismiss = useCallback(() => {
    if (attempt.current || busy.current) return false;
    mutation.reset();
    setOutcome('idle');
    return true;
  }, [mutation]);
  const reset = useCallback(() => {
    if (attempt.current || busy.current) return false;
    mutation.reset();
    setOutcome('idle');
    setReference(crypto.randomUUID());
    return true;
  }, [mutation]);
  return {
    submit,
    retry,
    dismiss,
    reset,
    reference,
    state: outcome,
    pending: mutation.isPending,
    error: mutation.error,
    message: mutation.error === null ? null : errorMessage(mutation.error),
  };
}
