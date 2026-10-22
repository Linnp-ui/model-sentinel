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
    // vendor 只留 react/react-dom（稳定小包）：antd 若整包进 manualChunks，首屏会下载
    // 「全站所有页面用到的 antd 并集」（Table/Form/Modal 等别的页组件也被拖进首屏，
    // 实测 1088KB）。antd 交给 Rollup 按页面自然拆分：首屏只带 shell 用的组件，
    // 页面各自/共享 chunk 随页懒载；library 代码内容不变 hash 不变，缓存收益不丢
    rollupOptions: {
      output: {
        manualChunks: {
          vendor: ['react', 'react-dom'],
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
