import { HttpError } from './client';

/**
 * A mutation whose response was lost may or may not have committed: a network
 * failure or a gateway/provider 502/503/504 (AGENTS.md: a timed-out contract
 * call "may have committed"). The UI must say so and offer a safe retry with
 * the SAME operation reference instead of a fresh, possibly duplicate, attempt.
 */
export function isUnknownOutcome(error: unknown): boolean {
  if (error instanceof HttpError) return [502, 503, 504].includes(error.status);
  if (error instanceof DOMException && error.name === 'AbortError') return true;
  return error instanceof TypeError;
}

export function errorMessage(error: unknown, fallback = 'Something went wrong'): string {
  if (error instanceof HttpError) return error.message;
  if (error instanceof TypeError) return 'The server could not be reached.';
  return fallback;
}

export function errorStatus(error: unknown): number | undefined {
  return error instanceof HttpError ? error.status : undefined;
}
