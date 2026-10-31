import { fileURLToPath, URL } from 'node:url'
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
  // 产物由 Starlette 的 /static 静态目录提供
  base: '/static/dist/',
  resolve: {
    alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) },
  },
  build: {
    // 直接产出到后端真正读取的目录，避免「构建完忘记拷贝」导致页面还是旧前端
    outDir: '../static/dist',
    emptyOutDir: true,
    // 固定文件名，模板里直接写 /static/dist/app.js，省掉 manifest 解析
    rollupOptions: {
      output: {
        entryFileNames: 'app.js',
        chunkFileNames: 'chunk-[name].js',
        assetFileNames: 'app.[ext]',
      },
    },
  },
  server: {
    port: 5173,
    proxy: { '/api': 'http://127.0.0.1:8003' },
  },
})
