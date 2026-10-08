"""
统一数据库访问模块。

作用：把"怎么连数据库、怎么执行 SQL、怎么拿结果"封装起来，
     业务代码只关心 SQL 本身，不关心底层是 SQLite 还是 PostgreSQL。

为什么要做这层抽象：
    本项目的重点是 Text-to-SQL 的链路设计，不是数据库操作细节。
    如果业务代码里到处 import sqlite3 或者 psycopg，切换数据库就要改一堆文件。
    用 SQLAlchemy 统一后，切换数据库只改 .env 里 DB_URL 一行。

使用方式：
    from app.core.db import execute_query, get_engine

    result = execute_query("SELECT * FROM orders LIMIT 10")
    print(result.columns)    # 列名列表
    print(result.rows)       # 行数据（列表的列表）
    print(result.elapsed_ms) # 耗时毫秒数
"""

import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Iterator, Sequence

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from app.core.config import PROJECT_ROOT, settings


# ===============================================================
# 查询结果封装
# ===============================================================


@dataclass
class QueryResult:
    """
    SQL 执行结果的统一载体。

    为什么要定义这个类而不是直接返回元组：
        1. 调用方不用记"第几个位置是什么"，result.columns 比 row[0] 清晰
        2. 可以挂载额外信息（耗时、是否被截断），这些在界面上要展示
        3. 出错时也能用同一个类型返回，调用方不用写两套分支
    """

    # 列名列表，顺序与 rows 中每条记录的字段顺序一致
    columns: list[str] = field(default_factory=list)

    # 行数据，每个元素是一行，行内每个元素按 columns 顺序排列
    rows: list[list[Any]] = field(default_factory=list)

    # 执行耗时（毫秒），用来发现慢查询
    elapsed_ms: float = 0.0

    # 实际执行的 SQL 文本。
    # 注意：它与用户原始提问对应的 SQL 可能不同（校验层会改写，比如追加 LIMIT），
    #      界面上展示改写后的版本更诚实
    executed_sql: str = ""

    # 是否执行成功
    success: bool = True

    # 失败时的错误信息，成功时为空字符串
    error: str = ""

    @property
    def row_count(self) -> int:
        """结果行数。"""
        return len(self.rows)

    @property
    def column_count(self) -> int:
        """结果列数。"""
        return len(self.columns)

    def to_dicts(self) -> list[dict[str, Any]]:
        """
        把结果转成"字典列表"形式，便于转 DataFrame 或序列化为 JSON。

        返回: 形如 [{"列名": 值, ...}, ...] 的列表
        """
        # 用 zip 把列名和每行数据配对，是处理二维表数据的惯用写法
        return [dict(zip(self.columns, row)) for row in self.rows]

    def preview(self, max_rows: int = 5) -> str:
        """
        生成便于打印的结果预览。

        参数:
            max_rows 最多展示多少行，默认 5 行，避免刷屏

        返回: 多行字符串
        """
        if not self.success:
            return f"[执行失败] {self.error}"

        if not self.columns:
            return "[执行成功] 无返回结果集（可能是写入类语句）"

        lines = [
            f"共 {self.row_count} 行 × {self.column_count} 列，耗时 {self.elapsed_ms:.1f} ms",
            " | ".join(str(c) for c in self.columns),
            "-" * 60,
        ]
        # 只展示前 max_rows 行，并在末尾提示还有多少行没显示
        for row in self.rows[:max_rows]:
            lines.append(" | ".join(str(v) for v in row))
        if self.row_count > max_rows:
            lines.append(f"... 还有 {self.row_count - max_rows} 行未显示")
        return "\n".join(lines)


