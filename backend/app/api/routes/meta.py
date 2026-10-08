"""元信息相关接口：健康检查、配置、表结构、索引管理、示例问题。

这些接口的共同点是"不执行问答"，都很轻，前端在页面初始化时一次性拉全，
用来渲染侧边栏和"服务是否就绪"的提示。

把它们和问答接口分开的原因：
    问答是重操作（要调模型、要查库），元信息是轻操作。
    今后要做限流、鉴权、超时，两类接口的策略必然不同，
    路由分开才好在中间件层面按前缀施加不同规则。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.core.config import settings
from app.core.runtime_config import get_runtime_config

from app.api.schemas import (
    HealthResponse,
    IndexStatsResponse,
    LLMConfigResponse,
    LLMConfigUpdateRequest,
    MetaResponse,
    RebuildIndexRequest,
    RebuildIndexResponse,
    TableInfo,
)
from app.api.service import (
    ensure_index,
    get_health,
    get_index_stats_payload,
    get_llm_config_payload,
    get_table_infos,
    reset_llm_config_payload,
    update_llm_config_payload,
)

router = APIRouter(tags=["元信息"])


# 示例问题。挑的标准是"覆盖不同能力"：
# 单表统计、多表关联、时间趋势、反连接、嵌套聚合各一条，
# 让人点几下就能看出系统的能力边界在哪，而不是全挑最简单的显得很厉害
EXAMPLE_QUESTIONS: list[str] = [
    "一共有多少个客户？",
    "各商品类目的总销售额是多少？",
    "最近六个月每个月的销售额是多少？",
    "各会员等级的平均订单金额是多少？",
    "销量最高的 10 个商品是哪些？",
    "退款率最高的商品类目是哪个？",
    "哪些商品从来没有被卖出过？",
    "复购客户占了总客户数的多少比例？",
]


@router.get("/health", response_model=HealthResponse, summary="健康检查")
def health() -> HealthResponse:
    """
    报告服务与依赖是否正常。

    前端在页面加载时调用它，决定要不要显示"索引未就绪"的告警条。
    """
    return HealthResponse(**get_health())


@router.get("/meta", response_model=MetaResponse, summary="配置与限制信息")
def meta() -> MetaResponse:
    """
    返回模型配置、安全限制参数和示例问题。

    安全说明：模型摘要里的密钥已在配置层脱敏（只留前 6 位），
    所以这个接口可以安全地暴露给前端展示。
    """
    runtime = get_runtime_config()

    return MetaResponse(
        # 用"当前生效"的配置而不是 .env 的原始值，
        # 否则用户在界面上换成别的模型后，侧边栏还显示旧模型名
        model_summary=runtime.summary,
        mock_mode=runtime.mock,
        limits={
            "sql_row_limit": settings.sql_row_limit,
            "sql_timeout_seconds": settings.sql_timeout_seconds,
            "sql_max_retries": settings.sql_max_retries,
            "schema_top_k": settings.schema_top_k,
        },
        example_questions=EXAMPLE_QUESTIONS,
    )


@router.get("/tables", response_model=list[TableInfo], summary="表结构与字段说明")
def tables() -> list[TableInfo]:
    """
    返回系统认识的所有表及其字段业务含义。

    前端用它渲染"数据字典"面板。这个面板看着不起眼，但它直观解释了
    为什么这个 Agent 能写出正确的 SQL——它先知道了业务口径，才去写查询。
    """
    return get_table_infos()


@router.get("/index/stats", response_model=IndexStatsResponse, summary="向量索引统计")
def index_stats() -> IndexStatsResponse:
    """返回 Schema 向量索引的统计信息与就绪状态。"""
    return IndexStatsResponse(**get_index_stats_payload())


@router.post("/index/rebuild", response_model=RebuildIndexResponse, summary="重建向量索引")
def rebuild_index(payload: RebuildIndexRequest | None = None) -> RebuildIndexResponse:
    """
    重建 Schema 向量索引。

    注意：真实模式下这一步会调用向量化接口并产生费用，
    所以前端应该在按钮上加二次确认，而不是点一下就发请求。
    """
    request = payload or RebuildIndexRequest()

    try:
        count = ensure_index(force=request.force, verbose=False)
    except Exception as exc:  # noqa: BLE001
        # 向量化失败往往是密钥或网络问题，把原始错误回传，
        # 比返回一个笼统的 500 更容易让人定位
        raise HTTPException(status_code=502, detail=f"重建索引失败：{exc}") from exc

    return RebuildIndexResponse(document_count=count, message=f"索引重建完成，共 {count} 条")


# ===============================================================
# 运行时模型配置（界面可改）
# ===============================================================
# 说明：这里的改动只存在于后端进程内存里，重启后端即回到 .env 的值。
# 之所以不做持久化：写 .env 会让"配置来源"变得模糊——
# 用户下次看文件时不知道这个值是自己改的还是程序改的。


@router.get("/llm/config", response_model=LLMConfigResponse, summary="读取当前生效的模型配置")
def llm_config() -> LLMConfigResponse:
    """
    返回当前生效的模型名、接口地址、脱敏密钥与结构化输出方式。

    密钥只返回脱敏形式：前端不需要原始密钥（提交时留空即不修改），
    返回明文等于凭空多一个泄露面。
    """
    return get_llm_config_payload()


@router.post("/llm/config", response_model=LLMConfigResponse, summary="更新模型配置")
def update_llm_config(payload: LLMConfigUpdateRequest) -> LLMConfigResponse:
    """
    更新模型配置，立即生效于下一次提问。

    只提交需要修改的字段：exclude_unset 保证"前端没提的字段"不会被当成
    "要改成 null"，否则用户只改密钥就会把模型名一起清空。

    校验失败返回 422 并把原因说清楚，因为"接口地址少写了 https://"
    这类错误不看提示很难自己发现。
    """
    changes = payload.model_dump(exclude_unset=True, exclude_none=True)

    try:
        return update_llm_config_payload(changes)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/llm/config/reset", response_model=LLMConfigResponse, summary="恢复 .env 配置")
def reset_llm_config() -> LLMConfigResponse:
    """
    清空界面上的覆盖，恢复为 .env 里的配置。

    用 POST 而不是 DELETE：这个操作不是"删除某个资源"，
    而是"触发一次恢复动作"，语义上更贴近 POST。
    """
    return reset_llm_config_payload()
