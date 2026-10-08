"""
用 LangGraph 状态图重构整条链路。

这个文件回答了本项目最核心的一个问题：
    为什么要用状态图，而不是一串函数调用？

对比基础链路版的函数实现（text2sql_chain.py），这里加了两个条件分支：
    分支一：SQL 校验不通过 -> 带着"被拦截的原因"回到生成节点重写
    分支二：SQL 执行报错   -> 带着"数据库的报错原文"回到生成节点重写

用函数实现这两个分支会很难看：
    得写嵌套的 while 循环 + 状态变量 + 各种 break 条件，
    而且重试一次和重试两次的逻辑要复制一遍，中间结果也没地方存。
状态图的优势正在这里：
    "回到哪一步"变成了一条边，"还能重试几次"变成了状态里的一个字段，
    流程本身是声明式的、看得见的。

图的结构（对照理解）：
                    +-------------+
        START ----> | parse_intent|   解析意图
                    +-------------+
                           |
                    +-----------------+
                    | retrieve_schema |   检索相关表
                    +-----------------+
                           |
                    +--------------+  <---------------------------+
                    | generate_sql |                             |
                    +--------------+                             |
                           |                                     |
                    +----------+                                 |
                    | validate |   安全校验                       |
                    +----------+                                 |
                      |        |                                 |
              通过 ---+        +--- 不通过且还有重试次数 ---------+
                      |
                    +---------+
                    | execute |   执行查询
                    +---------+
                      |       |
              成功 ---+       +--- 失败且还有重试次数 --------------+
                      |
                     END

运行方式：
    python -m app.agent.safety.sql_graph
"""

from typing import Any

from langchain_core.prompts import ChatPromptTemplate
from langgraph.graph import END, START, StateGraph

from app.core.config import settings
from app.core.db import QueryResult, execute_query
from app.core.llm import get_structured_runnable

from app.agent.schema_rag.schema_indexer import (
    build_index,
    retrieve_schema_text,
)
from app.agent.schema_rag.text2sql_chain import (
    INTENT_PROMPT,
    QueryIntent,
    SQLDraft,
    strip_code_fence,
)
from app.agent.safety.sql_guard import validate_sql
from app.agent.state import SQLAgentState, add_trace, create_initial_state


# ===============================================================
# 带"失败反馈"的 SQL 生成提示词
# ===============================================================
# 这是整个"自我修正"机制的关键。相比基础链路版的提示词，多了 {error_feedback} 段：
# 第一次生成时它是空的，重试时填入上一轮失败的具体原因。
#
# 为什么这样做有效：
#   模型不知道自己的 SQL 为什么错了，除非你告诉它。
#   把数据库的原始报错（比如 "no such column: order_amount"）原样回传，
#   它就非常明确地知道要改什么，这比笼统地说"请再试一次"有效得多。
SQL_PROMPT_WITH_FEEDBACK = ChatPromptTemplate.from_messages(
    [
        (
            "system",
            """你是资深数据分析师，擅长把业务问题翻译成 SQL 查询。

数据库表结构如下：
{schema_text}

生成 SQL 时必须遵守：
1. 只使用上面列出的表和字段名，绝对不要自创字段名
2. 涉及多表关联时，严格按上面给出的关联关系写 JOIN 条件
3. 统计销售额、销量时，排除订单状态为「已取消」和「待支付」的订单
4. 相对时间（如「最近三个月」）请基于数据中的最大日期计算，不要用 CURRENT_DATE
5. 金额一律用 ROUND(..., 2) 保留两位小数，聚合结果用中文别名
6. 只生成 SELECT 查询，不要生成任何修改数据的语句
7. 不要给表起没有意义的别名，关联时用完整表名或清晰的短别名
8. 直接输出 SQL，不要包裹在 ```sql 代码块里
{error_feedback}""",
        ),
        (
            "human",
            """用户的原始提问：{question}

已解析的查询意图：
- 核心指标：{metric}
- 分析维度：{dimensions}
- 筛选条件：{filters}
- 时间范围：{time_range}
- 排序限制：{sort_and_limit}

请生成对应的 SQL。""",
        ),
    ]
)


# ===============================================================
# 工具函数
# ===============================================================


