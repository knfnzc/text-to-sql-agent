"""接口层的共享服务：状态图单例、索引预热、结果序列化、流式执行。

这是 app/agent/ 与 app/api/ 之间的唯一衔接点。路由函数只调用这里的函数，
不直接碰 Agent 内部实现，这样将来改内部结构时改动面只集中在这一个文件。

两个值得说明的设计：

1. 状态图只编译一次
   build_graph() 内部要构造提示词模板、绑定结构化输出、组装节点与条件边，
   开销不大但完全没必要每次请求都做一遍。这里用"双检锁"做成进程内单例，
   和 Streamlit 时代用 st.cache_resource 达到的效果一样，
   但不再依赖任何框架特性——单例就是普通的 Python 代码。

2. 流式执行用"线程 + 队列"桥接
   LangGraph 的 stream() 是同步生成器，而 SSE 需要异步生成器。
   直接在事件循环里跑同步生成器会把整个事件循环卡住（其他请求全部排队）。
   正确做法是把它放到工作线程里跑，通过线程安全队列把事件交回事件循环。
   call_soon_threadsafe 是关键：它保证队列操作发生在事件循环线程上，
   避免了跨线程直接操作 asyncio.Queue 会引发的竞态。
"""

from __future__ import annotations

import asyncio
import json
import threading
from typing import Any, AsyncIterator

from app.agent.database.schema_definition import (
    COLUMN_DESCRIPTIONS,
    TABLE_DESCRIPTIONS,
    get_table_names,
)
from app.agent.output.renderer import decide_chart_spec, interpret_result
from app.agent.safety.sql_graph import build_graph
from app.agent.safety.sql_guard import validate_sql
from app.agent.schema_rag.schema_indexer import build_index, get_index_stats
from app.agent.state import create_initial_state
from app.core.config import settings
from app.core.db import check_connection
from app.core.runtime_config import (
    STRUCTURED_METHODS,
    LLMRuntimeConfig,
    get_runtime_config,
    reset_runtime_config,
    update_runtime_config,
)

from app.api.schemas import (
    ChartPayload,
    ExecutionPayload,
    LLMConfigResponse,
    QueryResponse,
    TableInfo,
    ValidationPayload,
)


# ===============================================================
# 状态图单例
# ===============================================================

_graph_lock = threading.Lock()
_compiled_graph: Any = None


def get_agent_graph() -> Any:
    """
    获取编译好的 LangGraph 状态图，进程内只编译一次。

    返回:
        已编译的图对象，可直接 invoke / stream
    """
    global _compiled_graph

    # 第一次判空在锁外：绝大多数请求进来时单例已存在，
    # 不必为了读一个引用去抢锁。只有为空时才进锁，锁内再判一次防止重复构造
    if _compiled_graph is None:
        with _graph_lock:
            if _compiled_graph is None:
                _compiled_graph = build_graph()
    return _compiled_graph


def reset_agent_graph() -> None:
    """
    丢弃已编译的图，下次请求时按新配置重新编译。

    为什么换配置后必须重建图：
        build_graph() 在"编译期"就把结构化输出链建好了，而那条链里绑定了
        具体的模型实例（含 base_url / api_key）。图又是进程内单例，
        不重建的话，用户改了密钥也照样走旧实例——表现为"界面显示改了，
        实际请求还是旧的"，这是最难查的一类问题。

    说明：已经在跑的请求握着旧图对象的引用，会正常跑完，不受影响。
    """
    global _compiled_graph

    with _graph_lock:
        _compiled_graph = None


def ensure_index(force: bool = False, verbose: bool = False) -> int:
    """
    确保 Schema 向量索引存在，不存在则构建。

    为什么放在启动时而不是第一次查询时：
        首次构建索引要调用向量化接口，耗时数秒。
        如果拖到用户第一次提问时才做，那一次请求会莫名其妙地卡住，
        用户会以为服务挂了。启动时付这个成本，换取每次请求都稳定。

    参数:
        force   是否强制重建
        verbose 是否打印过程

    返回:
        索引中的文档数量
    """
    return build_index(force=force, verbose=verbose)


