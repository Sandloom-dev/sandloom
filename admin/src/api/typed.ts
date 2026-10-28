/**
 * Compile-time typed access to the public API, derived from the generated
 * OpenAPI types (npm run contracts). Paths, path/query parameters, request
 * bodies and responses are all checked; transport and session handling stay
 * in ApiClient.
 */
import type { paths } from './generated';
import type { ApiClient } from './client';

type Method = 'get' | 'post' | 'put' | 'patch' | 'delete';
type Operation<P extends keyof paths, M extends Method> = NonNullable<paths[P][M]>;
export type PathsWith<M extends Method> = {
  [P in keyof paths]: paths[P][M] extends { responses: unknown } ? P : never;
}[keyof paths];

type JsonContent<R> = R extends { content: { 'application/json': infer T } } ? T : never;
type SuccessOf<R> = {
  [K in keyof R]: K extends 200 | 201 | 202 ? JsonContent<R[K]> : K extends 204 ? null : never;
}[keyof R];
export type ResponseOf<P extends keyof paths, M extends Method> =
  Operation<P, M> extends { responses: infer R } ? SuccessOf<R> : never;

type ParamsOf<O> = O extends { parameters: infer X } ? X : never;
type PathParams<O> = ParamsOf<O> extends { path: infer X } ? X : never;
type QueryParams<O> = ParamsOf<O> extends { query?: infer X } ? X : never;
type BodyOf<O> = O extends { requestBody?: infer RB }
  ? [NonNullable<RB>] extends [never]
    ? never
    : NonNullable<RB> extends { content: { 'application/json': infer B } }
      ? undefined extends RB
        ? B | undefined
        : B
      : never
  : never;

export type RequestOptions<P extends keyof paths, M extends Method> = {
  signal?: AbortSignal;
  headers?: Record<string, string>;
} & ([PathParams<Operation<P, M>>] extends [never]
  ? { path?: never }
  : { path: PathParams<Operation<P, M>> }) &
  ([QueryParams<Operation<P, M>>] extends [undefined]
    ? { query?: never }
    : { query?: QueryParams<Operation<P, M>> }) &
  ([BodyOf<Operation<P, M>>] extends [never]
    ? { body?: never }
    : { body: BodyOf<Operation<P, M>> });

/** Fill `{name}` placeholders; every value is URI-encoded. */
export function fillPath(template: string, values: Record<string, unknown> = {}): string {
  return template.replace(/\{([^}]+)\}/g, (_match, name: string) => {
    const value = values[name];
    if (value === undefined || value === null || value === '')
      throw new Error(`Missing path parameter: ${name}`);
    if (typeof value !== 'string' && typeof value !== 'number')
      throw new Error(`Unsupported path parameter: ${name}`);
    return encodeURIComponent(value);
  });
}

export function queryString(query: Record<string, unknown> = {}): string {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query).sort(([a], [b]) => a.localeCompare(b))) {
    if (value === undefined || value === null || value === '') continue;
    if (typeof value !== 'string' && typeof value !== 'number' && typeof value !== 'boolean')
      throw new Error(`Unsupported query parameter: ${key}`);
    params.set(key, String(value));
  }
  return params.size ? `?${params.toString()}` : '';
}

export function typedApi(client: ApiClient) {
  function call<P extends PathsWith<M>, M extends Method>(
    method: M,
    path: P,
    options: RequestOptions<P, M>,
  ): Promise<ResponseOf<P, M>> {
    const { signal, headers, body } = options as {
      signal?: AbortSignal;
      headers?: Record<string, string>;
      body?: unknown;
    };
    const { path: params, query } = options as {
      path?: Record<string, unknown>;
      query?: Record<string, unknown>;
    };
    return client.request<ResponseOf<P, M>>(`${fillPath(path, params)}${queryString(query)}`, {
      method: method.toUpperCase(),
      ...(signal ? { signal } : {}),
      ...(headers ? { headers } : {}),
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    });
  }
  return {
    get: <P extends PathsWith<'get'>>(path: P, options: RequestOptions<P, 'get'>) =>
      call('get', path, options),
    post: <P extends PathsWith<'post'>>(path: P, options: RequestOptions<P, 'post'>) =>
      call('post', path, options),
    put: <P extends PathsWith<'put'>>(path: P, options: RequestOptions<P, 'put'>) =>
      call('put', path, options),
    patch: <P extends PathsWith<'patch'>>(path: P, options: RequestOptions<P, 'patch'>) =>
      call('patch', path, options),
    delete: <P extends PathsWith<'delete'>>(path: P, options: RequestOptions<P, 'delete'>) =>
      call('delete', path, options),
  };
}
