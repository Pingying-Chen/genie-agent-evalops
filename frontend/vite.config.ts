// Dev server: proxies /api to the local backend and plays the Databricks Apps proxy,
// injecting the viewer identity headers from EVALOPS_DEV_TOKEN / EVALOPS_DEV_EMAIL.
import path from "node:path";
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

const backend = process.env.EVALOPS_BACKEND ?? "http://localhost:8799";

export default defineConfig({
  // Tailwind 4 through its Vite plugin, and the `@/` alias, as in the Genie Workbench.
  plugins: [react(), tailwindcss()],
  resolve: { alias: { "@": path.resolve(__dirname, "./src") } },
  server: {
    proxy: {
      "/api": {
        target: backend,
        headers: {
          "x-forwarded-access-token": process.env.EVALOPS_DEV_TOKEN ?? "",
          "x-forwarded-email": process.env.EVALOPS_DEV_EMAIL ?? "",
        },
      },
    },
  },
  build: { outDir: "dist", emptyOutDir: true },
});