# ===============================================================
# 元信息
# ===============================================================


def get_table_infos() -> list[TableInfo]:
    """
    汇总所有表的元信息，供前端展示"这个系统认识哪些数据"。

    返回:
        TableInfo 列表
    """
    infos: list[TableInfo] = []

    for name in get_table_names():
        columns = [
            {"name": column_name, "description": description}
            for column_name, description in COLUMN_DESCRIPTIONS.get(name, {}).items()
        ]
        infos.append(
            TableInfo(
                name=name,
                description=TABLE_DESCRIPTIONS.get(name, ""),
                columns=columns,
            )
        )

    return infos


def get_health() -> dict[str, Any]:
    """
    收集健康检查所需的全部信息。

    说明：这里刻意不抛异常。健康检查本身就是用来"报告哪里坏了"的，
    它自己再抛异常就失去了意义，所以每一项都单独兜住。

    返回:
        健康检查结果字典
    """
    stats = get_index_stats()
    ready = stats["document_count"] == stats["expected_count"] and stats["document_count"] > 0

    return {
        "status": "ok" if ready else "degraded",
        "mock_mode": settings.llm_mock,
        "database_connected": check_connection(),
        "index_ready": ready,
        "index_document_count": stats["document_count"],
        "index_expected_count": stats["expected_count"],
    }


def get_index_stats_payload() -> dict[str, Any]:
    """
    获取索引统计，并补一个 ready 标记。

    返回:
        含 ready 字段的统计字典
    """
    stats = get_index_stats()
    stats["ready"] = (
        stats["document_count"] == stats["expected_count"] and stats["document_count"] > 0
    )
    return stats


# ===============================================================
# 运行时模型配置
# ===============================================================


def _to_llm_config_response(
    runtime: LLMRuntimeConfig, message: str = ""
) -> LLMConfigResponse:
    """
    把运行时配置快照转成对外响应模型。

    参数:
        runtime 配置快照
        message 操作结果说明，读接口传空串

    返回:
        LLMConfigResponse
    """
    return LLMConfigResponse(
        model=runtime.model,
        base_url=runtime.base_url,
        api_key_masked=runtime.masked_api_key,
        has_api_key=runtime.has_api_key,
        structured_method=runtime.structured_method,
        disable_thinking=runtime.disable_thinking,
        mock_mode=runtime.mock,
        overridden=list(runtime.overridden),
        summary=runtime.summary,
        structured_method_options=list(STRUCTURED_METHODS),
        message=message,
    )


def get_llm_config_payload() -> LLMConfigResponse:
    """
    读取"当前生效"的模型配置。

    返回:
        LLMConfigResponse，密钥已脱敏
    """
    return _to_llm_config_response(get_runtime_config())


def update_llm_config_payload(changes: dict[str, Any]) -> LLMConfigResponse:
    """
    更新运行时模型配置，并让状态图按新配置重建。

    参数:
        changes 字段名 -> 新值，只包含用户真的改过的项

    返回:
        更新后的 LLMConfigResponse

    抛出:
        ValueError 存在不支持的字段，或某个值不合法
    """
    runtime = update_runtime_config(**changes)
    # 配置变了，已编译的图里还绑着旧模型实例，必须丢掉重建
    reset_agent_graph()

    changed = "、".join(changes) if changes else "无"
    return _to_llm_config_response(runtime, message=f"已更新：{changed}（下次提问生效）")


def reset_llm_config_payload() -> LLMConfigResponse:
    """
    清空覆盖层，恢复为 .env 里的配置，并让状态图按新配置重建。

    返回:
        恢复后的 LLMConfigResponse
    """
    runtime = reset_runtime_config()
    reset_agent_graph()
    return _to_llm_config_response(runtime, message="已恢复为 .env 中的配置（下次提问生效）")


# ===============================================================
# 结果序列化
# ===============================================================


