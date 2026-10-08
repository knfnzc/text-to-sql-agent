"""
本项目的核心状态定义。

LangGraph 的思维方式：把整个处理流程想象成一条流水线，
"状态（State）"就是在这条流水线上传递的工件，每个节点（Node）拿到它、
加工一部分、再传下去。

理解状态最关键的三个点：
    1. 状态是一个字典，字段就是流水线上传递的信息
    2. 每个节点不需要更新所有字段，只需要返回"我改了哪些字段"
    3. 同名列表字段默认是"覆盖"，想改成"追加"要用 Annotated 配 reducer

第 3 点是最容易踩坑的地方：比如 sql_history 想记录每次尝试的 SQL，
如果不用 reducer，第二次生成的 SQL 会把第一次覆盖掉，历史就丢了。

运行方式：
    python -m app.agent.state
"""

import operator
from typing import Annotated, Any, TypedDict


class SQLAgentState(TypedDict, total=False):
    """
    Text-to-SQL 流程的状态定义。

    关于 total=False：
        表示这个 TypedDict 的所有字段都是"可选"的。
        为什么需要：流程开始时只有 question 一个字段有值，
        其余字段是随着节点执行逐步填充的。
        如果设成必填，初始状态就无法通过类型检查。
        LangGraph 允许节点只返回部分字段，正是基于这个特性。
    """

    # ===============================================================
    # 输入
    # ===============================================================

    # 用户原始提问，整个流程的起点
    question: str

    # ===============================================================
    # 第一步：意图解析
    # ===============================================================

    # 解析出的意图结构，包含：
    #   metric     要统计什么指标（销售额、订单数、平均价格……）
    #   dimensions 按什么维度拆分（类目、省份、月份……）
    #   filters    筛选条件（限定时间范围、渠道、会员等级……）
    #   time_range 时间范围，相对时间已换算成具体区间
    # 为什么先做意图解析：直接让模型"看到问题就写 SQL"容易漏掉隐含条件。
    # 让它先把需求拆解成结构化字段，再据此写 SQL，准确率明显更高。
    intent: dict[str, Any]

    # ===============================================================
    # 第二步：Schema 检索
    # ===============================================================

    # 向量检索召回的候选表名列表，例如 ["orders", "customers"]
    candidate_tables: list[str]

    # 由候选表拼装出的表结构文本，会直接塞进生成 SQL 的 Prompt 里。
    # 这是控制 Prompt 长度的关键：全库 Schema 可能有几千 token，
    # 只取相关表的描述通常能压到几百 token
    schema_text: str

    # ===============================================================
    # 第三步：SQL 生成
    # ===============================================================

    # 当前这一版生成的 SQL。会被后续节点检查和执行
    sql: str

    # 历史生成过的所有 SQL。
    # 用 Annotated[..., operator.add] 声明为"追加式"字段：
    #   普通字段：节点返回值会覆盖旧值
    #   带 reducer 的字段：节点返回值会被追加到旧值后面
    # 保留历史的价值：失败时能看到"模型改了几版、往错误的方向走了多远"，
    # 这是调试 Prompt 的重要线索
    sql_history: Annotated[list[str], operator.add]

    # ===============================================================
    # 第四步：安全校验
    # ===============================================================

    # 校验是否通过
    validation_passed: bool

    # 校验发现的问题列表，例如 ["检测到 DELETE 关键字", "未包含 LIMIT 子句"]
    validation_errors: list[str]

    # 本次校验实际命中的规则名，用于在界面上展示"我们拦了什么"
    applied_rules: list[str]

    # 经过校验层改写后的最终可执行 SQL（例如被自动追加了 LIMIT）
    safe_sql: str

    # ===============================================================
    # 第五步：执行
    # ===============================================================

    # 查询结果。类型标注成 Any 是因为 QueryResult 是自定义类，
    # TypedDict 里放自定义类型需要额外处理，教学项目用 Any 加注释说明更轻量
    query_result: Any

    # 执行失败时的错误信息。它会作为反馈回传给生成节点，让模型自我修正
    execution_error: str

    # ===============================================================
    # 第六步：结果解读
    # ===============================================================

    # 面向用户的自然语言结论
    final_answer: str

    # 图表配置字典，包含 chart_type（图表类型）、x_field、y_field 等。
    # 用配置字典而不是直接塞图表对象，是为了让状态保持"可序列化"，
    # 这对后续做持久化、断点续跑很重要
    chart_spec: dict[str, Any]

    # ===============================================================
    # 流程控制
    # ===============================================================

    # 已重试次数。校验失败或执行失败都会 +1
    retry_count: int

    # 当前走到了哪一步，取值如 "generate_sql"、"validate"、"execute"。
    # 用途：界面展示进度、日志定位问题
    current_step: str

    # 流程是否终止
    is_finished: bool

    # ===============================================================
    # 可观测性
    # ===============================================================

    # 执行轨迹。每个节点把自己的动作追加进来，前端按顺序渲染成时间线。
    # 这也是 Annotated + operator.add 的典型用法
    trace: Annotated[list[dict[str, Any]], operator.add]

    # token 用量统计字典：{"prompt_tokens": int, "completion_tokens": int}
    # 用普通字段（覆盖式）而不是 reducer：因为每个节点返回的是"最新总量"，
    # 由节点自己负责累加，避免多次重试时重复计数
    token_usage: dict[str, int]


