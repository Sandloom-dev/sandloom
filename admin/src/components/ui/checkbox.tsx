import type { ComponentProps } from 'react';
import { Checkbox as CheckboxPrimitive } from 'radix-ui';
import { Check, Minus } from 'lucide-react';
import { cn } from '@/lib/cn';

export function Checkbox({ className, ...props }: ComponentProps<typeof CheckboxPrimitive.Root>) {
  return (
    <CheckboxPrimitive.Root
      className={cn(
        'peer grid size-4 shrink-0 place-items-center rounded-sm border border-border-strong bg-surface transition-colors motion-fast data-[state=checked]:border-fg data-[state=checked]:bg-fg data-[state=checked]:text-bg data-[state=indeterminate]:border-fg data-[state=indeterminate]:bg-fg data-[state=indeterminate]:text-bg',
        className,
      )}
      {...props}
    >
      <CheckboxPrimitive.Indicator className="group">
        {props.checked === 'indeterminate' ? (
          <Minus aria-hidden className="size-3" />
        ) : (
          <Check aria-hidden className="size-3" />
        )}
      </CheckboxPrimitive.Indicator>
    </CheckboxPrimitive.Root>
  );
}
