import { defineConfig } from 'vitest/config';
import { fileURLToPath } from 'url';

export default defineConfig({
  resolve: {
    // Mirror tsconfig's `@/*` → `./*` so modules under test can use the app's
    // import convention.
    alias: { '@': fileURLToPath(new URL('.', import.meta.url)) },
  },
});
