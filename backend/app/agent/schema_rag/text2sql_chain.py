"""
基础 Text-to-SQL 链路（线性版）。

这一版的目标很明确：**先跑通"提问 -> SQL -> 结果"**，不追求健壮性。

流程只有三步，用普通函数串联：
    1. parse_intent   把自然语言拆成结构化意图
    2. generate_sql   按意图 + 相关表结构生成 SQL
    3. execute_sql    执行并返回结果

为什么刻意不做校验和重试：
    这是"演进式学习"的安排。你先看到这条链路能跑，也先看到它有多脆弱——
    SQL 写错就直接失败、没有重试、没有安全边界。
    带着这些问题去看状态图版（sql_graph.py），你才会真正理解
    "为什么这类流程要用状态图而不是一串函数"。

状态图版会解决这里的三个硬伤：
    硬伤一：出错就断，不会让模型看着报错自我修正
    硬伤二：没有安全边界，模型可能生成写操作
    硬伤三：无法复用中间结果（比如重试时不必重新解析意图）

运行方式：
    python -m app.agent.schema_rag.text2sql_chain
"""

import re
from typing import Any

from langchain_core.prompts import ChatPromptTemplate
from pydantic import BaseModel, Field

from app.core.db import QueryResult, execute_query
from app.core.llm import get_structured_runnable

from app.agent.schema_rag.schema_indexer import (
    build_index,
    retrieve_schema_text,
)


# ===============================================================
# 结构化输出的数据模型
# ===============================================================
# 这两个 Pydantic 类定义了"希望模型输出成什么样"。
# 用声明式模型而不是让模型自由输出 JSON 字符串，好处是：
#   1. 字段缺失、类型错误会被框架自动拦下，不用自己写一堆 if 判断
#   2. Field 里的 description 会作为提示词的一部分发给模型，
#      相当于把"字段说明"和"输出格式要求"写在一处
#   3. 拿到的就是对象，可以直接点属性访问


class QueryIntent(BaseModel):
    """
    用户提问的结构化意图。

    为什么要先解析意图，而不是直接让模型写 SQL：
        直接写 SQL 时，模型容易漏掉问题里的隐含条件，
        比如"上月销售额"漏掉"排除已取消订单"。
        让它先把需求拆成"指标 + 维度 + 筛选 + 时间范围"，
        相当于强制它把需求想清楚，再翻译成 SQL，准确率会明显提升。
        这也是"先想后做"的提示工程思路。
    """

    metric: str = Field(
        description=(
            "要统计的核心指标，例如：销售额、订单数、客户数、平均客单价、"
            "商品销量、退款金额。如果问题包含多个指标，用加号连接，如「销售额+订单数」"
        )
    )

    dimensions: list[str] = Field(
        default_factory=list,
        description=(
            "分析维度，即按什么分组，例如：商品类目、省份、月份、会员等级、渠道。"
            "如果问题不需要分组（例如「一共有多少客户」），返回空列表"
        ),
    )

    filters: list[str] = Field(
        default_factory=list,
        description=(
            "筛选条件，例如：订单状态为已完成、渠道为App、库存为零。"
            "注意把问题里隐含的口径要求也写进来，例如统计销售额时应排除已取消订单"
        ),
    )

    time_range: str = Field(
        default="",
        description=(
            "时间范围描述，例如：2025年全年、最近三个月、2026年第一季度。"
            "如果问题与时间无关，返回空字符串。"
            "注意：如果是相对时间，请保留原始表述（如「最近三个月」），不要自行换算成具体日期"
        ),
    )

    sort_and_limit: str = Field(
        default="",
        description=(
            "排序与限制要求，例如：按销售额降序取前10。"
            "如果问题没有要求，返回空字符串"
        ),
    )


