import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// The core serves the built app from src/fvs/webapp (it ships inside the Python package,
// so running FVS never needs Node). `npm run dev` proxies the API to a running core;
// start it with `fvs serve --dev-origin http://localhost:5173`.
export default defineConfig({
  plugins: [react()],
  build: { outDir: "../src/fvs/webapp", emptyOutDir: true },
  server: {
    proxy: {
      "/api": { target: "http://127.0.0.1:8787", changeOrigin: true },
      "/auth": { target: "http://127.0.0.1:8787", changeOrigin: true },
    },
  },
});
