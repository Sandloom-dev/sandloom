import type { ComponentProps, ReactNode } from 'react';
import { Dialog as DialogPrimitive } from 'radix-ui';
import { X } from 'lucide-react';
import { cn } from '@/lib/cn';

export const Dialog = DialogPrimitive.Root;
export const DialogTrigger = DialogPrimitive.Trigger;
export const DialogClose = DialogPrimitive.Close;

export function DialogContent({
  className,
  children,
  hideClose = false,
  ...props
}: ComponentProps<typeof DialogPrimitive.Content> & { hideClose?: boolean }) {
  return (
    <DialogPrimitive.Portal>
      <DialogPrimitive.Overlay className="fixed inset-0 z-overlay animate-fade-in bg-overlay" />
      <DialogPrimitive.Content
        className={cn(
          'fixed inset-x-4 top-24 z-dialog mx-auto flex max-h-dialog max-w-dialog animate-pop-in flex-col overflow-auto rounded-xl border border-border bg-surface shadow-lg',
          className,
        )}
        {...props}
      >
        {children}
        {!hideClose && (
          <DialogPrimitive.Close
            className="absolute top-3 right-3 grid size-control-sm place-items-center rounded-md text-fg-subtle hover:bg-surface-hover hover:text-fg"
            aria-label="Close"
          >
            <X aria-hidden className="size-4" />
          </DialogPrimitive.Close>
        )}
      </DialogPrimitive.Content>
    </DialogPrimitive.Portal>
  );
}

export function DialogHeader({
  icon,
  title,
  description,
}: {
  icon?: ReactNode;
  title: ReactNode;
  description?: ReactNode;
}) {
  return (
    <div className="flex gap-3.5 px-5 pt-5">
      {icon}
      <div className="flex min-w-0 flex-col gap-1 pr-6">
        <DialogPrimitive.Title className="text-lg font-semibold">{title}</DialogPrimitive.Title>
        {description ? (
          <DialogPrimitive.Description className="text-sm text-fg-muted">
            {description}
          </DialogPrimitive.Description>
        ) : (
          <DialogPrimitive.Description className="sr-only">{title}</DialogPrimitive.Description>
        )}
      </div>
    </div>
  );
}

export function DialogBody({ className, ...props }: ComponentProps<'div'>) {
  return <div className={cn('flex flex-col gap-4 px-5 py-4', className)} {...props} />;
}

export function DialogFooter({ className, ...props }: ComponentProps<'div'>) {
  return (
    <div
      className={cn(
        'flex flex-wrap justify-end gap-2 rounded-b-xl border-t border-border bg-surface-muted px-5 py-3.5',
        className,
      )}
      {...props}
    />
  );
}