def _structured_call(
    prompt: ChatPromptTemplate,
    schema: type,
    payload: dict[str, Any],
    temperature: float = 0.0,
) -> tuple[Any, dict[str, int]]:
    """
    调用模型并获取结构化输出，同时返回本次的 token 用量。

    为什么要自己包一层：
        with_structured_output 默认只返回解析后的对象，拿不到 token 用量。
        加上 include_raw=True 后会额外返回原始消息对象，从里面才能读到用量。
        用量统计是成本控制的前提，所以这层封装很有必要。

    参数:
        prompt      提示词模板
        schema      期望输出的 Pydantic 模型类
        payload     提示词变量
        temperature 采样温度

    返回:
        (解析后的对象, {"prompt_tokens": int, "completion_tokens": int})
    """
    # 结构化输出的实现方式由配置决定（各服务商支持情况不同），
    # 已收口在 get_structured_runnable 里，这里不再直接调 with_structured_output
    chain = prompt | get_structured_runnable(schema, temperature=temperature)

    outcome = chain.invoke(payload)

    # 兼容处理：不同版本返回的结构可能不同。
    # 正常情况下是 {"raw": AIMessage, "parsed": 对象, "parsing_error": None}
    # 如果框架忽略了 include_raw，就直接返回解析后的对象
    if isinstance(outcome, dict) and "parsed" in outcome:
        parsing_error = outcome.get("parsing_error")
        if parsing_error:
            # 结构化输出解析失败，说明模型没按要求的格式返回。
            # 这里抛出明确的异常，由上层流程处理
            raise ValueError(f"模型输出未按预期结构返回：{parsing_error}")

        parsed = outcome["parsed"]
        raw_message = outcome.get("raw")

        # 从原始消息里提取 token 用量
        usage: dict[str, int] = {"prompt_tokens": 0, "completion_tokens": 0}
        if raw_message is not None:
            metadata = getattr(raw_message, "usage_metadata", None) or {}
            usage["prompt_tokens"] = int(
                metadata.get("input_tokens") or metadata.get("prompt_tokens") or 0
            )
            usage["completion_tokens"] = int(
                metadata.get("output_tokens") or metadata.get("completion_tokens") or 0
            )
        return parsed, usage

    # 兜底路径：直接拿到了对象，用量无法统计，返回 0
    return outcome, {"prompt_tokens": 0, "completion_tokens": 0}


def _accumulate_usage(state: SQLAgentState, usage: dict[str, int]) -> dict[str, int]:
    """
    把本次调用的用量累加到状态里。

    为什么用覆盖式的普通字段而不是 reducer：
        用量是"累计总量"的语义。如果每个节点都返回"本次增量"并让 reducer 累加，
        那么同一个节点被重试多次时会重复计数，总数就不准了。
        由节点自己读取旧值、加上增量、返回新总量，语义更清晰。

    参数:
        state 当前状态
        usage 本次调用的用量

    返回:
        累加后的用量字典，作为节点返回值的一部分
    """
    previous = state.get("token_usage") or {}
    return {
        "prompt_tokens": int(previous.get("prompt_tokens", 0)) + usage.get("prompt_tokens", 0),
        "completion_tokens": int(previous.get("completion_tokens", 0))
        + usage.get("completion_tokens", 0),
    }


def _build_error_feedback(state: SQLAgentState) -> str:
    """
    根据上一轮失败原因，拼出给模型的反馈文本。

    这是自我修正机制的核心输入。要求写得具体（含原始报错），
    而不是笼统的"请重试"。

    参数:
        state 当前状态

    返回:
        反馈文本；没有失败历史时返回空字符串
    """
    validation_errors = state.get("validation_errors") or []
    execution_error = state.get("execution_error") or ""
    previous_sql = state.get("sql") or ""

    # 没有任何失败信息，说明这是第一次生成
    if not validation_errors and not execution_error:
        return ""

    lines = ["", "=" * 40, "重要：你上一次生成的 SQL 未能通过，请针对以下问题修正："]

    if previous_sql:
        lines.append(f"上一次的 SQL：{previous_sql}")

    if validation_errors:
        lines.append("安全校验未通过，原因：")
        for error in validation_errors:
            lines.append(f"  - {error}")

    if execution_error:
        lines.append(f"数据库执行报错原文：{execution_error}")
        lines.append("请仔细阅读报错信息，常见原因包括：字段名拼写错误、")
        lines.append("表关联条件缺失导致字段歧义、聚合函数使用位置不当、")
        lines.append("日期格式处理不当。请据此修正。")

    lines.append("=" * 40)

    return "\n".join(lines)


