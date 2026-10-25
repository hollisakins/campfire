import { defineConfig } from 'vitest/config';
import { fileURLToPath } from 'url';

export default defineConfig({
  // Vite 8 follows tsconfig's `"jsx": "preserve"` (which Next needs) and would
  // leave JSX in the .tsx tests untransformed; compile it like React does.
  oxc: { jsx: { runtime: 'automatic' } },
  resolve: {
    // Mirror tsconfig's `@/*` → `./*` so modules under test can use the app's
    // import convention.
    alias: { '@': fileURLToPath(new URL('.', import.meta.url)) },
  },
});
