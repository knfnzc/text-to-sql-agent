/// <reference types="vite/client" />

// 让 TypeScript 认识 .vue 文件。没有这段声明，所有 `import Xxx from './Xxx.vue'`
// 都会报 "找不到模块"，类型检查直接失效。
declare module '*.vue' {
  import type { DefineComponent } from 'vue'

  const component: DefineComponent<Record<string, unknown>, Record<string, unknown>, unknown>
  export default component
}

// 环境变量的类型声明。
// 声明出来而不是用 any，是为了写错变量名时能立刻发现——
// 环境变量拼错的话运行时会静默变成 undefined，非常难排查。
interface ImportMetaEnv {
  /** 后端接口前缀。默认 /api，由 Vite 代理转发到 FastAPI */
  readonly VITE_API_BASE?: string
  /** 置为 'true' 时强制进入离线演示模式，用内置样例数据渲染界面 */
  readonly VITE_DEMO?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
