import path from 'node:path';
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  build: {
    outDir: 'output/playwright/history-search-dist',
    rollupOptions: { input: path.resolve(import.meta.dirname, 'e2e/history-search.html') },
  },
});
