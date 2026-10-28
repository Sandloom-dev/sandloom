import type { Meta, StoryObj } from '@storybook/react-vite';
import { Columns3, LogOut } from 'lucide-react';
import { Button } from './button';
import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTrigger,
} from './dialog';
import {
  DropdownMenu,
  DropdownMenuCheckboxItem,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from './dropdown-menu';
import { Popover, PopoverContent, PopoverTrigger } from './popover';
import { Toaster, toast } from './toast';

const meta: Meta = { title: 'UI/Overlays' };
export default meta;
type Story = StoryObj;

export const DialogExample: Story = {
  render: () => (
    <Dialog>
      <DialogTrigger asChild>
        <Button>Open dialog</Button>
      </DialogTrigger>
      <DialogContent>
        <DialogHeader title="Dialog title" description="Supporting description." />
        <DialogBody>Body content.</DialogBody>
        <DialogFooter>
          <Button variant="primary">Done</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  ),
};

export const Menu: Story = {
  render: () => (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button>
          <Columns3 aria-hidden />
          Menu
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent>
        <DropdownMenuLabel>Columns</DropdownMenuLabel>
        <DropdownMenuCheckboxItem checked>Name</DropdownMenuCheckboxItem>
        <DropdownMenuCheckboxItem>Created</DropdownMenuCheckboxItem>
        <DropdownMenuSeparator />
        <DropdownMenuItem tone="danger">
          <LogOut aria-hidden />
          Sign out
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  ),
};

export const PopoverExample: Story = {
  render: () => (
    <Popover>
      <PopoverTrigger asChild>
        <Button>Open popover</Button>
      </PopoverTrigger>
      <PopoverContent>Popover content.</PopoverContent>
    </Popover>
  ),
};

export const Toasts: Story = {
  render: () => (
    <>
      <div className="flex gap-2">
        <Button
          onClick={() => {
            toast({ tone: 'success', title: 'Sessions revoked' });
          }}
        >
          Success
        </Button>
        <Button
          onClick={() => {
            toast({ tone: 'error', title: 'Could not save', description: 'Try again later.' });
          }}
        >
          Error
        </Button>
        <Button
          onClick={() => {
            toast({ tone: 'info', title: 'Heads up' });
          }}
        >
          Info
        </Button>
      </div>
      <Toaster />
    </>
  ),
};