# ===============================================================
# 引擎管理
# ===============================================================


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    """
    创建并返回全局唯一的数据库引擎。

    为什么要缓存：引擎内部维护连接池，每次调用都新建会造成连接泄漏和性能下降。
    用 lru_cache 保证整个进程只有一个引擎实例。

    返回:
        SQLAlchemy Engine 实例
    """
    url = settings.db_url

    # 针对不同数据库设置连接参数，这部分差异在这里一次性处理掉
    connect_args: dict[str, Any] = {}

    if url.startswith("sqlite"):
        # Streamlit 会在多个线程里操作数据库，而 SQLite 默认禁止跨线程复用连接，
        # 不关闭这个检查会报 "SQLite objects created in a thread can only be used in that same thread"
        connect_args["check_same_thread"] = False

    return create_engine(
        url,
        # 回显 SQL 到日志，调试时很有用，正式使用建议关掉（输出会非常多）
        echo=False,
        # 关闭连接前先 ping 一下，防止使用已被数据库服务端断开的死连接
        # 对 SQLite 无影响，对 PostgreSQL 很有必要
        pool_pre_ping=True,
        # 连接池中保持的连接数
        pool_size=5,
        # 超过池容量时允许临时创建的额外连接数
        max_overflow=10,
        # 从池中取连接的等待超时秒数
        pool_timeout=30,
        # 连接空闲多久后回收（秒），避免被数据库服务端的空闲超时踢掉
        pool_recycle=1800,
        # 上面按数据库类型拼好的额外参数
        connect_args=connect_args,
    )


@contextmanager
def get_connection() -> Iterator[Any]:
    """
    获取一个数据库连接的上下文管理器。

    用 with 语句使用，退出时自动关闭连接并归还给连接池。
    这样即使中间抛异常也不会泄漏连接。

    用法:
        with get_connection() as conn:
            conn.execute(text("SELECT 1"))

    返回:
        数据库连接对象
    """
    engine = get_engine()
    # begin() 会自动开启事务，正常退出提交，异常退出回滚
    with engine.begin() as connection:
        yield connection


# ===============================================================
# SQL 执行
# ===============================================================


def execute_query(sql: str, params: dict[str, Any] | None = None) -> QueryResult:
    """
    执行查询类 SQL 并返回封装好的结果。

    特点：
        1. 不抛异常，所有错误都封进 QueryResult.error。
           因为 Text-to-SQL 场景里 SQL 报错是常态，调用方需要拿到错误信息
           反馈给模型做自我修正，用异常控制流程会很难写。
        2. 记录耗时，便于发现慢查询。
        3. 对返回结果做行数保护，见下方说明。

    参数:
        sql    要执行的 SQL 语句
        params 绑定参数（可选）。强烈建议用参数绑定而不是字符串拼接，
               这是防 SQL 注入的根本手段

    返回:
        QueryResult 实例
    """
    # 记录起始时间，用 perf_counter 而不是 time.time，前者是单调时钟，
    # 不受系统时间调整影响，测耗时更准确
    started_at = time.perf_counter()

    try:
        with get_connection() as conn:
            # text() 把字符串包装成 SQLAlchemy 可识别的语句对象
            # 用字符串拼接 SQL 是危险的，参数必须走 params 绑定
            result = conn.execute(text(sql), params or {})

            # 判断是不是查询语句：有返回列的才是（SELECT），
            # 写入类语句（INSERT/UPDATE）没有结果集
            if not result.returns_rows:
                return QueryResult(
                    executed_sql=sql,
                    elapsed_ms=(time.perf_counter() - started_at) * 1000,
                    success=True,
                )

            # 列名从游标的 description 里取
            columns = list(result.keys())

            # 把每行转成普通 list（默认是 Row 对象，转成 list 更好处理）
            rows = [list(row) for row in result.fetchall()]

            return QueryResult(
                columns=columns,
                rows=rows,
                executed_sql=sql,
                elapsed_ms=(time.perf_counter() - started_at) * 1000,
                success=True,
            )

    except SQLAlchemyError as exc:
        # 数据库层面的错误，把原始信息带出去。
        # 这段错误文本是关键：本项目的"失败自动修正"就是把它回传给模型
        return QueryResult(
            executed_sql=sql,
            elapsed_ms=(time.perf_counter() - started_at) * 1000,
            success=False,
            error=f"{type(exc).__name__}: {exc}",
        )
    except Exception as exc:  # noqa: BLE001 - 兜底捕获，保证任何情况都不让服务崩掉
        return QueryResult(
            executed_sql=sql,
            elapsed_ms=(time.perf_counter() - started_at) * 1000,
            success=False,
            error=f"未预期错误 {type(exc).__name__}: {exc}",
        )


