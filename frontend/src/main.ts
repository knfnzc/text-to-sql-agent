/**
 * 前端入口。
 *
 * 只做三件事：挂载根组件、引入全局样式、设置一个全局错误钩子。
 */

import { createApp } from 'vue'

import App from './App.vue'
import './style.css'

const app = createApp(App)

// 兜底的错误处理：组件里漏掉的异常不至于让整页白屏，
// 至少能在控制台看到是哪一步炸的。
// 生产环境应该把这里换成上报到监控平台
app.config.errorHandler = (error, _instance, info) => {
  console.error('[未捕获的组件错误]', info, error)
}

app.mount('#app')