class SQLDraft(BaseModel):
    """
    模型生成的 SQL 草稿及其自述。

    为什么要求模型同时给出解释和用到的表：
        这两个字段是"可核查的"。
        如果解释和你预期的不一致，或用的表明显不该用，
        不用执行就能判断出这条 SQL 大概率有问题。
        这是很实用的一个技巧：让模型输出"可验证的元信息"，而不只是最终答案。
    """

    sql: str = Field(
        description=(
            "完整可执行的 SQL 查询语句。要求："
            "使用标准 SQL 语法；金额字段用 ROUND 保留两位小数；"
            "聚合结果请起中文别名便于阅读；不要包含代码块的 markdown 标记"
        )
    )

    explanation: str = Field(
        description="用一到两句话说明这条 SQL 的查询思路，以及关键条件为什么这么写"
    )

    tables_used: list[str] = Field(
        default_factory=list,
        description="这条 SQL 用到的表名列表",
    )


# ===============================================================
# 提示词模板
# ===============================================================

# 意图解析提示词。
# 注意几个设计点：
#   1. 明确角色，让模型进入"需求分析师"的思维
#   2. 要求把隐含口径写进 filters，这是提升准确率的关键约束
#   3. 明确要求相对时间不要换算——因为"最近三个月"的基准应该是数据里的最大日期，
#      而不是今天的日期，交给 SQL 层处理更准确
INTENT_PROMPT = ChatPromptTemplate.from_messages(
    [
        (
            "system",
            """你是数据分析需求分析师。把用户的自然语言提问拆解成结构化的查询意图。

拆解要求：
1. 明确区分「指标」（要算什么）和「维度」（按什么分组），不要混淆
2. 把问题中隐含的业务口径也写进筛选条件。例如电商场景统计销售额时，
   通常需要排除「已取消」和「待支付」的订单
3. 时间范围保持用户的原始表述，不要自行换算成具体日期，
   因为相对时间的基准是数据中的最新日期，由下游 SQL 处理
4. 如果提问存在歧义，按最常见的业务理解处理，不要追问

用户提问：{question}""",
        ),
        ("human", "请拆解这个提问的查询意图。"),
    ]
)

