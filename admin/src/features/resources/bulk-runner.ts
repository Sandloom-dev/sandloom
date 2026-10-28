import { isUnknownOutcome } from '@/api/errors';
export interface BulkResult {
  applied: number;
  failures: { id: string; message: string }[];
}
class BulkOutcomeUnknown extends TypeError {}

/** Confirmed successes are never resent, including after a lost sibling response. */
export function bulkRunner() {
  const applied = new Map<string, Set<string>>();
  return async (
    reference: string,
    ids: string[],
    run: (id: string) => Promise<unknown>,
  ): Promise<BulkResult> => {
    const done = applied.get(reference) ?? new Set<string>();
    applied.set(reference, done);
    const failures: BulkResult['failures'] = [];
    let unknown = false;
    for (const id of ids) {
      if (done.has(id)) continue;
      try {
        await run(id);
        done.add(id);
      } catch (error) {
        if (isUnknownOutcome(error)) unknown = true;
        failures.push({ id, message: error instanceof Error ? error.message : 'Failed' });
      }
    }
    if (unknown)
      throw new BulkOutcomeUnknown('Some outcomes are unknown. Retry the original selection.');
    applied.delete(reference);
    return { applied: done.size, failures };
  };
}
