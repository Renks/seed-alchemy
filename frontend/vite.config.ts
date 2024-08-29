import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";


const BACKEND_URL = "https://13f7-34-125-33-191.ngrok-free.app/"

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
