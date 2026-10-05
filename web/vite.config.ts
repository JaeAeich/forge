import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// `forge up` serves the built app from web/dist; `npm run dev` proxies the API to it instead.
export default defineConfig({
  plugins: [react()],
  server: { proxy: { "/api": "http://127.0.0.1:8000" } },
});
