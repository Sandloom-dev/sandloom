import { clsx, type ClassValue } from 'clsx';
import { extendTailwindMerge } from 'tailwind-merge';

// Teach tailwind-merge the custom token scales so conflicting classes resolve.
const merge = extendTailwindMerge({
  extend: {
    classGroups: {
      'font-size': [{ text: ['xs', 'sm', 'base', 'lg', 'xl', '2xl'] }],
      z: ['z-sticky', 'z-dropdown', 'z-overlay', 'z-dialog', 'z-toast'],
    },
  },
});

/** Compose class names; later token classes win over earlier ones. */
export function cn(...inputs: ClassValue[]): string {
  return merge(clsx(inputs));
}
