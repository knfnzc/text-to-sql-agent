"""路由模块。

按资源划分：
    meta.py   健康检查、配置元信息、表结构、索引管理、内置评测问题
    query.py  问答主接口（同步 + SSE 流式）

__all__ 里显式列出 router，让 main.py 的注册代码保持一行一个、可读性最好。
"""

from app.api.routes.meta import router as meta_router
from app.api.routes.query import router as query_router

__all__ = ["meta_router", "query_router"]
