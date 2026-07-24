import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// In dev, the React app runs on :5173 and the FastAPI backend on :8000.
// Proxy same-origin /api and /public calls to the backend so the browser
// treats them as same-origin (cookies, SSE, no CORS). Set TALLY_API_TARGET to
// point at a deployed backend instead of localhost.
const target = process.env.TALLY_API_TARGET || "http://127.0.0.1:8000";

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/api": { target, changeOrigin: true },
      "/public": { target, changeOrigin: true },
    },
  },
});
