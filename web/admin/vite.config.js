import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// 构建产物输出到网关 static/admin/，由 FastAPI 托管于 /admin/app
// base 必须与托管路径一致，否则 js/css 404
export default defineConfig({
  plugins: [react()],
  base: '/static/admin/',
  build: {
    outDir: '../../static/admin',
    emptyOutDir: true,
  },
  server: {
    port: 5175,
    // 本地开发代理到本机网关，避免 CORS
    proxy: {
      '/admin/api': 'http://127.0.0.1:8080',
    },
  },
});
