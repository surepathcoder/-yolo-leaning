/// <reference types="vitest/config" />
import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import path from "node:path";

export default defineConfig({
  plugins: [react()],
  resolve: {
    // import.meta.dirname rather than __dirname: the config is ESM, and
    // Vite's native config loader does not shim the CommonJS globals.
    alias: { "@": path.resolve(import.meta.dirname, "./src") },
  },
  server: {
    port: 5173,
    // Proxy in development so the browser talks to one origin. This keeps the
    // dev environment shaped like production (nginx does the same) instead of
    // relying on CORS, which hides same-origin cookie and header problems
    // until deployment.
    proxy: {
      "/api": { target: "http://127.0.0.1:8010", changeOrigin: true },
      "/ws": { target: "ws://127.0.0.1:8010", ws: true },
      "/health": { target: "http://127.0.0.1:8010", changeOrigin: true },
    },
  },
  build: {
    outDir: "dist",
    sourcemap: true,
    rollupOptions: {
      output: {
        // Split the heavy charting library out of the main bundle: the
        // dashboard's first paint should not wait for code only the analytics
        // page needs. Written as a function because Rollup's typed overloads
        // no longer accept the object form.
        manualChunks(id: string) {
          if (id.includes("node_modules/recharts") || id.includes("node_modules/d3")) {
            return "charts";
          }
          if (
            id.includes("node_modules/react") ||
            id.includes("node_modules/scheduler")
          ) {
            return "react";
          }
          return undefined;
        },
      },
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./tests/setup.ts"],
    include: ["tests/**/*.test.{ts,tsx}"],
  },
});
