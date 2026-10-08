# 智能数据分析 Agent（Text-to-SQL）

用中文提问，Agent 自动完成 **意图解析 → 表结构检索 → SQL 生成 → 安全校验 → 执行查询 → 结论与图表**。

一条自然语言问题，比如「上个月销售额最高的 5 个商品类目是什么」，
进入系统后会被拆成上面六步依次执行，每一步的中间结果都实时推送到界面。
生成 SQL 这一步由模型负责，但**模型说了不算**——所有 SQL 在真正执行前
都要过一道独立于模型的安全校验，不通过就带着拦截原因回到生成环节重写。

前后端完全分离：`backend/` 是 FastAPI 服务，`frontend/` 是独立的 Vue 3 + TypeScript + Vite 工程，
两者通过 REST + SSE 通信。

## 界面

<!-- 截好图后，把下面这行注释换成图片引用即可（放法与命名见 docs/README.md）：
![主界面](docs/screenshot-main.png)
-->

界面分四块：左侧边栏（服务状态 / 模型配置 / 安全限制 / 索引管理 / 示例问题 / 数据字典）、
顶部六步执行链路条、中部结果区（结论、图表、表格、SQL、校验详情）、
右侧执行轨迹时间线。

## 系统架构

```
┌───────────────────────────────────────────────────────────────┐
│  浏览器  http://localhost:5173                                 │
│  Vue 3 + TypeScript + Vite                                     │
│  状态全部收敛在 App.vue；子组件只收 props、只发 emit             │
└───────────────────────────┬───────────────────────────────────┘
                            │  /api/*（Vite 代理 → 同源，无 CORS 问题）
                            │  REST 取元信息  +  SSE 取执行过程
┌───────────────────────────▼───────────────────────────────────┐
│  FastAPI  http://127.0.0.1:8000                                │
│  app/main.py        应用装配、CORS、lifespan 预热               │
│  app/api/routes/    meta.py（元信息）/ query.py（问答）          │
│  app/api/service.py 唯一的衔接层：图单例、索引预热、           │
│                     结果序列化、"线程 + 队列"桥接 SSE          │
└───────────────────────────┬───────────────────────────────────┘
                            │
┌───────────────────────────▼───────────────────────────────────┐
│  LangGraph 状态图（app/agent/safety/sql_graph.py）              │
│                                                                │
│   解析意图 → 检索表结构 → 生成 SQL ⇄ 安全校验 → 执行查询        │
│                              ▲     │                           │
│                              └─────┘  两条重试回边              │
│                           失败且还有重试次数                    │
└───────┬──────────────────────────────┬────────────────────────┘
        │                              │
┌───────▼────────────┐      ┌──────────▼──────────────────────┐
│ SQLite             │      │ Chroma 向量库                    │
│ data/ecommerce.db  │      │ data/chroma/                     │
│ 客户/商品/订单五表 │      │ 表与字段描述 → 向量，用于表检索   │
└────────────────────┘      └─────────────────────────────────┘
```

## 几个值得说明的设计点

### SQL 安全校验层独立于模型

这是整个项目里最值得看的一层。把数据库交给大模型自由发挥是不安全的：
模型可能生成 `DELETE`、可能去读 `sqlite_master`、可能用注释把校验逻辑绕过去、
也可能一个 `SELECT *` 把百万行拉进内存。

`app/agent/safety/sql_guard.py` 用 `sqlparse` 解析语法树而不是做字符串匹配
（字符串匹配能被注释、大小写、多语句轻易绕过），硬性检查四类问题：
写操作与 DDL、系统表访问、注释注入、无条件全表拉取；并为查询强制追加行数上限。
校验不通过时，拦截原因会作为反馈写进状态，让模型带着"上次为什么被拒"重写。

关键点是这条约束**不依赖模型配合**——即使换一个更弱的模型，
不可能执行的 SQL 依然出不了这一层。

### 用状态图而不是一串函数调用

基础链路版（`app/agent/schema_rag/text2sql_chain.py`）是一条直线函数调用。
一旦要加两条重试分支就变得很难看：嵌套 `while` + 状态变量 + 各种 `break` 条件，
重试一次和两次的逻辑还要复制一遍，中间结果也没地方存。

状态图版本（`app/agent/safety/sql_graph.py`）里，
"回到哪一步"是一条边，"还能重试几次"是状态里的一个字段，
流程变成声明式的、看得见的。两条回边的触发条件分别是
**SQL 校验不通过**与**数据库执行报错**，两条都带着具体原因回到生成节点。

