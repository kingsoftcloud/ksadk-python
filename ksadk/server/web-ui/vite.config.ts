import path from "path"
import react from "@vitejs/plugin-react"
import { defineConfig } from "vite"

export default defineConfig({
  base: './',
  plugins: [react()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  server: {
    proxy: {
      // proxy /agentengine API to local fast API server
      '/agentengine': {
        target: 'http://localhost:8080',
        changeOrigin: true,
      },
      '/run_sse': {
        target: 'http://localhost:8080',
        changeOrigin: true,
      }
    }
  }
})