# ===============================================================
# 节点函数
# ===============================================================
# LangGraph 节点的签名约定：
#   入参：完整的状态对象
#   返回：一个字典，只包含"这个节点修改了的字段"
# 不需要返回全部状态，框架会自动把返回值合并进状态


def node_parse_intent(state: SQLAgentState) -> dict[str, Any]:
    """
    节点一：解析用户提问的意图。

    这个节点只在流程开始时执行一次。为什么要单独成节点而不是塞进生成节点：
        重试时我们希望复用已解析的意图，不必重新调用模型解析一遍。
        拆开之后，重试路径直接从 generate_sql 开始，省一次模型调用。
        这就是"节点粒度设计"——粒度太粗会导致重试代价高。

    参数:
        state 当前状态

    返回:
        含 intent、current_step、trace、token_usage 的更新字典
    """
    question = state["question"]

    intent, usage = _structured_call(INTENT_PROMPT, QueryIntent, {"question": question})

    # 把解析结果转成普通字典存进状态。
    # 用 model_dump() 而不是直接存对象，是为了保持状态可序列化——
    # 这对将来做检查点持久化、把状态存进 Redis 是必须的
    return {
        "intent": intent.model_dump(),
        "current_step": "parse_intent",
        "token_usage": _accumulate_usage(state, usage),
        "trace": add_trace(
            state,
            "parse_intent",
            f"解析意图：指标={intent.metric}，维度={intent.dimensions or '无'}，"
            f"时间范围={intent.time_range or '无'}",
        ),
    }


def node_retrieve_schema(state: SQLAgentState) -> dict[str, Any]:
    """
    节点二：检索与问题相关的表结构。

    参数:
        state 当前状态

    返回:
        含 candidate_tables、schema_text 的更新字典
    """
    question = state["question"]

    tables, schema_text = retrieve_schema_text(question)

    return {
        "candidate_tables": tables,
        "schema_text": schema_text,
        "current_step": "retrieve_schema",
        "trace": add_trace(
            state,
            "retrieve_schema",
            f"Schema 检索命中 {len(tables)} 张表：{', '.join(tables)}",
        ),
    }


def node_generate_sql(state: SQLAgentState) -> dict[str, Any]:
    """
    节点三：生成 SQL。

    这个节点是重试的"落点"——校验失败或执行失败都会回到这里。
    它会读取状态里的失败信息，拼进提示词，让模型带着反馈重写。

    参数:
        state 当前状态

    返回:
        含 sql、sql_history、重试计数与错误信息清空的更新字典
    """
    question = state["question"]
    intent = state.get("intent") or {}
    error_feedback = _build_error_feedback(state)

    # 重试次数在这里 +1。
    # 放在生成节点而不是校验节点，是因为"重试"这个动作本质上是"再生成一次"
    retry_count = int(state.get("retry_count", 0))
    is_retry = bool(error_feedback)
    if is_retry:
        retry_count += 1

    draft, usage = _structured_call(
        SQL_PROMPT_WITH_FEEDBACK,
        SQLDraft,
        {
            "schema_text": state.get("schema_text", ""),
            "question": question,
            "metric": intent.get("metric", ""),
            "dimensions": "、".join(intent.get("dimensions") or []) or "无",
            "filters": "、".join(intent.get("filters") or []) or "无",
            "time_range": intent.get("time_range") or "无",
            "sort_and_limit": intent.get("sort_and_limit") or "无",
            "error_feedback": error_feedback,
        },
    )

    sql = strip_code_fence(draft.sql)

    return {
        "sql": sql,
        # sql_history 是带 reducer 的字段，返回值会追加到列表末尾而不是覆盖
        "sql_history": [sql],
        "retry_count": retry_count,
        # 关键：清空上一轮的错误信息。
        # 不清空的话，下一轮如果成功，旧的错误还会留在状态里造成误导
        "validation_errors": [],
        "execution_error": "",
        "current_step": "generate_sql",
        "token_usage": _accumulate_usage(state, usage),
        "trace": add_trace(
            state,
            "generate_sql",
            f"{'第 ' + str(retry_count) + ' 次重试' if is_retry else '首次'}生成 SQL："
            f"{sql[:80]}{'...' if len(sql) > 80 else ''}",
            level="warning" if is_retry else "info",
        ),
    }


