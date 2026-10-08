# legacy：第一版 Streamlit 界面

## 这是什么

本目录保存的是项目早期的 **Streamlit 单文件界面**（`streamlit_app.py`），
它对应初版 Streamlit 界面阶段的产物。功能上它已经能完整跑通
"提问 -> 生成 SQL -> 安全校验 -> 执行 -> 解读 -> 画图"整条链路。

## 为什么被取代

从"可展示的作品集项目"角度看，Streamlit 有明确的局限：

| 维度 | Streamlit 版 | 前后端分离版（frontend/ + backend/） |
|---|---|---|
| 交互形态 | 整页重跑，交互粒度粗 | 前端按需请求，可做流式输出与局部刷新 |
| 接口边界 | 无 HTTP 接口，逻辑与界面耦合 | backend 暴露 REST/SSE 接口，可被任意客户端复用 |
| 可测试性 | 逻辑依赖 Streamlit 运行时，难以独立测试 | 业务逻辑在 `app/agent/**`，可被 pytest 直接覆盖 |
| 工程结构 | 所有状态塞在 `st.session_state` | 状态由 LangGraph 状态图承载，可序列化、可持久化 |

因此正式版本的界面已迁移到独立的 `frontend/`（Vue3）。
本目录仅作为**演进过程的历史留存**，方便对比"为什么要做前后端分离"。

## 它还在干什么

`streamlit_app.py` 里被复用的部分已经全部抽到 `app/agent/**` 与 `app/core/**`：

- `app.core.config` —— 配置读取
- `app.core.db` —— 数据库连通性检查
- `app.agent.schema_rag.schema_indexer` —— 建索引 / 查索引状态
- `app.agent.safety.sql_graph` —— 编译后的状态图
- `app.agent.output.renderer` —— 图表配置选择、绘图、结果解读
- `app.agent.state` —— 初始状态工厂

也就是说，本文件现在只是一个**很薄的界面壳**，不含任何业务逻辑。

## 如何运行（可选）

工作目录切到 `backend/`：

```powershell
pip install streamlit
streamlit run legacy/streamlit_app.py
```

说明：`requirements.txt` 是为正式后端准备的依赖清单，按规格**未包含 streamlit**
（它只服务于这个历史界面）。所以想跑这个旧界面需要单独安装。
