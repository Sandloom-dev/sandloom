import { billingWidgets } from './billing/widgets';
import { identityWidgets } from './identity/widgets';
import { tenancyWidgets } from './tenancy/widgets';
import type { WidgetDefinition } from './widget-types';

/** Compiled UI components only. A manifest names them; it never supplies code. */
const registry: Readonly<Record<string, WidgetDefinition>> = {
  ...identityWidgets,
  ...tenancyWidgets,
  ...billingWidgets,
};

export const widgetNames: readonly string[] = Object.keys(registry);

export function widget(name: string): WidgetDefinition {
  if (!Object.hasOwn(registry, name)) throw new Error(`Unknown admin component: ${name}`);
  const definition = registry[name];
  if (!definition) throw new Error(`Unknown admin component: ${name}`);
  return definition;
}
