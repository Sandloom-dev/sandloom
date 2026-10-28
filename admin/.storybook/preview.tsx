import type { Preview } from '@storybook/react-vite';
import { withThemeByDataAttribute } from '@storybook/addon-themes';
import { TooltipProvider } from '../src/components/ui/tooltip';
import '../src/styles/index.css';

const preview: Preview = {
  parameters: {
    layout: 'padded',
    a11y: { test: 'error' },
    controls: { expanded: true },
  },
  decorators: [
    (Story) => (
      <TooltipProvider>
        <Story />
      </TooltipProvider>
    ),
    withThemeByDataAttribute({
      themes: { light: 'light', dark: 'dark' },
      defaultTheme: 'light',
      attributeName: 'data-theme',
      parentSelector: 'html',
    }),
  ],
};
export default preview;
