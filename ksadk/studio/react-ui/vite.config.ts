import path from "path"
import react from "@vitejs/plugin-react"
import { defineConfig } from "vite"

export default defineConfig(({ command }) => ({
  base: command === "build" ? "/static/" : "/",
  // Generated chunks contain meaningful whitespace inside template literals
  // (including Lit's attribute parser). Leave JS rewriting to the bundler.
  plugins: [react(), {
    name: "preserve-generated-js-literals",
    // The removed generateBundle rewrite ran after content hashing. Fixed
    // bytes would otherwise reuse an old, immutable-cached asset filename.
    augmentChunkHash: () => "preserve-js-literals-v1",
  }],
  resolve: {
    alias: { "@": path.resolve(import.meta.dirname, "./src") },
  },
  server: {
    port: 5174,
    fs: { allow: [".."] },
    proxy: {
      "/api": "http://127.0.0.1:8080",
      "/v1": "http://127.0.0.1:8080",
      "/agentengine": "http://127.0.0.1:8080",
    },
  },
  build: {
    outDir: "../static",
    emptyOutDir: true,
    chunkSizeWarningLimit: 3000,
    rollupOptions: {
      output: {
        manualChunks(id) {
          if (id.includes("node_modules/react-dom") || id.includes("node_modules/react/")) return "react-vendor";
        },
      },
    },
  },
}))
