import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import BACKEND_URL from "./src/Constants"


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
