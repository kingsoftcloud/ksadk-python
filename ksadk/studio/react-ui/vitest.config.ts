import path from "node:path";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// ksadk-web's NativeTerminalPanel imports @xterm/xterm/css/xterm.css, which
// vitest's jsdom environment cannot load. Stub it to an empty module so App
// integration tests that transitively import the terminal panel do not fail
// on the CSS extension.

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      "@": path.resolve(import.meta.dirname, "./src"),
      // Node ESM resolver in vitest does not hand .css to the plugin chain;
      // alias the xterm stylesheet to an empty module file so imports of
      // @xterm/xterm/css/xterm.css resolve to nothing at runtime.
      "@xterm/xterm/css/xterm.css": path.resolve(import.meta.dirname, "./src/test/empty.ts"),
    },
  },
  test: {
    environment: "jsdom",
    setupFiles: ["./src/test/setup.ts"],
    css: true,
    include: ["src/**/*.test.{ts,tsx}"],
    maxWorkers: 1,
    server: {
      deps: {
        inline: ["@xterm/xterm", "@kingsoftcloud/ksadk-web"],
      },
    },
  },
});
