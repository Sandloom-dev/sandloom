import type { ReactNode } from 'react';
import { Tooltip as TooltipPrimitive } from 'radix-ui';

export const TooltipProvider = TooltipPrimitive.Provider;

/** Supplementary hint only — never the sole label of a control. */
export function Tooltip({ content, children }: { content: ReactNode; children: ReactNode }) {
  return (
    <TooltipPrimitive.Root>
      <TooltipPrimitive.Trigger asChild>{children}</TooltipPrimitive.Trigger>
      <TooltipPrimitive.Portal>
        <TooltipPrimitive.Content
          sideOffset={6}
          className="z-dropdown animate-fade-in rounded-sm bg-fg px-2 py-1 text-xs text-bg shadow-md"
        >
          {content}
        </TooltipPrimitive.Content>
      </TooltipPrimitive.Portal>
    </TooltipPrimitive.Root>
  );
}
