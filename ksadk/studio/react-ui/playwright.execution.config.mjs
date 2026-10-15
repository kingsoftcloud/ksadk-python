import { defineConfig } from "@playwright/test";
export default defineConfig({
  testDir: "./e2e", testMatch: "execution-presentation.spec.ts", workers: 1, timeout: 45000,
  outputDir: "output/playwright/execution-results", reporter: "list",
  use: { baseURL: "http://127.0.0.1:4195", viewport: { width: 1440, height: 1000 }, screenshot: "only-on-failure" },
  webServer: { command: "npx vite build --config vite.execution.config.ts && npx vite preview --outDir output/playwright/execution-dist --host 127.0.0.1 --port 4195 --strictPort",
    url: "http://127.0.0.1:4195/e2e/execution-presentation.html", reuseExistingServer: false },
});