### 离线可跑，零密钥

`.env` 里 `LLM_MOCK=true`（默认值）时，对话模型与向量模型都会切换到
`app/core/mock_llm.py` 里的离线替身，接口与真实实现完全一致，业务代码一行不用改。

模拟对话模型会按结构化输出的字段注解反向构造合法响应——包括
从问题的关键词里挑出真实的取值去填 `WHERE` 条件，所以离线状态下
问出来的 SQL 是能真跑出结果的，不是把一句假 SQL 打印出来。
模拟向量模型用「字符二元组 + 哈希落桶 + L2 归一化」，
语义相近的文本向量确实更接近，**表检索与语义去重在离线模式下是真的在起作用**。

因此：没有 API Key、不联网，也能把完整链路跑通并演示。

### SSE 用「线程 + 队列」桥接

`LangGraph` 的 `stream()` 是同步生成器，SSE 需要异步生成器。
直接在事件循环里跑同步生成器会把整个事件循环卡住（其他请求全部排队）。

`app/api/service.py` 的做法是把它放进工作线程，用线程安全队列把事件交回事件循环，
并且**必须用 `call_soon_threadsafe`**——`asyncio.Queue` 不是线程安全的，
从工作线程直接塞会破坏其内部状态。

## 快速开始

前置条件：Python ≥ 3.11、Node.js ≥ 18。

### 1. 启动后端

```bash
cd backend

# 建议先建虚拟环境
python -m venv .venv
# Windows:  .venv\Scripts\activate
# macOS/Linux:  source .venv/bin/activate

pip install -r requirements.txt

# 把配置模板复制成实际配置。默认 LLM_MOCK=true，不需要填任何密钥
# Windows:  copy .env.example .env
# macOS/Linux:  cp .env.example .env

# 【必须执行】初始化示例数据库，生成 data/ecommerce.db
python -m app.agent.database.init_database

uvicorn app.main:app --reload --port 8000
```

数据库这一步不能跳过：本仓库把 `data/ecommerce.db` 与 `data/chroma/`
当作**可再生的本地产物**排除在版本控制之外，新克隆的仓库里没有这两个东西。
`init_database` 会建出 5 张表（客户 / 商品 / 订单 / 订单明细 / 退款），
写入约 1200 条订单与 3000 余条订单明细，数据用固定随机种子生成、每次跑出来都一致。
`data/chroma/` 里的向量索引则由服务启动时的 `lifespan` 自动构建（失败不阻断启动，
只记一条警告，因为索引只影响"检索相关表"的准确度）。

初始化完成后可以顺手验证一下：`python -m app.agent.database.test_questions`
会打印各表行数、订单状态分布、按月销售额等样本查询结果。

跑完后：

- <http://127.0.0.1:8000/docs> — 自动生成的接口文档，可以直接点着试
- <http://127.0.0.1:8000/api/health> — 健康检查

### 2. 启动前端

```bash
cd frontend
npm install
npm run dev
```

打开 <http://localhost:5173>。

开发服务器会把 `/api` 代理到 `http://127.0.0.1:8000`，所以前端代码里统一写相对路径 `/api`，
不需要为不同环境改地址，也**不会触发跨域**——只有前后端部署在不同域名时才需要配 CORS。

### 没有后端也能看界面

后端未启动时，页面会给出提示，并提供「用内置演示数据预览界面」的入口。
点它之后会用 `frontend/src/api/demo.ts` 里的样例响应按真实的事件顺序重放一遍，
步骤条与执行轨迹都会走完。也可以直接在 `frontend/.env.local` 里写 `VITE_DEMO=true`
强制进入演示模式，此时一个请求都不发。

这样把构建产物发到 GitHub Pages 上也能完整演示，而不只是放一张截图。

## 接口一览

