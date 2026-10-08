"""问答主接口：同步调用与 SSE 流式推送。

为什么同一个功能要提供两个接口：
    同步接口 /query 返回一次性结果，适合脚本、自动化评测、接口联调——
        调用方拿到一个完整 JSON 就好，不需要处理事件流。
    流式接口 /query/stream 把执行过程逐条推给前端——
        用户能看到"正在检索表结构""正在生成 SQL"，
        而不是盯着一个转圈图标等十几秒。同一个后端，两种消费方式。

关于 SSE 里为什么用 GET：
    浏览器的 EventSource 只支持 GET，用 GET 才能让前端几行代码接上。
    代价是参数只能放在 query string 里，所以问题文本直接作为查询参数。
    如果将来要传复杂参数（比如多轮对话历史），再补一个 POST 版本，
    用 fetch + ReadableStream 消费。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from sse_starlette.sse import EventSourceResponse

from app.api.schemas import QueryRequest, QueryResponse
from app.api.service import run_query, stream_query

router = APIRouter(tags=["问答"])


@router.post("/query", response_model=QueryResponse, summary="提问（同步返回完整结果）")
def query(payload: QueryRequest) -> QueryResponse:
    """
    执行一次完整问答并返回结果。

    适用场景：接口联调、自动化评测、写集成测试。
    界面交互请用 /query/stream，能实时看到进度。

    参数:
        payload 含 question（必填）与 with_answer（是否生成结论与图表）

    返回:
        QueryResponse，包含 SQL、校验结果、数据、图表配置与结论
    """
    question = payload.question.strip()
    if not question:
        # 虽然 Pydantic 的 min_length 已经拦了一层，
        # 但纯空格能通过长度校验，这里补上语义校验
        raise HTTPException(status_code=422, detail="问题不能为空")

    try:
        return run_query(question, with_answer=payload.with_answer)
    except Exception as exc:  # noqa: BLE001
        # Agent 内部异常（模型超时、数据库不可用等）统一转成 500 并保留原始信息。
        # 不吞掉异常堆栈，也不把它直接暴露成诡异的 200 空响应
        raise HTTPException(status_code=500, detail=f"执行失败：{exc}") from exc


@router.get("/query/stream", summary="提问（SSE 流式推送执行过程）")
async def query_stream(
    question: str = Query(..., min_length=1, max_length=500, description="自然语言问题"),
    with_answer: bool = Query(default=True, description="是否生成结论与图表配置"),
) -> EventSourceResponse:
    """
    执行问答并以 SSE 事件流的形式推送过程与结果。

    事件顺序（正常情况下）：
        stage(parse_intent)    -> trace...
        stage(retrieve_schema) -> trace...
        stage(generate_sql)    -> trace...
        stage(validate)        -> trace...
        stage(execute)         -> trace...
        stage(render, running) -> 生成结论与图表（耗时最长）
        result                 -> 完整结果 JSON
        stage(render, done)
        done                   -> 流结束

    任何一步出问题都会先发一个 error 事件，再发 done。
    也就是说前端只要等到 done，就一定能结束 loading 状态，
    不会出现"转圈转到天荒地老"的情况。
    """
    return EventSourceResponse(
        stream_query(question.strip(), with_answer=with_answer),
        # 心跳间隔：代理和浏览器会在连接长时间无数据时主动断开，
        # 定期发注释行能保活。结论生成可能要十几秒，这个设置是必要的
        ping=15,
    )
