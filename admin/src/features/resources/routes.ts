/** URL helpers preserve the explicit tenant through every resource transition. */
function scoped(path: string, scope: string, filter: Record<string, string> = {}) {
  const params = new URLSearchParams();
  for (const [name, value] of Object.entries(filter)) params.set(`f.${name}`, value);
  if (scope) params.set('tenant', scope);
  return `${path}${params.size ? `?${params}` : ''}`;
}
export function listHref(
  resource: string,
  filter: Record<string, string> = {},
  scope = '',
): string {
  return scoped(`/${encodeURIComponent(resource)}`, scope, filter);
}
export function recordHref(resource: string, id: string | number, scope = ''): string {
  return scoped(`/${encodeURIComponent(resource)}/${encodeURIComponent(String(id))}`, scope);
}
export function formHref(resource: string, id: string | number | undefined, scope = ''): string {
  return scoped(
    id === undefined
      ? `/${encodeURIComponent(resource)}/new`
      : `/${encodeURIComponent(resource)}/${encodeURIComponent(String(id))}/edit`,
    scope,
  );
}