def _json_default(value: Any) -> Any:
    """
    JSON 序列化的兜底转换器。

    为什么需要：SQL 结果里可能出现 Decimal（金额字段）和 date（日期字段），
    这两类对象 json 模块不认，会直接抛 TypeError。
    在这里统一转成字符串/浮点数，避免每个字段各自处理。
    """
    # Pydantic 模型 -> 普通字典。
    # 这一条必须放在最前面：如果落到最后的 str(value) 分支，
    # 序列化出来的会是一段 "question='...' intent={...}" 的 Python repr，
    # 看着像 JSON 其实不是，前端解析时才发现类型不对
    if hasattr(value, "model_dump"):
        return value.model_dump()
    # Decimal -> 数字。用 float 而不是 str，前端拿到就能直接参与图表计算
    if hasattr(value, "as_tuple") and hasattr(value, "__float__"):
        return float(value)
    # date / datetime -> ISO 字符串
    if hasattr(value, "isoformat"):
        return value.isoformat()
    # numpy 标量等具有 item() 的对象，取出其 Python 原生值
    if hasattr(value, "item"):
        return value.item()
    return str(value)


def to_jsonable(payload: Any) -> Any:
    """
    把任意对象转成"只含 JSON 原生类型"的结构。

    为什么要绕一圈 json.dumps/loads 而不是只做类型转换：
        SQL 结果里的 Decimal、date 藏在两层列表里，
        逐层递归转换要写一堆类型判断，还容易漏。
        交给 json 模块做一次序列化再反序列化，等于借它的手做了一次
        深度遍历，代码更短也更难写错。

    参数:
        payload 待处理对象

    返回:
        可直接交给 json.dumps 或 SSE 层的数据结构
    """
    return json.loads(json.dumps(payload, ensure_ascii=False, default=_json_default))


def dumps(payload: Any) -> str:
    """
    把任意对象转成 JSON 字符串。

    参数:
        payload 待序列化对象

    返回:
        JSON 字符串。中文不转义，便于直接阅读调试
    """
    return json.dumps(payload, ensure_ascii=False, default=_json_default)


def _build_execution(state: dict[str, Any]) -> ExecutionPayload:
    """
    从状态里取出执行结果并整理成响应模型。

    参数:
        state Agent 最终状态

    返回:
        ExecutionPayload
    """
    result = state.get("query_result")

    # 查询没跑起来（比如校验一直不过、最终放弃）时，也要返回一个结构完整的
    # 空结果，而不是 None。前端就不用为"这一段会不会是 null"写额外分支
    if result is None:
        return ExecutionPayload(
            success=False,
            error=state.get("execution_error") or "未执行查询",
        )

    return ExecutionPayload(
        success=bool(result.success),
        error=result.error or "",
        row_count=result.row_count,
        elapsed_ms=float(result.elapsed_ms),
        columns=list(result.columns),
        rows=[[_json_default(value) for value in row] for row in result.rows],
    )


def _build_validation(state: dict[str, Any]) -> ValidationPayload:
    """
    整理安全校验结果。

    参数:
        state Agent 最终状态

    返回:
        ValidationPayload
    """
    sql = state.get("safe_sql") or state.get("sql") or ""

    # 重新跑一次校验只是为了拿到"识别到哪些表"这个展示字段。
    # 它是纯函数、不碰数据库，成本可以忽略
    detected = validate_sql(sql).detected_tables if sql else []

    return ValidationPayload(
        passed=bool(state.get("validation_passed")),
        errors=list(state.get("validation_errors") or []),
        warnings=[],
        applied_rules=list(state.get("applied_rules") or []),
        detected_tables=detected,
    )


