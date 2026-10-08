# 智能数据分析 Agent · 前端

用中文提问，自动完成 **意图解析 → 表结构检索 → SQL 生成 → 安全校验 → 执行查询 → 结论与图表**。
本目录是独立的前端工程，通过 REST + SSE 与 `../backend` 的 FastAPI 服务通信。

## 技术选型

| 关注点 | 选择 | 理由 |
| --- | --- | --- |
| 框架 | Vue 3（`<script setup>` + Composition API） | 上手成本低、单文件组件把模板与逻辑放在一起，读起来不跳文件 |
| 语言 | TypeScript（`strict`） | 接口契约有类型兜底，改后端字段时编译期就能发现问题 |
| 构建 | Vite 6 | 开发期冷启动在百毫秒级，热更新不丢组件状态 |
| 图表 | ECharts（按需引入） | 后端只下发"画什么、用哪两列"，图形由前端画，传输体积小且样式可改 |
| Markdown | marked + DOMPurify | 结论文本由模型生成，必须先渲染再净化，否则等于开了一条 XSS 通道 |

## 快速开始

前置条件：Node.js ≥ 18（开发时使用 22.16）。

```bash
# 1. 安装依赖
npm install

# 2. 启动后端（另开一个终端，在前端工程的上一级目录）
cd ../backend
uvicorn app.main:app --reload --port 8000

# 3. 启动前端开发服务器
npm run dev
```

打开 http://localhost:5173 即可。开发服务器会把 `/api` 代理到 `http://127.0.0.1:8000`，
所以前端代码里统一写相对路径 `/api`，不需要为不同环境改地址。

### 没有后端也能看界面

后端未启动时，页面会给出提示，并提供一个「用内置演示数据预览界面」的入口。
点它会用 `src/api/demo.ts` 里的样例响应把界面完整渲染一遍，
步骤条与执行轨迹也会按真实的事件顺序走一遍。
这样把构建产物发到静态托管（GitHub Pages 等）上也能演示。

也可以在 `.env.local` 里写 `VITE_DEMO=true`，让页面直接进入演示模式。

## 可用脚本

| 命令 | 作用 |
| --- | --- |
| `npm run dev` | 启动开发服务器（含 `/api` 代理、热更新） |
| `npm run build` | 类型检查 + 生产构建，产物在 `dist/` |
| `npm run build:only` | 只构建，跳过类型检查（排查"是不是类型报错挡住了构建"时用） |
| `npm run preview` | 本地预览生产构建产物（同样带 `/api` 代理） |
| `npm run type-check` | 只做类型检查 |

## 目录结构

```
src/
├── api/
│   ├── client.ts        # 所有网络调用：REST 封装 + SSE 订阅 + 错误归一化
│   └── demo.ts          # 离线演示数据（无后端时用）
├── components/
│   ├── AppSidebar.vue      # 服务状态、安全限制、索引管理、示例问题、数据字典
│   ├── LLMConfigPanel.vue  # 更换模型 / 密钥：只提交改动过的字段，密钥留空即不修改
│   ├── StageBar.vue        # 六步执行链路步骤条，由 SSE 的 stage 事件驱动
│   ├── TraceTimeline.vue   # 执行轨迹时间线，逐条追加并自动跟随滚动
│   ├── IntentPanel.vue     # 模型解析出的查询意图
│   ├── ValidationPanel.vue # SQL 安全校验：拦截原因、命中的表、已执行的检查项
│   ├── SqlPanel.vue        # SQL 展示与高亮、多次尝试历史、复制
│   ├── MetricCards.vue     # 结果行数 / 耗时 / 尝试次数 / token / 命中表数
│   ├── ChartView.vue       # 按后端下发的图表配置用 ECharts 渲染
│   ├── DataTable.vue       # 结果表格（二维数组按列下标对齐）+ CSV 导出
│   └── AnswerPanel.vue     # 结论解读（Markdown 渲染 + XSS 净化）
├── types/api.ts         # 与 backend/app/api/schemas.py 一一对应的类型定义
├── utils/
│   ├── format.ts        # 节点中文名、数值/耗时格式化
│   └── markdown.ts      # Markdown 渲染 + DOMPurify 净化
├── App.vue              # 状态编排：所有网络交互与运行状态都在这里
├── main.ts              # 入口，挂载根组件、注册全局错误钩子
└── style.css            # 设计变量与通用样式（卡片、徽标、按钮、表格、Markdown 排版）
```

## 几个值得说明的设计点

### 状态全部收敛在 `App.vue`

子组件只通过 props 收数据、通过 emit 发意图，自己不留业务状态。
这样数据流向是单向的，排查问题时只需要看一个文件。
网络请求也全部集中在 `App.vue` 发起——否则一个页面里会有七八处 `fetch`，
将来要加统一的超时、重试、鉴权，根本不知道该往哪加。

### SSE 客户端有两个容易踩的坑

第一，`EventSource` 只支持 GET，所以流式问答走 `GET /api/query/stream?question=...`。
第二，带名字的事件（`stage` / `trace` / `result` / `error` / `done`）
**不会触发 `onmessage`**，必须用 `addEventListener` 逐个订阅——
只写 `onmessage` 的话会一条事件都收不到。

另外 `error` 这个事件名在浏览器里同时承担两种含义：服务端发来的业务错误是
`MessageEvent`（带 `data`），连接层故障是普通 `Event`（没有 `data`）。
代码里靠 `data` 存不存在来区分，并用一个 `finished` 标记保证只回调一次——
否则正常结束后浏览器会自动重连并触发一次 `onerror`，每次都附带一条假告警。

### 进度不靠猜

步骤条完全由后端推送的 `stage` 事件点亮，前端不做时间估算。
用定时器"预估"进度看起来很流畅，但只要某一节点变慢，
进度条就会和实际状态自相矛盾，那比没有进度条更糟。

### 权限与安全

- 结论文本来自模型，渲染前一律经过 DOMPurify 白名单过滤。
- 重建索引按钮需要先勾选确认——真实模式下这一步会产生向量化费用。
- 前端不持有任何密钥，`.env` 里只放接口地址这类非敏感配置。

## 环境变量

见 `.env.example`。

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `VITE_API_BASE` | `/api` | 接口前缀。前后端不同域名时改成完整地址 |
| `VITE_DEMO` | `false` | 置为 `true` 强制进入离线演示模式 |

> 只有 `VITE_` 开头的变量会被注入前端代码，其余变量不会被打包出去。
> 换句话说，**不要在这里写任何密钥**，前端代码对使用者是完全可见的。

## 部署到静态托管

```bash
VITE_DEMO=true npm run build
```

产物在 `dist/`。`vite.config.ts` 里 `base: './'` 用的是相对路径，
所以直接放进 GitHub Pages 仓库的子目录也能正常加载。
演示模式下不需要后端，页面上会显式标注"离线演示数据"，不会让人误以为是实时结果。