# SQL 生成提示词。
# 关键设计点：
#   1. 把"相关表结构"作为变量注入，而不是写死，这样才和 Schema 检索配合起来
#   2. 要求严格使用给定的表名和字段名——模型很爱自创字段名，这条约束能显著减少错误
#   3. 要求不生成 markdown 代码块，省得后面还要写正则去剥
SQL_PROMPT = ChatPromptTemplate.from_messages(
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
4. 相对时间（如「最近三个月」）请基于数据中的最大日期计算，
   不要用 CURRENT_DATE 或 NOW()，因为数据是历史快照
5. 金额一律用 ROUND(..., 2) 保留两位小数，聚合结果用中文别名
6. 只生成 SELECT 查询，不要生成任何修改数据的语句
7. 直接输出 SQL，不要包裹在 ```sql 代码块里""",
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


def strip_code_fence(text: str) -> str:
    """
    去掉模型输出里可能带的 markdown 代码块标记。

    为什么需要这个函数：
        即使提示词里明确要求不要加代码块，模型仍有一定概率加上。
        这在生产环境很常见——提示词是"软约束"，代码层面的兜底清洗才是"硬保证"。
        这类"防御式解析"是 LLM 应用开发的日常。

    参数:
        text 模型输出的原始文本

    返回:
        清洗后的纯 SQL 文本
    """
    cleaned = text.strip()

    # 匹配形如 ```sql ... ``` 或 ``` ... ``` 的包裹形式
    fence_pattern = re.compile(r"^```(?:sql|SQL)?\s*(.*?)\s*```$", re.DOTALL)
    match = fence_pattern.match(cleaned)
    if match:
        cleaned = match.group(1)

    # 去掉行尾可能残留的分号后再补一个，避免出现 ";;"
    cleaned = cleaned.rstrip().rstrip(";").strip()

    return cleaned


def looks_like_select(sql: str) -> bool:
    """
    粗略判断 SQL 是否是查询语句。

    说明：这是一个临时保护，不是完整的校验。
        状态图版会用 sqlparse 做真正的语法级解析，能识别注释绕过、
        多语句拼接、CTE 里的写操作等更隐蔽的情况。
        这里先挡住最明显的风险，避免在学习过程中误执行破坏性语句。

    参数:
        sql 待判断的 SQL

    返回:
        是查询语句返回 True
    """
    # 去掉前导注释和空白后，看开头是不是 SELECT 或 WITH（CTE）
    normalized = re.sub(r"^\s*(--[^\n]*\n|/\*.*?\*/)*", "", sql, flags=re.DOTALL).strip()
    return normalized.upper().startswith(("SELECT", "WITH"))


# ===============================================================
# 三个处理步骤
# ===============================================================


def parse_intent(question: str, verbose: bool = True) -> QueryIntent:
    """
    步骤一：把自然语言提问解析成结构化意图。

    参数:
        question 用户提问
        verbose  是否打印中间结果

    返回:
        QueryIntent 对象

    技术说明：
        这里用 with_structured_output 让模型按 Pydantic 模型输出。
        LangChain v1 里另一种写法是用 create_agent(..., response_format=ToolStrategy(QueryIntent))，
        适合需要"边调工具边产出结构化结果"的复杂场景。
        这里只需要一次转换，用 with_structured_output 更直接。
        注意温度必须为 0，否则同样的提问每次解析出的意图都会变，无法复现问题。
    """
    # 把模型包装成"输出指定结构"的形式，实现方式由配置决定（见 core/llm.py）
    structured_llm = get_structured_runnable(QueryIntent, temperature=0)

    # 把提示词和模型串成一条链（用 | 管道符，LangChain 表达式语言的写法）
    chain = INTENT_PROMPT | structured_llm

    intent = chain.invoke({"question": question})

    if verbose:
        print("  [意图解析]")
        print(f"    指标    : {intent.metric}")
        print(f"    维度    : {intent.dimensions or '（无）'}")
        print(f"    筛选    : {intent.filters or '（无）'}")
        print(f"    时间范围: {intent.time_range or '（无）'}")
        print(f"    排序限制: {intent.sort_and_limit or '（无）'}")

    return intent


def generate_sql(
    question: str,
    intent: QueryIntent,
    schema_text: str,
    verbose: bool = True,
) -> SQLDraft:
    """
    步骤二：结合意图和相关表结构生成 SQL。

    参数:
        question    用户原始提问
        intent      已解析的意图
        schema_text 由 Schema 检索得到的表结构文本
        verbose     是否打印中间结果

    返回:
        SQLDraft 对象
    """
    structured_llm = get_structured_runnable(SQLDraft, temperature=0)
    chain = SQL_PROMPT | structured_llm

    draft = chain.invoke(
        {
            "schema_text": schema_text,
            "question": question,
            # 意图里的列表要转成文本才能塞进提示词
            "metric": intent.metric,
            "dimensions": "、".join(intent.dimensions) if intent.dimensions else "无",
            "filters": "、".join(intent.filters) if intent.filters else "无",
            "time_range": intent.time_range or "无",
            "sort_and_limit": intent.sort_and_limit or "无",
        }
    )

    # 兜底清洗，防止模型加了代码块标记
    draft.sql = strip_code_fence(draft.sql)

    if verbose:
        print("  [SQL 生成]")
        print(f"    思路    : {draft.explanation}")
        print(f"    用到的表: {draft.tables_used}")
        print(f"    SQL     :")
        for line in draft.sql.splitlines():
            print(f"      {line}")

    return draft


def execute_sql(sql: str, verbose: bool = True) -> QueryResult:
    """
    步骤三：执行 SQL 并返回结果。

    参数:
        sql     要执行的 SQL
        verbose 是否打印执行信息

    返回:
        QueryResult 对象

    安全性说明：
        这里做了最基础的 SELECT 检查。听起来很低级，但这是必要的——
        模型确实会在用户说"把测试数据清掉"时生成 DELETE 语句，
        而你在学习过程中很可能随手输入各种问题测试。
        完整的校验层在状态图版里，包括语法解析、危险关键字拦截、行数限制等。
    """
    # 最低限度的安全保护
    if not looks_like_select(sql):
        result = QueryResult(
            executed_sql=sql,
            success=False,
            error="出于安全考虑，基础链路只允许执行 SELECT 查询（完整校验见状态图版）",
        )
        if verbose:
            print(f"  [执行] 已拦截：{result.error}")
        return result

    if verbose:
        print("  [执行] 正在查询数据库...")

    result = execute_query(sql)

    if verbose:
        if result.success:
            print(f"    成功，返回 {result.row_count} 行，耗时 {result.elapsed_ms:.1f} ms")
        else:
            # 注意：这里的错误信息是宝贵的素材。
            # 状态图版会把这段文本回传给模型，让它据此修正 SQL——这就是"自我修正"的核心
            print(f"    失败：{result.error}")

    return result


# ===============================================================
# 串联成完整链路
# ===============================================================


def answer_question(question: str, verbose: bool = True) -> dict[str, Any]:
    """
    完整链路：提问 -> 意图 -> 检索 -> SQL -> 结果。

    参数:
        question 用户提问
        verbose  是否打印过程

    返回:
        包含 intent、schema_tables、sql、result 的字典
    """
    if verbose:
        print("=" * 78)
        print(f"提问：{question}")
        print("=" * 78)

    # 第一步：解析意图
    intent = parse_intent(question, verbose=verbose)

    # 第二步：检索相关表结构
    tables, schema_text = retrieve_schema_text(question)
    if verbose:
        print(f"  [Schema 检索] 命中表：{tables}")

    # 第三步：生成 SQL
    draft = generate_sql(question, intent, schema_text, verbose=verbose)

    # 第四步：执行
    result = execute_sql(draft.sql, verbose=verbose)

    if verbose:
        print("  [结果]")
        print(result.preview(max_rows=8))
        print()

    return {
        "question": question,
        "intent": intent,
        "schema_tables": tables,
        "sql": draft.sql,
        "explanation": draft.explanation,
        "result": result,
    }


# ---------------------------------------------------------------
# 自测入口
# ---------------------------------------------------------------
if __name__ == "__main__":
    # 确保索引存在（不存在会自动建，已存在会跳过）
    build_index(force=False, verbose=False)

    print("=" * 78)
    print("基础 Text-to-SQL 链路演示（线性版，无校验、无重试）")
    print("=" * 78)
    print()

    # 先跑简单题确认链路通畅，再跑困难题看它在哪里出问题
    demo_questions = [
        "一共有多少个客户？",
        "销售额最高的 5 个订单分别是哪个客户下的？",
        "各商品类目的总销售额是多少？",
        "最近三个月每个月的销售额是多少？",
    ]

    results: list[dict[str, Any]] = []
    for question in demo_questions:
        results.append(answer_question(question, verbose=True))

    # 汇总本次运行的成败情况
    print("=" * 78)
    print("本次运行汇总")
    print("=" * 78)
    success_count = sum(1 for r in results if r["result"].success)
    print(f"共 {len(results)} 个问题，执行成功 {success_count} 个")
    for r in results:
        flag = "成功" if r["result"].success else "失败"
        print(f"  [{flag}] {r['question']}")
        if not r["result"].success:
            print(f"         原因：{r['result'].error[:100]}")

    # 特意演示一下这条链路的脆弱之处，为状态图版做铺垫
    print()
    print("=" * 78)
    print("脆弱性演示：让它做一件危险的事")
    print("=" * 78)
    print("说明：这里不会真的执行，只是展示基础链路缺乏安全边界。")
    print("      完整的校验层会在状态图版里构建。")
    print()
    danger_demo_sql = "DELETE FROM orders WHERE status = '已取消'"
    print(f"假设模型生成了这样的 SQL：{danger_demo_sql}")
    blocked = execute_sql(danger_demo_sql, verbose=True)
    print(f"拦截结果：{'已拦截' if not blocked.success else '未拦截（危险）'}")

    print()
    print("-" * 78)
    print("本模块到此完成。这两个硬伤会在状态图版解决：")
    print("  1. SQL 执行失败时不会自我修正，直接断掉")
    print("  2. 缺少完整的 SQL 安全校验层")
    print("下一步：python -m app.agent.safety.sql_graph")
