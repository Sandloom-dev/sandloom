import type { ComponentProps } from 'react';
import { DropdownMenu as Menu } from 'radix-ui';
import { Check } from 'lucide-react';
import { cn } from '@/lib/cn';

export const DropdownMenu = Menu.Root;
export const DropdownMenuTrigger = Menu.Trigger;
export const DropdownMenuGroup = Menu.Group;

const item =
  'relative flex h-control cursor-default items-center gap-2 rounded-sm px-2 text-sm outline-none select-none data-[disabled]:opacity-50 data-[highlighted]:bg-surface-hover [&_svg]:size-4 [&_svg]:text-fg-subtle';

export function DropdownMenuContent({
  className,
  sideOffset = 6,
  ...props
}: ComponentProps<typeof Menu.Content>) {
  return (
    <Menu.Portal>
      <Menu.Content
        sideOffset={sideOffset}
        className={cn(
          'z-dropdown min-w-48 animate-pop-in rounded-md border border-border bg-surface p-1 shadow-md',
          className,
        )}
        {...props}
      />
    </Menu.Portal>
  );
}

export function DropdownMenuItem({
  className,
  tone = 'default',
  ...props
}: ComponentProps<typeof Menu.Item> & { tone?: 'default' | 'danger' }) {
  return (
    <Menu.Item
      className={cn(item, tone === 'danger' && 'text-danger [&_svg]:text-danger', className)}
      {...props}
    />
  );
}

export function DropdownMenuCheckboxItem({
  className,
  children,
  ...props
}: ComponentProps<typeof Menu.CheckboxItem>) {
  return (
    <Menu.CheckboxItem className={cn(item, 'pl-7', className)} {...props}>
      <Menu.ItemIndicator className="absolute left-2">
        <Check aria-hidden />
      </Menu.ItemIndicator>
      {children}
    </Menu.CheckboxItem>
  );
}

export function DropdownMenuLabel({ className, ...props }: ComponentProps<typeof Menu.Label>) {
  return (
    <Menu.Label
      className={cn('px-2 py-1.5 text-xs font-semibold text-fg-subtle', className)}
      {...props}
    />
  );
}

export function DropdownMenuSeparator({
  className,
  ...props
}: ComponentProps<typeof Menu.Separator>) {
  return <Menu.Separator className={cn('-mx-1 my-1 h-px bg-border', className)} {...props} />;
}
