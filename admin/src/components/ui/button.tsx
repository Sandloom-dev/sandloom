import { forwardRef, type ButtonHTMLAttributes } from 'react';
import { Slot } from 'radix-ui';
import { cva, type VariantProps } from 'class-variance-authority';
import { LoaderCircle } from 'lucide-react';
import { cn } from '@/lib/cn';

export const buttonVariants = cva(
  'inline-flex shrink-0 items-center justify-center gap-2 rounded-md font-medium whitespace-nowrap transition-colors motion-fast select-none disabled:pointer-events-none disabled:opacity-50 [&_svg]:size-4 [&_svg]:shrink-0',
  {
    variants: {
      variant: {
        primary: 'bg-primary text-primary-fg hover:opacity-90',
        accent: 'bg-accent text-accent-fg hover:opacity-90',
        secondary: 'border border-border bg-surface text-fg shadow-sm hover:bg-surface-hover',
        ghost: 'text-fg-muted hover:bg-surface-hover hover:text-fg',
        danger: 'bg-danger-solid text-danger-solid-fg hover:opacity-90',
        'danger-outline': 'border border-danger bg-surface text-danger hover:bg-danger-muted',
        link: 'h-auto px-0 text-accent underline-offset-4 hover:underline',
      },
      size: {
        sm: 'h-control-sm px-2.5 text-xs',
        md: 'h-control px-3 text-sm',
        lg: 'h-control-lg px-4 text-sm',
        icon: 'size-control',
        'icon-sm': 'size-control-sm',
      },
    },
    defaultVariants: { variant: 'secondary', size: 'md' },
  },
);

export interface ButtonProps
  extends ButtonHTMLAttributes<HTMLButtonElement>, VariantProps<typeof buttonVariants> {
  /** Render the child element (e.g. a router Link) with button styling. */
  asChild?: boolean;
  /** Shows a spinner and disables the button while an operation is in flight. */
  loading?: boolean;
}

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(function Button(
  { className, variant, size, asChild = false, loading = false, disabled, children, ...props },
  ref,
) {
  if (asChild) {
    return (
      <Slot.Root ref={ref} className={cn(buttonVariants({ variant, size }), className)} {...props}>
        {children}
      </Slot.Root>
    );
  }
  return (
    <button
      ref={ref}
      type="button"
      className={cn(buttonVariants({ variant, size }), className)}
      disabled={Boolean(disabled) || loading}
      aria-busy={loading || undefined}
      {...props}
    >
      {loading && <LoaderCircle aria-hidden className="animate-spin" />}
      {children}
    </button>
  );
});