def node_validate(state: SQLAgentState) -> dict[str, Any]:
    """
    节点四：安全校验。

    这一步不调用模型，纯代码逻辑，所以又快又稳。
    "把不依赖模型的部分做成确定性代码"是 LLM 应用工程化的重要原则：
    能不用模型就不用，既省成本又提高可靠性。

    参数:
        state 当前状态

    返回:
        含 validation_passed、validation_errors、safe_sql 等的更新字典
    """
    sql = state.get("sql", "")

    guard_result = validate_sql(sql)

    return {
        "validation_passed": guard_result.passed,
        "validation_errors": guard_result.errors,
        "applied_rules": guard_result.applied_rules,
        "safe_sql": guard_result.safe_sql,
        "current_step": "validate",
        "trace": add_trace(
            state,
            "validate",
            (
                f"安全校验通过，命中规则 {len(guard_result.applied_rules)} 条"
                if guard_result.passed
                else f"安全校验拦截：{guard_result.errors[0]}"
            ),
            level="info" if guard_result.passed else "error",
        ),
    }


def node_execute(state: SQLAgentState) -> dict[str, Any]:
    """
    节点五：执行 SQL。

    参数:
        state 当前状态

    返回:
        含 query_result、execution_error 的更新字典
    """
    # 优先用校验层改写后的 SQL（它带上了强制 LIMIT），
    # 拿不到才回退到原始 SQL
    sql = state.get("safe_sql") or state.get("sql", "")

    result: QueryResult = execute_query(sql)

    return {
        "query_result": result,
        "execution_error": "" if result.success else result.error,
        "current_step": "execute",
        "is_finished": result.success,
        "trace": add_trace(
            state,
            "execute",
            (
                f"执行成功，返回 {result.row_count} 行，耗时 {result.elapsed_ms:.1f} ms"
                if result.success
                else f"执行失败：{result.error[:120]}"
            ),
            level="info" if result.success else "error",
        ),
    }


def node_give_up(state: SQLAgentState) -> dict[str, Any]:
    """
    终止节点：重试次数用尽，放弃并生成一份说明。

    为什么需要这个节点而不是直接结束：
        用户需要知道"为什么没拿到结果"，而不是面对一个空白页面。
        返回一个明确的失败说明，是产品体验的一部分。

    参数:
        state 当前状态

    返回:
        含 final_answer、is_finished 的更新字典
    """
    errors = state.get("validation_errors") or []
    execution_error = state.get("execution_error") or ""
    attempts = len(state.get("sql_history") or [])

    reason = errors[0] if errors else (execution_error or "未知原因")

    answer = (
        f"抱歉，经过 {attempts} 次尝试仍未能生成可执行的查询。\n"
        f"最后一次的失败原因：{reason}\n\n"
        "建议：把问题描述得更具体一些，例如明确时间范围、指定分析维度，"
        "或换一种说法重新提问。"
    )

    return {
        "final_answer": answer,
        "is_finished": True,
        "current_step": "give_up",
        "trace": add_trace(
            state,
            "give_up",
            f"重试 {attempts} 次后仍未成功，终止流程",
            level="error",
        ),
    }


# ===============================================================
# 条件路由函数
# ===============================================================
# 条件边需要一个"路由函数"：接收状态，返回一个字符串键，
# 框架再通过映射表找到下一个节点。
# 路由函数必须是纯函数式的——只读状态、做判断，不产生副作用