所有业务接口挂在 `/api` 前缀下。完整的请求/响应模型见 `/docs`。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/health` | 健康检查：数据库连通性、索引是否就绪、当前是否模拟模式 |
| GET | `/api/meta` | 配置与限制信息（模型、行数上限、超时、重试次数等），供前端展示 |
| GET | `/api/tables` | 表结构与字段说明，即"这个系统认识哪些数据" |
| GET | `/api/index/stats` | Schema 向量索引的统计信息与就绪状态 |
| POST | `/api/index/rebuild` | 重建向量索引（真实模式下会产生向量化费用） |
| GET | `/api/llm/config` | 读取当前生效的模型配置，密钥只返回脱敏形式 |
| POST | `/api/llm/config` | 更新模型配置，下一次提问生效 |
| POST | `/api/llm/config/reset` | 清空界面上的覆盖，恢复为 `.env` 里的配置 |
| POST | `/api/query` | 提问，同步返回完整结果 |
| GET | `/api/query/stream` | 提问，SSE 流式推送执行过程 |

SSE 事件类型：`stage`（某节点完成）、`trace`（一条执行轨迹）、`result`（完整结果）、
`error`（执行失败）、`done`（流结束）。心跳 15 秒一次，用于穿过反向代理与浏览器空闲超时。

前端默认走流式接口，因为"能看到 Agent 正在做什么"是这个项目要展示的核心，
同步接口主要服务于脚本调用、联调与测试。

## 在界面上更换模型与密钥

侧栏的「模型与密钥」面板可以改**后端进程里当前生效的配置**，保存后对下一次提问立即生效，
不用改 `.env`，也不用重启后端。可改的是这几项：

| 项 | 说明 |
| --- | --- |
| 模型名称 | 例如 `deepseek-chat` |
| 接口地址 | 必须以 `http://` 或 `https://` 开头，末尾斜杠自动去掉 |
| API Key | 输入框永远为空（后端不下发原文），**留空即表示沿用当前密钥** |
| 结构化输出方式 | `function_calling` / `json_schema` / `json_mode` |
| 关闭思考模式 | 接 DeepSeek 这类默认开思考的模型时必须打开 |

实现要点（都在 `backend/app/core/runtime_config.py`）：

- 当前生效值 = `覆盖层里有 ? 用户填的值 : .env 里的值`，恢复默认就是把覆盖层清空，
  所以不存在"恢复成错的值"这种情况。
- 覆盖层只存在进程内存里，**重启后端即回到 `.env`**。刻意不写回 `.env`——
  写文件会让"这个值是我改的还是程序改的"变得不可分辨。
- 改配置后必须丢掉已编译的 LangGraph 状态图（`service.reset_agent_graph()`）：
  图在编译期就绑定好了结构化输出链，链里持有具体模型实例，不重建的话
  界面显示改了、实际请求还在用旧密钥。
- `_build_chat_model` 的 `lru_cache` 把 `base_url` / `api_key` 一起作为缓存键，
  否则换密钥后相同模型名会命中旧实例。
- 温度、离线模拟开关（`LLM_MOCK`）**不在可改范围内**：前者放开会破坏 Text-to-SQL
  的可复现性，后者是部署级开关，界面上只做只读展示。

## 目录结构

```
text-to-sql-agent/
├── backend/
│   ├── app/
│   │   ├── main.py                    # 应用装配：CORS、lifespan 预热、挂载路由
│   │   ├── api/
│   │   │   ├── routes/meta.py         # 健康检查、元信息、表结构、索引管理、运行时模型配置
│   │   │   ├── routes/query.py        # 问答：同步 + SSE 流式
│   │   │   ├── schemas.py             # 对外的请求/响应模型（前端类型定义的依据）
│   │   │   └── service.py             # Agent 与接口层之间唯一的衔接点
│   │   ├── agent/
│   │   │   ├── state.py               # 状态定义与初始状态
│   │   │   ├── safety/sql_guard.py    # SQL 安全校验（不依赖模型的硬约束）
│   │   │   ├── safety/sql_graph.py    # LangGraph 主链路 + 两条重试回边
│   │   │   ├── schema_rag/
│   │   │   │   ├── schema_indexer.py  # 表描述向量化与检索
│   │   │   │   └── text2sql_chain.py  # 基础链路版（直线函数调用，用于对照）
│   │   │   ├── output/renderer.py     # 结论解读与图表选型
│   │   │   └── database/
│   │   │       ├── init_database.py   # 建表 + 生成示例数据（需手动执行一次）
│   │   │       ├── schema_definition.py   # 表与字段的中文描述
│   │   │       └── test_questions.py  # 30 条评测问题集
│   │   └── core/
│   │       ├── config.py              # 配置读取与自检
│   │       ├── runtime_config.py      # 运行时配置覆盖层（界面上更换模型 / 密钥的落点）
│   │       ├── llm.py                 # 模型工厂（真实 / 离线切换点）
│   │       ├── mock_llm.py            # 离线替身：模拟对话模型 + 模拟向量模型
│   │       └── db.py                  # 连接管理与查询执行
│   ├── data/                          # 本地产物，不入版本控制
│   ├── legacy/streamlit_app.py        # 第一版 Streamlit 界面，仅作演进留档
│   └── tests/                         # 169 个用例
└── frontend/                          # Vue 3 + TypeScript + Vite（详见 frontend/README.md）
```

