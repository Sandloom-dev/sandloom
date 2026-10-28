import { forwardRef, type InputHTMLAttributes, type TextareaHTMLAttributes } from 'react';
import { cn } from '@/lib/cn';

const field =
  'w-full rounded-md border border-border bg-surface px-2.5 text-sm text-fg shadow-sm transition-colors motion-fast placeholder:text-fg-subtle hover:border-border-strong disabled:cursor-not-allowed disabled:opacity-50 aria-invalid:border-danger';

export const Input = forwardRef<HTMLInputElement, InputHTMLAttributes<HTMLInputElement>>(
  function Input({ className, type = 'text', ...props }, ref) {
    return <input ref={ref} type={type} className={cn(field, 'h-control', className)} {...props} />;
  },
);

export const Textarea = forwardRef<
  HTMLTextAreaElement,
  TextareaHTMLAttributes<HTMLTextAreaElement>
>(function Textarea({ className, rows = 3, ...props }, ref) {
  return <textarea ref={ref} rows={rows} className={cn(field, 'py-2', className)} {...props} />;
});

/** Native select keeps keyboard and screen-reader behavior for free. */
export const NativeSelect = forwardRef<
  HTMLSelectElement,
  InputHTMLAttributes<HTMLSelectElement> & { children: React.ReactNode }
>(function NativeSelect({ className, children, ...props }, ref) {
  return (
    <select ref={ref} className={cn(field, 'h-control pr-8', className)} {...props}>
      {children}
    </select>
  );
});
