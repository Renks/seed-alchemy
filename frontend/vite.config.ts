import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";


export const BACKEND_URL = ""

// https://vitejs.dev/config/
export default defineConfig({
  plugins: [react()],
  base: "./",
  server: {
    proxy: {
      "/api": {
        target: BACKEND_URL,
        changeOrigin: true,
      },
      "/images": {
        target: BACKEND_URL,
        changeOrigin: true,
      },
      "/thumbnails": {
        target: BACKEND_URL,
        changeOrigin: true,
      },
    },
  },
});