## 环境变量

后端配置在 `backend/.env`（从 `backend/.env.example` 复制）。
带默认值、且默认值就能跑通离线演示的变量不需要动。

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `LLM_MOCK` | `true` | 离线模拟总开关。`true` 时对话与向量模型都走本地替身，不联网、零成本 |
| `LLM_API_KEY` | `sk-replace-me` | 模型密钥。`LLM_MOCK=false` 时必填 |
| `LLM_BASE_URL` | OpenAI 官方地址 | 换 DeepSeek / 通义 / 智谱等兼容服务时改这里 |
| `LLM_MODEL` | `gpt-4o-mini` | 对话模型名 |
| `LLM_TEMPERATURE` | `0` | 生成 SQL 需要确定性，默认给 0 |
| `LLM_MAX_TOKENS` | `2000` | 单次回复长度上限 |
| `LLM_TIMEOUT` | `60` | 单次调用超时（秒） |
| `LLM_MAX_RETRIES` | `2` | SDK 内部重试次数 |
| `LLM_DISABLE_THINKING` | `false` | 是否显式关闭模型思考模式。接 DeepSeek 这类默认开思考的模型必须设 `true`，否则温度会被静默忽略、结构化输出会被拒绝 |
| `LLM_STRUCTURED_METHOD` | `function_calling` | 结构化输出的实现方式。DeepSeek 不支持 `json_schema`，必须用 `function_calling` |
| `EMBEDDING_PROVIDER` | `openai` | 向量化服务提供方 |
| `EMBEDDING_MODEL` | `text-embedding-3-small` | 向量模型名 |
| `EMBEDDING_DIM` | `1536` | 向量维度，需与所选模型一致，否则索引构建会报错 |
| `EMBEDDING_MOCK` | `false` | 把向量化单独留在离线替身。DeepSeek 没有 embedding 接口，接它时需设为 `true`，否则 Schema 检索会整条失败 |
| `DB_URL` | `sqlite:///./data/ecommerce.db` | 数据库连接串，换 PostgreSQL 时改这里 |
| `SQL_ROW_LIMIT` | `500` | 查询强制追加的行数上限 |
| `SQL_TIMEOUT_SECONDS` | `10` | 单条查询超时 |
| `SQL_MAX_RETRIES` | `2` | 生成 SQL 的最大重试次数 |
| `SCHEMA_TOP_K` | `3` | 表检索返回的候选表数量 |
| `CHROMA_PERSIST_DIR` | `./data/chroma` | 向量库持久化目录 |
| `FORCE_REINDEX` | `false` | 启动时是否强制重建索引 |
| `CORS_ORIGINS` | `http://localhost:5173` | 允许的前端来源，多个用逗号分隔 |
| `REDIS_URL` | `redis://localhost:6379/0` | 缓存与会话存储（可选） |
| `TAVILY_API_KEY` / `SEARCH_MAX_RESULTS` | — | 历史链路保留的搜索配置，当前链路不依赖 |

前端配置见 `frontend/.env.example`，只有 `VITE_` 开头的变量会被注入前端代码——
**不要在前端放任何密钥**，前端代码对使用者完全可见。

## 测试

```bash
cd backend
python -m pytest tests -q
```

169 个用例，覆盖 SQL 安全校验的四类拦截规则与边界（含大小写变形、注释拼接、
多语句等绕过尝试）、接口层的请求校验与响应结构、运行时模型配置的读写改与非法值拦截、
离线模拟模式下 SQL 生成能真正命中数据。

## 部署

**前端**：`VITE_DEMO=true npm run build`，产物在 `frontend/dist/`。
`vite.config.ts` 里 `base: './'` 用的是相对路径，直接放进 GitHub Pages 子目录也能加载。
演示模式下页面会显式标注"离线演示数据"，不会让人误以为是实时结果。
要连真实后端，把 `VITE_API_BASE` 指向后端完整地址，并把前端域名加进后端的 `CORS_ORIGINS`。