def route_after_validate(state: SQLAgentState) -> str:
    """
    校验后的路由决策。

    三种去向：
        execute  校验通过，去执行
        retry    校验不通过但还有重试机会，回去重新生成
        give_up  重试用尽，走终止节点

    参数:
        state 当前状态

    返回:
        字符串键："execute" / "retry" / "give_up"

    注意这里的判断顺序：
        先看通过没通过，再看还有没有重试机会。
        反过来写会出现"校验通过了却因为重试次数用尽而放弃"的荒谬结果。
    """
    if state.get("validation_passed"):
        return "execute"

    # 已经尝试的次数 = 历史 SQL 条数（每生成一次就记录一条）
    attempts = len(state.get("sql_history") or [])
    if attempts < settings.sql_max_retries:
        return "retry"

    return "give_up"


def route_after_execute(state: SQLAgentState) -> str:
    """
    执行后的路由决策。

    两种去向：
        finished 执行成功，结束
        retry    执行失败但还有重试机会，回去重新生成
        give_up  重试用尽

    参数:
        state 当前状态

    返回:
        字符串键："finished" / "retry" / "give_up"
    """
    # query_result 的 success 属性表示执行是否成功
    query_result = state.get("query_result")
    if query_result is not None and getattr(query_result, "success", False):
        return "finished"

    attempts = len(state.get("sql_history") or [])
    if attempts < settings.sql_max_retries:
        return "retry"

    return "give_up"


# ===============================================================
# 构建状态图
# ===============================================================


def build_graph():
    """
    构建并编译状态图。

    返回:
        已编译的图对象，可以直接调用 .invoke(state) 执行

    三步走：
        1. 用状态类型创建 builder
        2. add_node 注册节点，add_edge / add_conditional_edges 连接节点
        3. compile() 编译成可执行对象（编译时会做结构检查，
           比如有没有孤立节点、有没有指向不存在的节点）
    """
    # 第一步：创建图构建器，传入状态类型。
    # 这个类型决定了状态里有哪些字段、哪些字段有 reducer
    builder = StateGraph(SQLAgentState)

    # 第二步：注册节点。
    # 第一个参数是节点名（在边和路由映射里引用），第二个是处理函数
    builder.add_node("parse_intent", node_parse_intent)
    builder.add_node("retrieve_schema", node_retrieve_schema)
    builder.add_node("generate_sql", node_generate_sql)
    builder.add_node("validate", node_validate)
    builder.add_node("execute", node_execute)
    builder.add_node("give_up", node_give_up)

    # 第三步：连接边。
    # START 和 END 是 LangGraph 内置的虚拟节点，代表入口和出口
    builder.add_edge(START, "parse_intent")

    # 直线流程：意图解析 -> 检索 -> 生成
    builder.add_edge("parse_intent", "retrieve_schema")
    builder.add_edge("retrieve_schema", "generate_sql")

    # 生成后一定要校验
    builder.add_edge("generate_sql", "validate")

    # 条件分支：校验后的三种去向。
    # 第三个参数是映射表，把路由函数的返回值翻译成目标节点名
    builder.add_conditional_edges(
        "validate",
        route_after_validate,
        {
            "execute": "execute",
            # 注意这一条：回到 generate_sql，形成闭环。
            # 这就是状态图相比函数链的核心优势——"回头"是一件很自然的事
            "retry": "generate_sql",
            "give_up": "give_up",
        },
    )

    # 条件分支：执行后的三种去向
    builder.add_conditional_edges(
        "execute",
        route_after_execute,
        {
            "finished": END,
            "retry": "generate_sql",
            "give_up": "give_up",
        },
    )

    # 终止节点走完就结束
    builder.add_edge("give_up", END)

    # 编译。这一步会做静态检查，能提前发现连错的边
    return builder.compile()


# ===============================================================
# 对外调用接口
# ===============================================================


