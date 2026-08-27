import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// No dev proxy, deliberately. Proxying /api to the backend would make local
// development same-origin, and production is not: the SPA is served from
// Cloudflare Workers and the API from Render. A cross-site cookie, a CORS
// preflight and an allowlisted origin are all things worth discovering on
// localhost rather than in a deployment.
export default defineConfig({
  plugins: [react()],
  server: { port: 5173 },
  build: { outDir: "dist", sourcemap: true },
});