def _build_chart(state: dict[str, Any], with_answer: bool) -> ChartPayload:
    """
    生成图表配置。

    为什么把画图逻辑放在接口层而不是图里：
        图表是"展示"问题不是"推理"问题，不该占用状态图节点。
        放在这里还有一个好处：前端可以选择不要结论（with_answer=false）时，
        这两次模型调用可以完全跳过，省时间也省钱。

    参数:
        state       Agent 最终状态
        with_answer 是否生成

    返回:
        ChartPayload。任何异常都不外抛，降级为 table 类型
    """
    result = state.get("query_result")

    if not with_answer or result is None or not result.success or result.row_count == 0:
        return ChartPayload()

    try:
        spec, notes = decide_chart_spec(state["question"], result, verbose=False)
    except Exception as exc:  # noqa: BLE001
        # 画不出图不能影响主流程，降级成表格并在 reason 里说明原因
        return ChartPayload(chart_type="table", reason=f"图表配置生成失败，已降级为表格：{exc}")

    return ChartPayload(
        chart_type=spec.chart_type,
        x_field=spec.x_field,
        y_field=spec.y_field,
        title=spec.title,
        reason=spec.reason,
        notes=list(notes),
    )


def _build_answer(state: dict[str, Any], with_answer: bool) -> str:
    """
    生成结论解读文本。

    参数:
        state       Agent 最终状态
        with_answer 是否生成

    返回:
        Markdown 文本；失败时返回空串
    """
    result = state.get("query_result")

    if not with_answer or result is None or not result.success:
        return ""

    try:
        return interpret_result(
            state["question"],
            result,
            sql=state.get("safe_sql", ""),
            verbose=False,
        )
    except Exception as exc:  # noqa: BLE001
        return f"（结论生成失败：{exc}）"


def build_query_response(state: dict[str, Any], with_answer: bool = True) -> QueryResponse:
    """
    把 Agent 状态整理成对外的响应对象。

    参数:
        state       Agent 最终状态
        with_answer 是否附带结论与图表配置

    返回:
        QueryResponse
    """
    execution = _build_execution(state)

    return QueryResponse(
        question=state.get("question", ""),
        intent=state.get("intent") or {},
        sql=state.get("sql") or "",
        safe_sql=state.get("safe_sql") or "",
        sql_history=list(state.get("sql_history") or []),
        candidate_tables=list(state.get("candidate_tables") or []),
        validation=_build_validation(state),
        execution=execution,
        chart=_build_chart(state, with_answer),
        answer=_build_answer(state, with_answer),
        trace=list(state.get("trace") or []),
        token_usage=dict(state.get("token_usage") or {}),
        retry_count=int(state.get("retry_count") or 0),
        succeeded=execution.success,
        mock=settings.llm_mock,
    )


# ===============================================================
# 同步执行
# ===============================================================


def run_query(question: str, with_answer: bool = True) -> QueryResponse:
    """
    跑完整条链路并返回结果（阻塞式）。

    适用场景：脚本调用、接口联调、写测试。
    前端走的是流式接口，能实时看到每一步在做什么。

    参数:
        question    用户提问
        with_answer 是否附带结论与图表配置

    返回:
        QueryResponse
    """
    graph = get_agent_graph()
    final_state = graph.invoke(create_initial_state(question))
    return build_query_response(final_state, with_answer=with_answer)


# ===============================================================
# 流式执行（SSE）
# ===============================================================

# 节点名 -> 给用户看的中文说明。
# 前端拿到节点名后能显示"正在做什么"，而不是一个转圈的图标干等
STAGE_LABELS: dict[str, str] = {
    "parse_intent": "解析问题意图",
    "retrieve_schema": "检索相关表结构",
    "generate_sql": "生成 SQL",
    "validate": "SQL 安全校验",
    "execute": "执行查询",
    "give_up": "放弃并说明原因",
}