# ===============================================================
# 状态工厂
# ===============================================================


def create_initial_state(question: str) -> SQLAgentState:
    """
    创建流程的初始状态。

    为什么要写工厂函数而不是在每个文件里手写初始字典：
        初始状态里哪些字段必须给默认值、哪些字段必须初始化成空列表，
        是容易出错的地方。集中一处，新加字段时不会漏改。
        特别要注意：所有用 operator.add 的列表字段都必须在初始状态里
        给一个空列表，否则第一次追加时会因 None 而报错。

    参数:
        question 用户提问

    返回:
        可以直接传给 graph.invoke() 的初始状态字典
    """
    return SQLAgentState(
        # ---- 输入 ----
        question=question,
        # ---- 意图解析：先给空结构，由解析节点填充 ----
        intent={},
        # ---- Schema 检索 ----
        candidate_tables=[],
        schema_text="",
        # ---- SQL 生成 ----
        sql="",
        # 必须是空列表，不能省略，否则 operator.add 会尝试 list + None
        sql_history=[],
        # ---- 安全校验 ----
        validation_passed=False,
        validation_errors=[],
        applied_rules=[],
        safe_sql="",
        # ---- 执行 ----
        query_result=None,
        execution_error="",
        # ---- 结果解读 ----
        final_answer="",
        chart_spec={},
        # ---- 流程控制 ----
        retry_count=0,
        current_step="start",
        is_finished=False,
        # ---- 可观测性 ----
        trace=[],
        token_usage={"prompt_tokens": 0, "completion_tokens": 0},
    )


def add_trace(
    state: SQLAgentState,
    node: str,
    message: str,
    level: str = "info",
) -> list[dict[str, Any]]:
    """
    构造一条执行轨迹记录。

    为什么返回列表而不是直接改状态：
        LangGraph 要求节点通过"返回值"更新状态，不能原地修改。
        所以这里返回一个只含 trace 的单元素列表，
        节点里用 `return {"trace": add_trace(...)}` 追加即可。

    参数:
        state   当前状态，用于记录步骤序号
        node    节点名称，如 "generate_sql"
        message 这步做了什么，写给用户看的
        level   级别：info / warning / error，前端据此决定颜色

    返回:
        只含 trace 字段的更新字典片段？不——返回的是可直接放进返回值的列表
    """
    return [
        {
            # 步骤序号从 1 开始，用已有轨迹长度 +1
            "step": len(state.get("trace", [])) + 1,
            # 哪个节点产生的
            "node": node,
            # 发生了什么
            "message": message,
            # 展示级别
            "level": level,
        }
    ]


def summarize_state(state: SQLAgentState) -> str:
    """
    生成状态摘要，用于调试和界面展示。

    参数:
        state 要摘要的状态

    返回:
        多行字符串
    """
    # get 取不到时给默认值，避免打印调试信息时反而报 KeyError
    lines = [
        f"当前步骤    : {state.get('current_step', '-')}",
        f"重试次数    : {state.get('retry_count', 0)}",
        f"候选表      : {state.get('candidate_tables', [])}",
        f"校验通过    : {state.get('validation_passed', False)}",
        f"最终 SQL    : {(state.get('safe_sql') or state.get('sql') or '-')[:120]}",
        f"执行成功    : {getattr(state.get('query_result'), 'success', None)}",
        f"是否结束    : {state.get('is_finished', False)}",
        f"轨迹条数    : {len(state.get('trace', []))}",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------
# 自测入口
# ---------------------------------------------------------------
if __name__ == "__main__":
    print("=" * 60)
    print("状态定义自检")
    print("=" * 60)

    # 1. 检查初始状态创建是否正常
    initial = create_initial_state("上个月各品类的销售额是多少？")
    print("初始状态字段：")
    for key, value in initial.items():
        # 用 repr 能看到空列表和空字符串的区别，比 str 更有信息量
        print(f"  {key:<18} = {value!r}")

    print("-" * 60)
    print("字段总数：", len(SQLAgentState.__annotations__))

    # 2. 检查 reducer 是否配置正确。
    # 判断方法：看类型注解里有没有 Annotated
    print("-" * 60)
    print("追加式字段（带 reducer，多次写入会累加而不是覆盖）：")
    for field_name, annotation in SQLAgentState.__annotations__.items():
        if "Annotated" in str(annotation):
            print(f"  {field_name}")

    # 3. 演示 trace 的追加效果
    print("-" * 60)
    print("轨迹追加演示：")
    state = create_initial_state("演示问题")
    first = add_trace(state, "parse_intent", "解析出指标：销售额")
    print("第一次返回：", first)
    # 模拟 LangGraph 的合并行为：把返回值追加到已有的 trace 后面
    state["trace"] = state["trace"] + first
    second = add_trace(state, "generate_sql", "生成 SQL 完成")
    state["trace"] = state["trace"] + second
    print("合并两次后的轨迹：")
    for item in state["trace"]:
        print("  ", item)

    print("-" * 60)
    print("状态摘要：")
    print(summarize_state(state))
