import type { Meta, StoryObj } from '@storybook/react-vite';

const meta: Meta = { title: 'Foundations/Tokens' };
export default meta;

const COLORS = [
  ['bg', 'bg-bg'],
  ['surface', 'bg-surface'],
  ['surface-muted', 'bg-surface-muted'],
  ['surface-hover', 'bg-surface-hover'],
  ['border', 'bg-border'],
  ['border-strong', 'bg-border-strong'],
  ['fg', 'bg-fg'],
  ['fg-muted', 'bg-fg-muted'],
  ['fg-subtle', 'bg-fg-subtle'],
  ['accent', 'bg-accent'],
  ['accent-muted', 'bg-accent-muted'],
  ['success', 'bg-success'],
  ['success-muted', 'bg-success-muted'],
  ['warning', 'bg-warning'],
  ['warning-muted', 'bg-warning-muted'],
  ['danger', 'bg-danger'],
  ['danger-muted', 'bg-danger-muted'],
  ['danger-solid', 'bg-danger-solid'],
] as const;

const TYPE = ['text-xs', 'text-sm', 'text-base', 'text-lg', 'text-xl', 'text-2xl'] as const;
const RADII = ['rounded-sm', 'rounded-md', 'rounded-lg', 'rounded-xl', 'rounded-full'] as const;
const SHADOWS = ['shadow-sm', 'shadow-md', 'shadow-lg'] as const;

/** Every value comes from src/styles/tokens.css; toggle the theme in the toolbar. */
export const Tokens: StoryObj = {
  render: () => (
    <div className="flex flex-col gap-8">
      <section className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
        {COLORS.map(([name, className]) => (
          <figure key={name} className="m-0 flex flex-col gap-1.5">
            <span className={`h-12 rounded-md border border-border ${className}`} />
            <figcaption className="font-mono text-xs">{name}</figcaption>
          </figure>
        ))}
      </section>
      <section className="flex flex-col gap-2">
        {TYPE.map((size) => (
          <p key={size} className={size}>
            {size} — The quick brown fox audits the outbox
          </p>
        ))}
        <p className="font-mono">font-mono — 5f2c…91ab</p>
      </section>
      <section className="flex flex-wrap gap-4">
        {RADII.map((radius) => (
          <span
            key={radius}
            className={`grid size-16 place-items-center bg-surface-muted text-xs ${radius}`}
          >
            {radius.replace('rounded-', '')}
          </span>
        ))}
        {SHADOWS.map((shadow) => (
          <span
            key={shadow}
            className={`grid size-16 place-items-center rounded-md bg-surface text-xs ${shadow}`}
          >
            {shadow.replace('shadow-', '')}
          </span>
        ))}
      </section>
    </div>
  ),
};
