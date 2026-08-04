import { defineConfig } from "vite";
import { resolve } from "node:path";

export default defineConfig({
  build: {
    emptyOutDir: true,
    lib: {
      entry: resolve(import.meta.dirname, "src/shared-message.ts"),
      formats: ["es"],
      fileName: () => "shared-web.js"
    },
    outDir: resolve(import.meta.dirname, "../static/vendor"),
    rollupOptions: {
      output: {
        assetFileNames: "shared-web.[ext]"
      }
    }
  }
});
