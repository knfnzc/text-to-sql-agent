import { fileURLToPath, URL } from 'node:url'

import vue from '@vitejs/plugin-vue'
import { defineConfig } from 'vite'

// 前后端分离后的关键一环：开发期把 /api 代理到 FastAPI 服务。
//
// 为什么需要代理而不是让前端直接请求 http://127.0.0.1:8000：
//   1. 同源问题。前端跑在 5173，后端在 8000，属于不同源，
//      浏览器会先发预检请求，后端必须配好 CORS 才能放行。
//      走代理后请求从 5173 发出、也回到 5173，浏览器认为是同源，不需要 CORS。
//   2. 免去环境变量的分支。前端代码里一律写相对路径 /api，
//      开发、预览、反向代理部署三种场景的写法完全一致。
//
// 后端仍然配置了 CORS 白名单：代理只在开发期存在，
// 生产部署形态不确定（可能前端独立域名），后端不能假定请求一定同源。
// 开发代理与预览代理共用同一份配置。
//
// 为什么要给 preview 也配上：
//   `npm run preview` 是用来看"生产构建产物"跑起来什么效果的，
//   很容易被当成验收手段。如果只有 dev 配了代理，
//   preview 下所有 /api 请求都会 404，让人误以为构建出来的东西是坏的。
//   两边共用一份配置，就不会出现"开发能跑、预览不能跑"这种割裂
const apiProxy = {
  '/api': {
    target: 'http://127.0.0.1:8000',
    changeOrigin: true,
  },
}

export default defineConfig({
  // 用相对路径作为资源前缀，构建产物可以直接放在任意子目录下
  // （例如 GitHub Pages 的 https://<user>.github.io/<repo>/）
  base: './',
  plugins: [vue()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    port: 5173,
    // 监听 0.0.0.0 便于在容器或局域网内访问
    host: true,
    proxy: apiProxy,
  },
  preview: {
    port: 4173,
    host: true,
    proxy: apiProxy,
  },
  build: {
    outDir: 'dist',
    // 生产包不生成 sourcemap：体积更小，也不会把源码结构暴露出去
    sourcemap: false,
    // ECharts 单独成块，它的体积远大于业务代码，
    // 拆开后业务代码改动不会让用户重新下载整个图表库
    rollupOptions: {
      output: {
        manualChunks: {
          echarts: ['echarts'],
          vendor: ['vue', 'marked', 'dompurify'],
        },
      },
    },
    chunkSizeWarningLimit: 1200,
  },
})
