/** Static output of sandloom admin export, version 1. No executable schema content. */
export interface Access {
  authenticated: boolean;
  roles: string[];
}
export interface Field {
  name: string;
  label: string;
  type: 'string' | 'boolean' | 'integer' | 'number' | 'json';
  required: boolean;
  nullable: boolean;
  format?: 'email' | 'uuid' | 'date' | 'date-time';
  enum?: (string | number | boolean)[];
  minLength?: number;
  maxLength?: number;
  minimum?: number;
  maximum?: number;
  exclusiveMinimum?: number;
  exclusiveMaximum?: number;
  multipleOf?: number;
  pattern?: string;
  default?: unknown;
  description?: string;
}
export interface Operation {
  path: string;
  method: 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE';
}
export type FormOperation = Operation & { fields: string[]; inputs: Field[]; has_body: boolean };
export type Action = FormOperation & {
  name: string;
  label: string;
  confirm: string;
  idempotency_field?: string | null;
  access: Access | null;
  tone: 'default' | 'danger';
  typed_confirmation: string | null;
  placement: 'toolbar' | 'widget';
};
export interface AdminResource {
  name: string;
  label: string;
  module: string;
  access: Access;
  group: string | null;
  icon: string | null;
  list: Operation;
  pagination: {
    mode: 'all' | 'offset' | 'cursor';
    limit: string;
    offset: string;
    cursor: string;
    next_header: string;
    max_page_size: number;
  };
  columns: Field[];
  detail: Operation | null;
  show: Field[];
  create: FormOperation | null;
  edit: FormOperation | null;
  delete: Operation | null;
  items_field: string | null;
  scope: { parameter: string | null; label: string } | null;
  filters: Field[];
  sort_parameter: string | null;
  sort_fields: string[];
  show_widgets: string[];
  edit_widgets: string[];
  actions: Action[];
  audit?: { resource_type: string; id_field: string } | null;
}
export interface AdminManifest {
  version: 1;
  modules: string[];
  resources: AdminResource[];
}
export const allowed = (access: Access, roles: readonly string[]): boolean =>
  access.authenticated || access.roles.some((role) => roles.includes(role));
