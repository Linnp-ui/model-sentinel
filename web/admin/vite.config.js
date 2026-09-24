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
    // vendor 单独分包：react/antd 不随业务代码变 hash，跨版本浏览器缓存命中
    rollupOptions: {
      output: {
        manualChunks: {
          vendor: ['react', 'react-dom', 'antd', '@ant-design/icons'],
        },
      },
    },
  },
  server: {
    port: 5175,
    // 本地开发代理到本机网关，避免 CORS
    proxy: {
      '/admin/api': 'http://127.0.0.1:8080',
    },
  },
});