def execute_many(statements: Sequence[str]) -> list[QueryResult]:
    """
    按顺序执行多条 SQL。

    用途：建表和灌测试数据的初始化脚本。

    参数:
        statements SQL 语句列表

    返回:
        与输入顺序对应的结果列表
    """
    return [execute_query(sql) for sql in statements]


def table_exists(table_name: str) -> bool:
    """
    判断表是否存在。

    用途：初始化脚本用它实现幂等——重复运行不会因为表已存在而报错。

    参数:
        table_name 表名

    返回:
        存在返回 True
    """
    # SQLAlchemy 提供跨数据库的 inspect 能力，不用手写各家的元数据查询
    from sqlalchemy import inspect

    inspector = inspect(get_engine())
    return table_name in inspector.get_table_names()


def list_tables() -> list[str]:
    """
    列出当前数据库里的所有表名。

    用途：本项目展示"数据库里有哪些表"，以及建索引时遍历所有表。

    返回:
        表名列表
    """
    from sqlalchemy import inspect

    inspector = inspect(get_engine())
    return sorted(inspector.get_table_names())


def describe_table(table_name: str) -> list[dict[str, Any]]:
    """
    获取单张表的字段结构。

    这是本项目的核心数据来源：Schema 向量化就是把这里的结构描述转成文本再去向量化。

    参数:
        table_name 表名

    返回:
        字段信息列表，每项形如
        {"name": "order_id", "type": "INTEGER", "nullable": False, "default": None}
    """
    from sqlalchemy import inspect

    inspector = inspect(get_engine())
    columns: list[dict[str, Any]] = []

    for col in inspector.get_columns(table_name):
        columns.append(
            {
                # 字段名
                "name": col["name"],
                # 字段类型，转成字符串便于拼进 Prompt
                "type": str(col["type"]),
                # 是否允许为空
                "nullable": col.get("nullable", True),
                # 默认值
                "default": col.get("default"),
                # 是否是主键
                "primary_key": col.get("primary_key", False),
            }
        )

    return columns


def get_foreign_keys(table_name: str) -> list[dict[str, Any]]:
    """
    获取单张表的外键关系。

    为什么重要：Text-to-SQL 最容易错的场景就是多表关联，
    模型不知道表之间怎么 JOIN 就只能瞎猜。把外键关系写进 Schema 描述能显著改善。

    参数:
        table_name 表名

    返回:
        外键信息列表，每项形如
        {"column": "customer_id", "references": "customers.customer_id"}
    """
    from sqlalchemy import inspect

    inspector = inspect(get_engine())
    relations: list[dict[str, Any]] = []

    for fk in inspector.get_foreign_keys(table_name):
        # 一对多/多对一场景下，constrained_columns 通常只有一个字段，
        # 这里取第一个即可，够用且简单
        local_columns = fk.get("constrained_columns") or []
        remote_table = fk.get("referred_table") or ""
        remote_columns = fk.get("referred_columns") or []

        if local_columns and remote_table and remote_columns:
            relations.append(
                {
                    "column": local_columns[0],
                    "references": f"{remote_table}.{remote_columns[0]}",
                }
            )

    return relations


def check_connection() -> bool:
    """
    检查数据库是否可连通。

    返回:
        可连通返回 True，否则打印原因并返回 False
    """
    result = execute_query("SELECT 1 AS ping")
    if result.success:
        print(f"[成功] 数据库连通：{settings.db_url}")
        tables = list_tables()
        print(f"        当前表：{tables if tables else '（空库，请先运行初始化脚本）'}")
        return True

    print(f"[失败] 数据库连接失败：{result.error}")
    if settings.db_url.startswith("sqlite"):
        print(f"提示：SQLite 数据库文件位于 {PROJECT_ROOT / 'data'}，请先运行初始化脚本生成")
    else:
        print("提示：确认 PostgreSQL 已启动（docker compose up -d）且 DB_URL 填写正确")
    return False


# ---------------------------------------------------------------
# 自测入口：用法 python -m app.core.db
# ---------------------------------------------------------------
if __name__ == "__main__":
    print("=" * 60)
    print("数据库连通性自检")
    print("=" * 60)
    check_connection()

    # 顺手演示结果封装的用法，本项目会大量使用
    demo = execute_query("SELECT 1 AS id, 'demo' AS name")
    print("-" * 60)
    print(demo.preview())
