import { useSyncExternalStore } from 'react';
import { Toast as ToastPrimitive } from 'radix-ui';
import { CircleAlert, CircleCheck, Info, X } from 'lucide-react';
import { cn } from '@/lib/cn';

export type ToastTone = 'success' | 'error' | 'info';
export interface ToastMessage {
  id: number;
  tone: ToastTone;
  title: string;
  description?: string;
}

let next = 1;
let queue: ToastMessage[] = [];
const listeners = new Set<() => void>();
const emit = () => {
  for (const listener of listeners) listener();
};

/** Imperative entry point used by command hooks; rendering lives in <Toaster>. */
export function toast(message: Omit<ToastMessage, 'id'>): void {
  queue = [...queue.slice(-4), { ...message, id: next++ }];
  emit();
}

function dismiss(id: number) {
  queue = queue.filter((item) => item.id !== id);
  emit();
}

const ICONS = { success: CircleCheck, error: CircleAlert, info: Info } as const;
const TONES = { success: 'text-success', error: 'text-danger', info: 'text-accent' } as const;

export function Toaster() {
  const items = useSyncExternalStore(
    (listener) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    () => queue,
  );
  return (
    <ToastPrimitive.Provider swipeDirection="right" duration={6000}>
      {items.map((item) => {
        const Icon = ICONS[item.tone];
        return (
          <ToastPrimitive.Root
            key={item.id}
            type={item.tone === 'error' ? 'foreground' : 'background'}
            onOpenChange={(open) => {
              if (!open) dismiss(item.id);
            }}
            className="flex animate-pop-in items-start gap-3 rounded-lg border border-border bg-surface p-3.5 shadow-md"
          >
            <Icon aria-hidden className={cn('mt-0.5 size-4 shrink-0', TONES[item.tone])} />
            <div className="flex min-w-0 flex-1 flex-col gap-0.5">
              <ToastPrimitive.Title className="font-semibold">{item.title}</ToastPrimitive.Title>
              {item.description && (
                <ToastPrimitive.Description className="text-fg-muted">
                  {item.description}
                </ToastPrimitive.Description>
              )}
            </div>
            <ToastPrimitive.Close
              aria-label="Dismiss notification"
              className="grid size-control-sm place-items-center rounded-md text-fg-subtle hover:bg-surface-hover"
            >
              <X aria-hidden className="size-4" />
            </ToastPrimitive.Close>
          </ToastPrimitive.Root>
        );
      })}
      <ToastPrimitive.Viewport className="fixed right-4 bottom-4 z-toast flex w-full max-w-field flex-col gap-2 outline-none" />
    </ToastPrimitive.Provider>
  );
}
