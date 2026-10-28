import type { Meta, StoryObj } from '@storybook/react-vite';
import { Ban, Plus } from 'lucide-react';
import { Badge } from './badge';
import { Button } from './button';
import { Card, CardContent, CardHeader, CardTitle } from './card';
import { Checkbox } from './checkbox';
import { Input, NativeSelect, Textarea } from './input';
import { Kbd } from './kbd';
import { Label } from './label';
import { Skeleton } from './skeleton';
import { Tabs, TabsContent, TabsList, TabsTrigger } from './tabs';
import { Tooltip } from './tooltip';

const meta: Meta = { title: 'UI/Primitives' };
export default meta;
type Story = StoryObj;

export const Buttons: Story = {
  render: () => (
    <div className="flex flex-wrap items-center gap-2">
      <Button variant="primary">Primary</Button>
      <Button variant="accent">Accent</Button>
      <Button>Secondary</Button>
      <Button variant="ghost">Ghost</Button>
      <Button variant="danger">
        <Ban aria-hidden />
        Danger
      </Button>
      <Button variant="danger-outline">Danger outline</Button>
      <Button variant="link">Link</Button>
      <Button loading>Saving</Button>
      <Button disabled>Disabled</Button>
      <Button size="sm">Small</Button>
      <Button size="icon" aria-label="Add">
        <Plus aria-hidden />
      </Button>
    </div>
  ),
};

export const Badges: Story = {
  render: () => (
    <div className="flex flex-wrap gap-2">
      <Badge>neutral</Badge>
      <Badge tone="outline">outline</Badge>
      <Badge tone="accent">accent</Badge>
      <Badge tone="success" dot>
        healthy
      </Badge>
      <Badge tone="warning" dot>
        degraded
      </Badge>
      <Badge tone="danger" dot>
        blocked
      </Badge>
    </div>
  ),
};

export const FormControls: Story = {
  render: () => (
    <div className="flex max-w-field flex-col gap-3">
      <div className="flex flex-col gap-1.5">
        <Label htmlFor="sb-input">Email</Label>
        <Input id="sb-input" placeholder="name@example.test" />
      </div>
      <div className="flex flex-col gap-1.5">
        <Label htmlFor="sb-invalid">Invalid</Label>
        <Input id="sb-invalid" aria-invalid defaultValue="nope" />
      </div>
      <div className="flex flex-col gap-1.5">
        <Label htmlFor="sb-select">Role</Label>
        <NativeSelect id="sb-select" defaultValue="member">
          <option value="member">member</option>
          <option value="admin">admin</option>
        </NativeSelect>
      </div>
      <div className="flex flex-col gap-1.5">
        <Label htmlFor="sb-text">Reason</Label>
        <Textarea id="sb-text" />
      </div>
      <div className="flex items-center gap-2">
        <Checkbox id="sb-check" defaultChecked />
        <Label htmlFor="sb-check">Remember view</Label>
      </div>
    </div>
  ),
};

export const CardsTabsAndHints: Story = {
  render: () => (
    <Card className="max-w-prose">
      <CardHeader>
        <CardTitle>Card title</CardTitle>
        <Tooltip content="Supplementary hint">
          <Button size="sm" variant="ghost">
            Hover me
          </Button>
        </Tooltip>
      </CardHeader>
      <CardContent>
        <Tabs defaultValue="one">
          <TabsList>
            <TabsTrigger value="one">Overview</TabsTrigger>
            <TabsTrigger value="two">Activity</TabsTrigger>
          </TabsList>
          <TabsContent value="one">
            Press <Kbd>⌘</Kbd> <Kbd>K</Kbd> to search.
          </TabsContent>
          <TabsContent value="two">
            <Skeleton className="h-4 w-1/2" />
          </TabsContent>
        </Tabs>
      </CardContent>
    </Card>
  ),
};
