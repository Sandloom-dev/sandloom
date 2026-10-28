import type { ComponentProps } from 'react';
import { Popover as PopoverPrimitive } from 'radix-ui';
import { cn } from '@/lib/cn';

export const Popover = PopoverPrimitive.Root;
export const PopoverTrigger = PopoverPrimitive.Trigger;
export const PopoverClose = PopoverPrimitive.Close;

export function PopoverContent({
  className,
  sideOffset = 6,
  align = 'start',
  ...props
}: ComponentProps<typeof PopoverPrimitive.Content>) {
  return (
    <PopoverPrimitive.Portal>
      <PopoverPrimitive.Content
        sideOffset={sideOffset}
        align={align}
        className={cn(
          'z-dropdown w-72 animate-pop-in rounded-md border border-border bg-surface p-3 shadow-md',
          className,
        )}
        {...props}
      />
    </PopoverPrimitive.Portal>
  );
}
