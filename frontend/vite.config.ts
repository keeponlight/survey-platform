import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// 前端只绑定 localhost（无网关时的首版约定，见 PRD-v1 §9 Q3）。
// 开发服务器把 /api 代理到本机 api（compose 中为服务名 api:8000）。
export default defineConfig({
  plugins: [react()],
  server: {
    host: "127.0.0.1",
    port: 5173,
    proxy: {
      "/api": {
        target: process.env.VITE_API_TARGET ?? "http://127.0.0.1:8000",
        changeOrigin: true,
      },
    },
  },
  preview: {
    host: "127.0.0.1",
    port: 4173,
  },
  build: {
    outDir: "dist",
    sourcemap: false,
  },
});
