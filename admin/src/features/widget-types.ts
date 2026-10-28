import type { ComponentType } from 'react';
import type { QueryKey } from '@tanstack/react-query';
import type { AdminResource } from '@/schema/types';
import type { ResourceRecord } from './resources/data';

export interface WidgetProps {
  resource: AdminResource;
  record: ResourceRecord;
  scope: string;
  /** Query keys to refresh after a command on this record. */
  invalidate: readonly QueryKey[];
}

/**
 * A compiled UI component a manifest may reference by name. Slots: `actions`
 * renders in the entity header toolbar, `banner` above the content, `body`
 * as a card in the main column. The manifest never supplies code or HTML.
 */
export interface WidgetDefinition {
  actions?: ComponentType<WidgetProps>;
  banner?: ComponentType<WidgetProps>;
  body?: ComponentType<WidgetProps>;
}