def ask(question: str, graph=None, verbose: bool = True) -> SQLAgentState:
    """
    对外的主入口：问一个问题，返回最终状态。

    参数:
        question 用户提问
        graph    已编译的图。传 None 会现场构建一次；
                 批量跑问题时建议在外部构建好传进来复用
        verbose  是否打印执行轨迹

    返回:
        流程结束后的完整状态
    """
    # 图对象可以复用，避免每次提问都重新构建
    compiled = graph if graph is not None else build_graph()

    # 初始状态只填 question，其余字段由工厂函数给好默认值
    initial_state = create_initial_state(question)

    # invoke 会同步执行整张图，返回最终状态。
    # 状态是累加合并出来的，所以最终状态里能看到所有中间结果
    final_state = compiled.invoke(initial_state)

    if verbose:
        print("=" * 78)
        print(f"提问：{question}")
        print("=" * 78)

        # 打印执行轨迹，这是状态图最直观的调试手段
        print("执行轨迹：")
        for item in final_state.get("trace", []):
            # 按级别加不同前缀，错误一眼可见
            marker = {"info": "  ·", "warning": "  !", "error": "  x"}.get(
                item.get("level", "info"), "  ·"
            )
            print(f"{marker} [{item['node']}] {item['message']}")

        # 打印最终 SQL 和结果
        print()
        print(f"最终 SQL：")
        for line in (final_state.get("safe_sql") or final_state.get("sql") or "").splitlines():
            print(f"    {line}")

        print()
        query_result = final_state.get("query_result")
        if query_result is not None:
            print(query_result.preview(max_rows=8))

        # 打印用量，培养成本意识
        usage = final_state.get("token_usage") or {}
        total = usage.get("prompt_tokens", 0) + usage.get("completion_tokens", 0)
        print()
        print(
            f"模型调用 {len(final_state.get('sql_history', []))} 次生成，"
            f"token 用量：输入 {usage.get('prompt_tokens', 0)} + "
            f"输出 {usage.get('completion_tokens', 0)} = {total}"
        )
        print()

    return final_state


# ---------------------------------------------------------------
# 自测入口
# ---------------------------------------------------------------
if __name__ == "__main__":
    # 确保索引存在
    build_index(force=False, verbose=False)

    # 图构建一次，重复使用。这比每次提问都重新编译要快
    graph = build_graph()

    print("#" * 78)
    print("# 状态图版 Text-to-SQL：带安全校验与失败自动修正")
    print("#" * 78)
    print(f"# 最大尝试次数：{settings.sql_max_retries}　行数上限：{settings.sql_row_limit}")
    print("#" * 78)

    demo_questions = [
        "一共有多少个客户？",
        "各商品类目的总销售额是多少？",
        "最近三个月每个月的销售额是多少？",
    ]

    final_states: list[SQLAgentState] = []
    for question in demo_questions:
        final_states.append(ask(question, graph=graph, verbose=True))

    # 汇总
    print("#" * 78)
    print("# 汇总")
    print("#" * 78)
    success = sum(
        1
        for s in final_states
        if getattr(s.get("query_result"), "success", False)
    )
    print(f"# 共 {len(final_states)} 个问题，成功 {success} 个")

    total_tokens = 0
    for state in final_states:
        usage = state.get("token_usage") or {}
        total_tokens += usage.get("prompt_tokens", 0) + usage.get("completion_tokens", 0)
        attempts = len(state.get("sql_history") or [])
        ok = getattr(state.get("query_result"), "success", False)
        print(f"#   [{'成功' if ok else '失败'}] 尝试 {attempts} 次 | {state['question']}")
    print(f"# 本次运行总 token 用量：{total_tokens}")

    # 演示自我修正：构造一个必然出错的问题，看它会不会重试
    print()
    print("#" * 78)
    print("# 自我修正机制演示")
    print("#" * 78)
    print("# 问一个需要跳步的问题：先要求统计不存在字段，观察重试行为")
    print("# 说明：这里通过故意问一个含糊问题，观察轨迹里是否出现多次 generate_sql")
    print()
    tricky = "帮我按客户年龄区间统计一下消费能力分布"
    tricky_state = ask(tricky, graph=graph, verbose=True)

    attempts = len(tricky_state.get("sql_history") or [])
    if attempts > 1:
        print(f"结论：该问题尝试了 {attempts} 次，重试机制生效。")
        print("     可以在上面的轨迹里看到每次重试前都带上了具体的失败原因。")
    else:
        print(f"结论：该问题一次通过（尝试 {attempts} 次）。")
        print("     想观察重试行为，可以查看 sql_history 字段或人为把校验规则改严。")

    print()
    print("-" * 78)
    print("本模块到此完成。下一步做结果解读与可视化：")
    print("  python -m app.agent.output.renderer")