def _stream_events(question: str, with_answer: bool, emit: Any) -> None:
    """
    在工作线程里跑完整条链路，逐个事件回调。

    为什么拆成独立函数并接收 emit 回调：
        这样它完全不感知 asyncio，既可以由 SSE 路由调用，
        也能在测试里用普通的 list.append 当回调直接跑，不必起事件循环。
        这是让"并发代码可测"的常用手法。

    参数:
        question    用户提问
        with_answer 是否附带结论与图表配置
        emit        (event, data) -> None 的事件回调
    """
    graph = get_agent_graph()
    initial_state = create_initial_state(question)

    # 最终状态以 stream_mode="values" 产出的最后一份为准。
    # 为什么不自己把 updates 累加起来：状态里的 trace/sql_history 用的是
    # operator.add 归约器，手工累加要复刻一遍归约规则，容易漏。
    # 让 LangGraph 自己算出完整状态，我们只取最后一份，最稳
    final_state: dict[str, Any] | None = None

    for mode, chunk in graph.stream(initial_state, stream_mode=["updates", "values"]):
        if mode == "values":
            final_state = chunk
            continue

        # mode == "updates"：chunk 形如 {"节点名": 该节点返回的状态片段}
        for node_name, update in chunk.items():
            emit(
                "stage",
                {
                    "node": node_name,
                    "label": STAGE_LABELS.get(node_name, node_name),
                    "status": "done",
                },
            )
            # 节点写进状态里的轨迹消息，逐条实时推给前端
            for item in (update or {}).get("trace", []) or []:
                emit("trace", item)

    if final_state is None:
        emit("error", {"message": "状态图没有产出任何结果"})
        return

    # 结论解读要走模型，耗时最长，单独发一个事件让前端知道在等什么
    if with_answer:
        emit("stage", {"node": "render", "label": "生成结论与图表", "status": "running"})

    payload = build_query_response(final_state, with_answer=with_answer)
    emit("result", to_jsonable(payload))

    if with_answer:
        emit("stage", {"node": "render", "label": "生成结论与图表", "status": "done"})


async def stream_query(question: str, with_answer: bool = True) -> AsyncIterator[dict[str, str]]:
    """
    以 SSE 事件流的形式执行问答。

    事件类型：
        stage   某个节点完成 / 开始，前端据此更新进度条
        trace   一条执行轨迹，前端追加到时间线
        result  最终结果（完整响应体）
        error   执行失败
        done    流结束标记，前端据此关闭连接并停止 loading

    参数:
        question    用户提问
        with_answer 是否附带结论与图表配置

    Yields:
        sse-starlette 需要的事件字典，形如 {"event": "trace", "data": "{...}"}
    """
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

    def emit(event: str, data: Any) -> None:
        """
        线程安全地投递一个事件。

        用 call_soon_threadsafe 而不是 queue.put_nowait：
            asyncio.Queue 不是线程安全的，从工作线程直接塞会破坏内部状态。
            必须把"放入队列"这个动作交给事件循环线程执行。
        """
        loop.call_soon_threadsafe(queue.put_nowait, {"event": event, "data": data})

    def worker() -> None:
        """工作线程主体：跑链路，异常也转成事件，最后一定投递结束哨兵。"""
        try:
            _stream_events(question, with_answer, emit)
        except Exception as exc:  # noqa: BLE001
            # 异常不往外抛，而是转成 error 事件。
            # 因为此时 HTTP 响应头已经发出去了，改不了状态码，
            # 唯一能让前端知道出事的渠道就是事件流本身
            emit("error", {"message": f"{type(exc).__name__}: {exc}"})
        finally:
            # None 是结束哨兵，保证消费端一定会退出，不会永久挂住连接
            loop.call_soon_threadsafe(queue.put_nowait, None)

    thread = threading.Thread(target=worker, name="agent-stream", daemon=True)
    thread.start()

    while True:
        item = await queue.get()
        if item is None:
            break
        # 这里必须自己序列化成 JSON 字符串。
        # sse-starlette 的 ServerSentEvent 对 data 用的是 str()，
        # 直接传字典会被写成 "{'node': 'execute'}" 这种 Python repr，
        # 前端 JSON.parse 会直接报错；传字符串则原样落到 data: 后面，是合法 JSON。
        # 又因为 json.dumps 会把换行转义成 \n 两个字符，
        # 不会出现"一条事件被拆成多行 data:"的情况
        yield {"event": item["event"], "data": dumps(item["data"])}

    # 结束哨兵也发一个事件，前端据此关闭 loading 状态并断开连接
    yield {"event": "done", "data": "{}"}