**后端**：`uvicorn app.main:app --host 0.0.0.0 --port 8000`（生产建议前置 Nginx 或其它反向代理托管静态产物）。

### 用 Docker 一键起

```bash
docker compose up --build
```

打开 <http://localhost:8080>。默认就是离线模拟模式，**不需要任何密钥**。
要连真实模型，用 shell 环境变量覆盖：

```bash
LLM_MOCK=false LLM_API_KEY=sk-xxx LLM_BASE_URL=https://api.deepseek.com/v1 \
  docker compose up --build
```

镜像分两个：`backend/Dockerfile`（python-slim + uvicorn，构建期就把示例数据库建好）
和 `frontend/Dockerfile`（node 构建 → nginx 托管，多阶段，产物里不含 Node）。
前端遇到真实后端时是**浏览器直连**，所以 compose 里已经把 nginx 的地址写进了后端的 `CORS_ORIGINS`。

> Vite 的环境变量是在**构建期**写进产物的，所以 `VITE_API_BASE` 走的是 build args 而不是
> `environment`——写成后者在浏览器端根本读不到。

#### 实测记录

| 项 | 实测值 |
| --- | --- |
| 镜像 | `text-to-sql-agent-backend` 1.11 GB、`text-to-sql-agent-frontend` 74.6 MB |
| 容器 | backend `Up (healthy)`、frontend `Up`（nginx 静态站没配 HEALTHCHECK） |
| 端口 | `8080 → 前端`、`8000 → 后端` |

```bash
$ curl -s localhost:8000/api/health
{"status":"ok","mock_mode":true,"database_connected":true,"index_ready":true,
 "index_document_count":5,"index_expected_count":5}
```

`index_document_count: 5` 对应 5 张表的 Schema 描述，说明镜像里 Chroma 集合已经建好。
构建日志中能看到 `RUN python -m app.agent.database.init_database` 的真实输出——
三张表建好并灌入 1200 条订单，示例库是**烘进镜像**的，容器启动时无需再初始化。
构建期建库而不是启动期，是因为 `drop_first` 默认为真，每次重启重建纯属浪费。

接口侧逐条验证过：`POST /api/query` 返回完整的
`intent / sql / safe_sql / validation / execution / chart / answer` 结构，
`GET /api/query/stream` 依次推送 `stage → trace → result → done` 四类事件，
`/api/tables`、`/api/index/stats` 均为 200。

前端产物里也能搜到烘焙好的地址，证明 build args 生效：

```bash
$ grep -o 'http://localhost:[0-9]*/api' frontend/dist/assets/index-*.js
http://localhost:8000/api
```

浏览器里也真的点过（`docker compose up` 之后访问 8080）：
「一共有多少个客户？」出大数字卡片 200；「各商品类目的总销售额是多少？」
出 5 根柱子的柱状图，结果表里是真实算出来的 2894293.42 / 2789809.46 等数值；
两种情形控制台都没有报错。

> 这一轮实跑顺带修掉了两个只在离线模拟模式下才会暴露的缺陷：
> 模拟模型不是 LangChain 的 `Runnable`，`prompt | llm` 这种链式写法会直接抛
> `Expected a Runnable, callable or dict`，让「分析结论」显示成一段报错；
> 以及前端定位图表列时，"x 不能等于 y" 的兜底会把单值卡片的 y 轴下标改回 -1，
> 卡片退化成"没有可展示的数值"。两处都已修复并复验。

### 持续集成

`.github/workflows/ci.yml` 两个任务：后端在 `backend/` 下装依赖、初始化示例数据库、跑 pytest；
前端在 `frontend/` 下 `npm ci` 后执行 `npm run build`（含 `vue-tsc` 类型检查，
所以类型错误会让 CI 直接红掉，不会拖到部署时才发现）。

其中一个坑值得单独记一笔：CI 里必须显式设 `LLM_MOCK=true`。
代码里这个配置项的默认值是 `false`，是 `.env.example` 把它写成 `true` 的，
而 `.env` 不入版本控制、CI 里并不存在——不显式打开，测试就会拿假 Key 去打真实模型接口。

## 相关文档

- [`frontend/README.md`](frontend/README.md) — 前端工程说明：技术选型、SSE 客户端的三个坑、设计点
- [`backend/legacy/README.md`](backend/legacy/README.md) — 第一版 Streamlit 界面为何被替换
