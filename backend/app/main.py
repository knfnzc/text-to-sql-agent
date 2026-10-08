"""FastAPI 应用入口。

启动方式（在 backend/ 目录下执行）：

    uvicorn app.main:app --reload --port 8000

然后打开 http://127.0.0.1:8000/docs 就能看到自动生成的接口文档，
可以直接在页面上点"Try it out"调试每个接口，不需要 Postman。

这个文件只做四件事，业务逻辑一概不放在这里：
    1. 创建应用对象，声明标题与描述（会显示在 /docs 上）
    2. 注册跨域中间件——前后端分离后前端在 5173 端口，不放行会被浏览器拦掉
    3. 启动时准备好数据目录和 Schema 向量索引
    4. 挂载路由

为什么要有 lifespan 而不是在模块顶层直接建索引：
    模块顶层代码在导入时就会执行，包括被测试导入时。
    那会导致"只想跑个单元测试，却顺手调了一次向量化接口"。
    lifespan 只在真正启动服务时触发，测试与导入都干干净净。
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes import meta_router, query_router
from app.api.service import ensure_index
from app.core.config import ensure_data_dirs, settings

logger = logging.getLogger("text2sql.api")


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    """
    应用生命周期钩子：启动时预热，关闭时留痕。

    启动阶段做的事：
        建数据目录 -> 检查/构建 Schema 向量索引。
    索引构建失败不阻断启动，只记一条警告——
    因为索引只影响"检索相关表"这一步的准确度，
    让它把整个服务拖得起不来，是把小问题放大成大故障。
    """
    ensure_data_dirs()

    logger.info("模型配置：%s", settings.llm_config_summary)
    logger.info("离线模拟模式：%s", "已开启" if settings.llm_mock else "已关闭")

    try:
        count = ensure_index(force=settings.force_reindex, verbose=False)
        logger.info("Schema 向量索引就绪，共 %d 条", count)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Schema 索引构建失败，将影响表检索准确度：%s", exc)

    yield

    logger.info("服务已停止")


app = FastAPI(
    title="智能数据分析 Agent · 接口服务",
    description=(
        "把自然语言问题转成 SQL 的 Agent 服务。"
        "链路为：解析意图 → 检索表结构 → 生成 SQL → 安全校验 → 执行 → 解读结论与选图。\n\n"
        "核心特点是 SQL 安全校验层：它独立于模型做硬约束，"
        "拦截写操作、系统表访问、注释注入与全表拉取，并强制追加行数上限。\n\n"
        "问答提供同步与 SSE 流式两套接口，前端默认用流式以展示执行过程。"
    ),
    version="1.0.0",
    lifespan=lifespan,
)

# 跨域配置。
# allow_credentials 保持 False：本服务不依赖 Cookie 鉴权，
# 而不带凭据的 CORS 可以配合通配来源，部署到任意演示域名时更省事
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 所有业务接口统一挂在 /api 前缀下。
# 这样将来要在同一域名下放出静态前端文件时，/api 与页面路由不会互相打架
app.include_router(meta_router, prefix="/api")
app.include_router(query_router, prefix="/api")


@app.get("/", tags=["元信息"], summary="服务信息")
def root() -> dict[str, str]:
    """
    根路径返回一句引导信息。

    存在的意义：让人误访问到后端端口时，能立刻知道这是 API 服务、
    文档在哪、前端在哪个端口，而不是看到一片 404。
    """
    return {
        "service": "text-to-sql-agent",
        "docs": "/docs",
        "health": "/api/health",
        "frontend_dev": "http://localhost:5173",
        "mock_mode": str(settings.llm_mock),
    }
