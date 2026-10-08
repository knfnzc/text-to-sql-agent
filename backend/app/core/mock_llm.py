"""
离线模拟层（Mock Layer）。

为什么这个文件存在，以及它为什么很重要：

    你现在的环境还没配 API Key。如果所有节点都强依赖真实模型，
    你就只能"读代码"而没法"跑代码"——而 Agent 开发恰恰是必须跑起来
    才能理解的东西（状态怎么流转、反馈循环怎么回退、轨迹长什么样）。

    所以这里提供一套"离线替身"：
        MockChatModel    —— 冒充对话模型，不联网，返回结构合法的假数据
        MockEmbeddings   —— 冒充向量化模型，用本地哈希生成向量（相似文本向量也相似）

    它们的产出全部带 MOCK 标记，绝不会被误当成真实结论。
    只要在 .env 里设置 LLM_MOCK=true，整条链路就能在零成本、零联网的情况下跑通。

工程视角：这其实是"测试替身（Test Double）"的标准做法。
    面试时可以这么讲：
        "我把模型层抽象成接口，业务代码只依赖接口不依赖具体实现，
         所以可以用 Mock 实现做端到端测试，不需要真的调模型，
         这让 CI 里能跑完整的图流程测试，单次只要几秒。"
    这是一个很加分的答案——说明你懂可测试性设计，而不是只会拼 API。

使用方式：
    # 方式一：改 .env（推荐）
    LLM_MOCK=true

    # 方式二：代码里显式指定
    from app.core.mock_llm import get_mock_chat_model
    llm = get_mock_chat_model()

模拟模型的行为约定（重要，请先读一遍）：
    1. 调用 with_structured_output(Schema) 时，会按 Schema 的字段类型
       自动造一份"结构完全合法"的数据。列表字段会给出 2-3 个元素，
       不会给空列表——否则下游节点拿到空数据会走不到有效分支。
    2. 审核场景（Schema 里有 verdict 字段）做了"剧情设计"：
       第 1 次审核返回 revise（不通过），第 2 次起返回 pass。
       这是为了让你能亲眼看到反馈循环回退、修订、再审、通过的全过程。
       如果要重置这个计数器，调用 reset_mock_scenario()。
    3. 普通文本生成（invoke）会根据 Prompt 里的关键词返回不同的
       占位文本，让不同节点的输出看起来有明显区别，便于观察状态流转。
"""

# __future__ 导入让类型注解延迟求值。
# 好处：可以在类定义里安全地写 `-> MockAIMessage` 这种"前向引用"
# （即引用后面才定义的类），在 Python 3.10 上也无需加引号。
from __future__ import annotations

# hashlib 用于给文本算哈希，是 MockEmbeddings 把文本变成向量的基础工具。
# 选 md5 不是因为安全，而是因为它快且结果稳定（同输入永远同输出）。
import hashlib

# re 用于从 Prompt 里提取结构化信息（比如 JSON 代码块）
import re

# Any 表示任意类型；Literal / get_args / get_origin 用于解析类型注解
from typing import Any, Literal, get_args, get_origin

# BaseModel 用于判断"某个类型注解是不是 Pydantic 模型"
from pydantic import BaseModel

from app.core.config import settings


# ===============================================================
# 一、模拟消息对象
# ===============================================================


class MockAIMessage:
    """
    冒充 LangChain 的 AIMessage（模型返回的消息对象）。

    真实 AIMessage 上业务代码只用到两个属性：
        .content         模型输出的文本
        .usage_metadata  token 用量字典

    所以这里只需要实现这两个即可。这就是"鸭子类型"的好处：
        只要接口一致，业务代码根本不需要知道拿到的是真货还是替身。

    属性:
        content        模拟的模型输出文本
        usage_metadata token 用量，形如 {"input_tokens": .., "output_tokens": ..}
    """

    def __init__(self, content: str, prompt_tokens: int = 0, completion_tokens: int = 0):
        """
        参数:
            content           要返回的文本
            prompt_tokens     模拟的输入 token 数
            completion_tokens 模拟的输出 token 数
        """
        # 模型输出正文
        self.content: str = content
        # 用量信息。注意键名用 input_tokens / output_tokens，
        # 与 LangChain 官方 usage_metadata 的命名保持一致，
        # 这样 TokenUsageTracker.add_from_message 无需改动就能解析。
        self.usage_metadata: dict[str, int] = {
            "input_tokens": prompt_tokens,
            "output_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        }

    def __repr__(self) -> str:
        """
        自定义打印格式。

        为什么要写：调试时经常直接 print(response)，
        默认的 <MockAIMessage object at 0x...> 毫无信息量，
        打印出内容前 60 个字符能省下大量来回翻看的时间。
        """
        preview = self.content[:60].replace("\n", " ")
        return f"MockAIMessage({preview!r}...)"


# ===============================================================
# 二、审核剧情设计（让反馈循环真的能被观察到）
# ===============================================================
# 为什么需要这个：
#    如果 Mock 永远返回"审核通过"，你就永远看不到反馈循环回退的那条边，
#    而这个循环恰恰是多智能体研究报告项目最有技术含量的部分。
#    所以这里设计一个可控的"剧情"：前 N 次审核判不通过，之后判通过。
#
# 用模块级可变字典而不是类属性：
#    图上多个节点会各自创建自己的 structured runnable，
#    计数器必须跨实例共享，才能形成"第 1 次 / 第 2 次"的序列。
_MOCK_SCENARIO: dict[str, Any] = {
    # 已发生的审核次数
    "review_calls": 0,
    # 前几次审核判定为"需要修订"。设为 1 表示第 1 次不通过、第 2 次通过
    "fail_first_n": 1,
}


def reset_mock_scenario(fail_first_n: int | None = None) -> None:
    """
    重置模拟剧情，便于重复演示。

    参数:
        fail_first_n 前几次审核判不通过。传 None 表示沿用当前设置。
                     想直接看"一次通过"就传 0；
                     想看"改两轮"就传 2。

    返回: 无

    使用示例:
        reset_mock_scenario(fail_first_n=2)   # 让接下来两次审核都不通过
    """
    _MOCK_SCENARIO["review_calls"] = 0
    if fail_first_n is not None:
        _MOCK_SCENARIO["fail_first_n"] = fail_first_n


# ===============================================================
# 三、通用"按 Schema 造数据"引擎
# ===============================================================


# 造数据时使用的占位文案。集中定义便于统一替换，
# 也方便你一眼看出哪些内容来自 Mock 而不是真实模型。
_PLACEHOLDER_TEXT = "（MOCK 占位内容，配置真实 API Key 后会替换为模型输出）"


def _pick_literal_values(annotation: Any) -> list[Any]:
    """
    从 Literal 类型注解里取出所有可选值。

    为什么要单独抽出来：结构化输出里经常用 Literal 限定枚举，
    例如 chart_type: Literal["bar", "line", "pie"]。
    Mock 必须从这些合法值里挑一个，随便编一个字符串会导致校验失败。

    参数:
        annotation 类型注解，例如 Literal["bar", "line", "pie"]

    返回:
        可选值列表；不是 Literal 时返回空列表。

    示例:
        _pick_literal_values(Literal["a", "b"])  ->  ["a", "b"]
        _pick_literal_values(str)                ->  []
    """
    # get_origin 能识别出这是不是 Literal[...]
    if get_origin(annotation) is Literal:
        # get_args 取出方括号里的所有值
        return list(get_args(annotation))
    return []


def _make_text_for_field(field_name: str, prefix: str = "") -> str:
    """
    为字符串字段生成一段"看起来合理"的占位文本。

    为什么要按字段名区分，而不是统一返回同一句话：
        下游节点和界面展示会读取这些文本。如果所有字段内容一模一样，
        你观察状态流转时会分不清"这个值是哪个节点写的"。
        按字段名给出不同的文案，调试体验好很多。

    参数:
        field_name 字段名，如 "title"、"goal"
        prefix     可选前缀，用于嵌套场景下加上层级信息

    返回:
        一段中文占位文本
    """
    # 字段名 -> 该字段在业务上应该长什么样
    samples: dict[str, str] = {
        "title": "示例标题",
        "goal": "说明这一节要回答的核心问题",
        "section_id": "1",
        "question": "示例研究子问题",
        "summary": "示例小结：本轮共检索到若干条资料，覆盖了主要子问题。",
        "reason": "依据数据结构自动选择的展示方式",
        "explanation": "示例说明：这里解释生成结果的依据。",
        "confidence": "medium",
        "verdict": "pass",
        "metric": "示例指标",
        "message": "示例消息",
    }
    base = samples.get(field_name, f"示例内容-{field_name}")
    # prefix 用于嵌套结构，例如 "第1章 > title"，便于看出层级
    return f"{prefix}{base}｜{_PLACEHOLDER_TEXT}" if prefix else f"{base}｜{_PLACEHOLDER_TEXT}"


def _make_list_items(item_type: Any, field_name: str, count: int = 3) -> list[Any]:
    """
    为列表字段生成元素。

    为什么不能直接返回空列表：
        绝大多数下游逻辑是"遍历列表做事"。空列表会静默跳过，
        流程图看起来跑通了，但什么也没做——这种"假成功"最难排查。
        给定 2-3 个元素能保证每个分支都被真正执行到。

    参数:
        item_type  列表元素的类型注解，如 str、dict[str, Any]、某个 BaseModel
        field_name 字段名，用于生成更有意义的占位文本
        count      生成几个元素，默认 3

    返回:
        元素列表
    """
    # 元素是 str：直接生成带序号的文本
    if item_type is str or item_type is Any:
        return [f"示例{field_name}第{i}条｜{_PLACEHOLDER_TEXT}" for i in range(1, count + 1)]

    # 元素是 dict：造一个含常用键的字典。
    # 取 item_type 的 value 类型其实用不上，这里统一给 str 值即可，
    # Pydantic 会自己做校验，不合格会抛错（这本身也是一种提醒）。
    if get_origin(item_type) is dict or item_type is dict:
        return [
            {"key": f"{field_name}_{i}", "value": f"示例值{i}", "note": _PLACEHOLDER_TEXT}
            for i in range(1, count + 1)
        ]

    # 元素是嵌套的 Pydantic 模型：递归造数据
    if isinstance(item_type, type) and issubclass(item_type, BaseModel):
        return [_fill_model(item_type) for _ in range(count)]

    # 其他类型（int / float 等）：按类型造
    return [_make_value(item_type, field_name) for _ in range(count)]


def _make_value(annotation: Any, field_name: str) -> Any:
    """
    根据类型注解造一个合法的值。这是整个 Mock 引擎的核心分支函数。

    参数:
        annotation 类型注解（可能是 str / int / list[str] / Literal[...] / BaseModel 子类……）
        field_name 字段名，用于生成有意义的占位文本

    返回:
        符合该注解的值
    """
    # ---- 情况 1：Literal 枚举，必须从合法值里挑 ----
    literal_values = _pick_literal_values(annotation)
    if literal_values:
        return literal_values[0]

    # ---- 情况 2：联合类型（如 str | None、int | None）----
    # get_origin 对 `str | None` 返回 types.UnionType（Python 3.10+），
    # 对 typing.Optional[str] 返回 typing.Union。
    # 这里统一用"有没有非 None 的分支"来判断，兼容两种写法。
    origin = get_origin(annotation)
    if origin is not None and str(origin) in ("typing.Union", "types.UnionType"):
        args = [a for a in get_args(annotation) if a is not type(None)]
        # 挑第一个非 None 的分支去造值。这样 Optional 字段会得到一个"有值"的结果，
        # 而不是 None——有值更利于观察流程。
        if args:
            return _make_value(args[0], field_name)
        return None

    # ---- 情况 3：列表 ----
    if origin in (list, tuple, set):
        args = get_args(annotation)
        item_type = args[0] if args else str
        return _make_list_items(item_type, field_name)

    # ---- 情况 4：字典 ----
    if origin is dict:
        # 注意：字典的"值类型"也必须遵守。早期版本无论值类型一律填字符串，
        # 遇到 dict[str, int]（例如 ReportDraft.section_word_counts）会在
        # Pydantic 校验阶段抛 ValidationError，把 Mock 模式下的整条流程打断。
        args = get_args(annotation)
        value_type = args[1] if len(args) == 2 else str
        # 只对明确的基础类型按值类型造值；dict[str, Any] 之类保持占位文本，
        # 避免填成 None 之后更难排查。
        if value_type in (int, float, bool):
            return {"note": _make_value(value_type, field_name)}
        return {"note": _PLACEHOLDER_TEXT}

    # ---- 情况 5：嵌套 Pydantic 模型 ----
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return _fill_model(annotation)

    # ---- 情况 6：基础类型 ----
    if annotation is str:
        return _make_text_for_field(field_name)
    if annotation is bool:
        # 布尔字段统一给 True：Mock 场景下希望流程"继续往前走"，
        # 给 False 容易让流程提前终止，反而看不到后面的逻辑。
        return True
    if annotation is int:
        return 0
    if annotation is float:
        return 0.0

    # ---- 兜底：完全无法识别的类型，给 None ----
    # 不抛异常，是为了让 Mock 尽量"不阻塞流程"。真有问题，
    # Pydantic 校验阶段会给出明确报错，比在这里静默处理要好。
    return None


# ===============================================================
# 三·B、SQL 场景剧本（Text-to-SQL 项目专用）
# ===============================================================
# 为什么需要这一节（这段说明值得读一遍，它解释了一个真实的工程坑）：
#
#   上面那套通用引擎只会给字符串字段填一句中文占位文本。
#   对普通文本字段这没问题，但 sql 字段拿到
#   "示例内容-sql｜（MOCK 占位内容……）"之后，sqlparse 会把它判成
#   UNKNOWN 语句类型，安全校验层必然拦截，于是整条链路固定走
#   "重试 -> 放弃"，执行节点和出图节点永远不会被触达。
#
#   后果很具体：不配 API Key 的访客打开演示页面，只能看到一个失败提示。
#   对一个要放到 GitHub 上做作品展示的项目来说，这等于没有 Demo。
#
#   所以这里为 SQL 字段准备了一组**真实可执行**的样例查询。
#   它们只查演示用的电商库（customers / products / orders /
#   order_items / refunds），并且刻意避开了安全校验层的全部黑名单
#   （不使用 INTO / CREATE / UPDATE / DELETE 等危险关键字），
#   因此 MOCK 模式下也能完整走通
#   "提问 -> 意图 -> 检索 -> 生成 -> 校验 -> 执行 -> 出图"。
#
# 匹配方式：
#   对每条样例的关键词做命中长度加权打分，取分值最高的一条。
#   用"关键词长度"而不是"命中个数"加权，是为了让更具体的词胜出：
#   "有哪些商品类目" 同时命中"类目"和"有哪些商品类目"，
#   按长度加权后才会选中"列举类目"，而不是"按类目统计销售额"。
#
# 如何扩展：
#   照抄一条，改 keywords / sql / metric / dimensions 即可。
#   新增的 SQL 必须能被 sql_guard 放行，改完跑一次
#   python -m app.agent.safety.sql_graph 就能验证。

# SQL 场景样例库。顺序不影响结果（按分数取最优），但建议由具体到宽泛排列，
# 便于人工阅读。每条包含：
#   keywords    命中关键词。命中长度之和即为该条得分
#   sql         样例查询。必须是可执行且能通过安全校验的 SELECT
#   tables      这条 SQL 用到的表，会填进 SQLDraft.tables_used
#   metric      填进 QueryIntent.metric，让意图解析结果看起来合理
#   dimensions  填进 QueryIntent.dimensions
#   explanation 填进 SQLDraft.explanation，说明查询思路
_MOCK_SQL_SCENES: list[dict[str, Any]] = [
    # ---- 相对时间：最近三个月销售额 ----
    {
        "keywords": ("最近三个月", "最近3个月", "近三个月"),
        "sql": (
            "SELECT strftime('%Y-%m', orders.order_date) AS 月份,\n"
            "       ROUND(SUM(orders.total_amount), 2) AS 销售额,\n"
            "       COUNT(*) AS 订单数\n"
            "FROM orders\n"
            "WHERE orders.status NOT IN ('已取消', '待支付')\n"
            "  AND orders.order_date >= DATE((SELECT MAX(order_date) FROM orders), '-3 months')\n"
            "GROUP BY 月份\n"
            "ORDER BY 月份"
        ),
        "tables": ["orders"],
        "metric": "销售额+订单数",
        "dimensions": ["月份"],
        "time_range": "最近三个月（基于数据中的最大日期推算）",
        "sort_and_limit": "按月份升序",
        "explanation": (
            "相对时间不能写 CURRENT_DATE，因为数据是历史快照。"
            "这里用子查询取出 orders 表中的最大下单日期作为基准，再往前推三个月。"
            "同时按业务口径排除了已取消与待支付订单。"
        ),
    },
    # ---- 月度趋势 ----
    {
        "keywords": ("每个月", "按月", "月度", "每月", "月份"),
        "sql": (
            "SELECT strftime('%Y-%m', orders.order_date) AS 月份,\n"
            "       COUNT(*) AS 订单数,\n"
            "       ROUND(SUM(orders.total_amount), 2) AS 销售额\n"
            "FROM orders\n"
            "WHERE orders.status NOT IN ('已取消', '待支付')\n"
            "GROUP BY 月份\n"
            "ORDER BY 月份"
        ),
        "tables": ["orders"],
        "metric": "销售额+订单数",
        "dimensions": ["月份"],
        "sort_and_limit": "按月份升序",
        "explanation": (
            "用 strftime 把下单日期截断到月份再分组。"
            "注意这里没有用 CURRENT_DATE 之类的函数做时间过滤，"
            "因为演示数据是历史快照，口径应与数据本身对齐。"
        ),
    },
    # ---- 季度销售额 ----
    {
        "keywords": ("季度", "环比"),
        "sql": (
            "SELECT strftime('%Y', orders.order_date) || '-Q' ||\n"
            "       CAST((CAST(strftime('%m', orders.order_date) AS INTEGER) + 2) / 3 AS TEXT) AS 季度,\n"
            "       ROUND(SUM(orders.total_amount), 2) AS 销售额\n"
            "FROM orders\n"
            "WHERE orders.status NOT IN ('已取消', '待支付')\n"
            "GROUP BY 季度\n"
            "ORDER BY 季度"
        ),
        "tables": ["orders"],
        "metric": "销售额",
        "dimensions": ["季度"],
        "sort_and_limit": "按季度升序",
        "explanation": (
            "先把月份截断到季度：SQLite 没有 QUARTER 函数，"
            "用 (月份 + 2) / 3 的整数除法映射到 1-4 季度，再拼上年份。"
        ),
    },
    # ---- 退款率：四表关联 ----
    {
        "keywords": ("退款率", "退款金额占比"),
        "sql": (
            "SELECT products.category AS 商品类目,\n"
            "       ROUND(COALESCE(SUM(refunds.refund_amount), 0), 2) AS 退款金额,\n"
            "       ROUND(SUM(order_items.quantity * order_items.unit_price - order_items.discount), 2) AS 销售额,\n"
            "       ROUND(COALESCE(SUM(refunds.refund_amount), 0) * 100.0\n"
            "             / NULLIF(SUM(order_items.quantity * order_items.unit_price - order_items.discount), 0), 2) AS 退款率\n"
            "FROM order_items\n"
            "JOIN products ON order_items.product_id = products.product_id\n"
            "JOIN orders ON order_items.order_id = orders.order_id\n"
            "LEFT JOIN refunds ON refunds.order_id = orders.order_id\n"
            "GROUP BY products.category\n"
            "ORDER BY 退款率 DESC"
        ),
        "tables": ["orders", "order_items", "products", "refunds"],
        "metric": "退款率",
        "dimensions": ["商品类目"],
        "sort_and_limit": "按退款率降序",
        "explanation": (
            "退款不是每单都有，所以关联 refunds 必须用 LEFT JOIN，"
            "写成 INNER JOIN 会把没有退款的类目整类丢掉。"
            "分母用 NULLIF 做了除零保护。"
        ),
    },
    # ---- 退款原因分布 ----
    {
        "keywords": ("退款原因", "退款理由"),
        "sql": (
            "SELECT refunds.refund_reason AS 退款原因,\n"
            "       COUNT(*) AS 退款笔数,\n"
            "       ROUND(SUM(refunds.refund_amount), 2) AS 退款金额\n"
            "FROM refunds\n"
            "GROUP BY refunds.refund_reason\n"
            "ORDER BY 退款笔数 DESC"
        ),
        "tables": ["refunds"],
        "metric": "退款笔数+退款金额",
        "dimensions": ["退款原因"],
        "sort_and_limit": "按退款笔数降序",
        "explanation": "直接在退款表上按退款原因分组统计，得到各原因的笔数与金额分布。",
    },
    # ---- 发生过退款的订单 ----
    {
        "keywords": ("退款", "退货"),
        "sql": (
            "SELECT orders.order_id AS 订单号,\n"
            "       refunds.refund_amount AS 退款金额,\n"
            "       refunds.refund_reason AS 退款原因,\n"
            "       refunds.refund_date AS 退款日期\n"
            "FROM refunds\n"
            "JOIN orders ON refunds.order_id = orders.order_id\n"
            "ORDER BY refunds.refund_amount DESC"
        ),
        "tables": ["orders", "refunds"],
        "metric": "退款金额",
        "dimensions": ["订单号"],
        "sort_and_limit": "按退款金额降序",
        "explanation": (
            "退款信息只能从 refunds 表查，不能从订单状态推断。"
            "这里用 refunds 关联 orders 拿到订单号。"
        ),
    },
    # ---- 从未售出的商品：反连接 ----
    {
        "keywords": ("没被卖", "没有卖", "从未", "不曾", "没卖过", "卖出过", "从来没有"),
        "sql": (
            "SELECT products.product_id AS 商品ID,\n"
            "       products.product_name AS 商品名称,\n"
            "       products.category AS 商品类目\n"
            "FROM products\n"
            "LEFT JOIN order_items ON products.product_id = order_items.product_id\n"
            "WHERE order_items.product_id IS NULL\n"
            "ORDER BY products.product_id"
        ),
        "tables": ["products", "order_items"],
        "metric": "商品数",
        "dimensions": ["商品名称"],
        "explanation": (
            "这是反连接场景，必须用 LEFT JOIN 再筛 NULL，"
            "写成 INNER JOIN 会得到完全相反的结果（只剩卖出去的商品）。"
        ),
    },
    # ---- 平均每笔订单的商品种类数 ----
    {
        "keywords": ("几种商品", "包含几种", "商品种类", "每笔订单包含"),
        "sql": (
            "SELECT ROUND(AVG(item_count), 2) AS 平均每笔订单商品种类数\n"
            "FROM (\n"
            "    SELECT orders.order_id AS order_id, COUNT(*) AS item_count\n"
            "    FROM orders\n"
            "    JOIN order_items ON orders.order_id = order_items.order_id\n"
            "    WHERE orders.status NOT IN ('已取消', '待支付')\n"
            "    GROUP BY orders.order_id\n"
            ")"
        ),
        "tables": ["orders", "order_items"],
        "metric": "平均每笔订单商品种类数",
        "dimensions": [],
        "explanation": (
            "这是嵌套聚合：先按订单统计各自的商品条目数，"
            "再对结果求平均。不能直接 COUNT(*) 除以订单数，"
            "因为未下单的订单会把分母算大。"
        ),
    },
    # ---- 复购客户占比 ----
    {
        "keywords": ("复购", "占比", "比例", "多少比例"),
        "sql": (
            "SELECT ROUND(\n"
            "         COUNT(DISTINCT CASE WHEN order_count >= 2 THEN customer_id END) * 100.0\n"
            "         / NULLIF(COUNT(DISTINCT customer_id), 0), 2\n"
            "       ) AS 复购客户占比\n"
            "FROM (\n"
            "    SELECT orders.customer_id AS customer_id, COUNT(*) AS order_count\n"
            "    FROM orders\n"
            "    WHERE orders.status NOT IN ('已取消', '待支付')\n"
            "    GROUP BY orders.customer_id\n"
            ")"
        ),
        "tables": ["orders"],
        "metric": "复购客户占比",
        "dimensions": [],
        "explanation": (
            "先按客户聚合出各自的有效订单数，再在外层统计订单数 >= 2 的客户占比。"
            "分母口径取「下过有效订单的客户数」而不是全量客户数，"
            "否则未下单客户会被算进分母，比例被系统性低估。"
        ),
    },
    # ---- 类目销售额：三表关联 ----
    {
        "keywords": ("类目", "品类", "分类"),
        "sql": (
            "SELECT products.category AS 商品类目,\n"
            "       ROUND(SUM(order_items.quantity * order_items.unit_price - order_items.discount), 2) AS 销售额,\n"
            "       SUM(order_items.quantity) AS 销量\n"
            "FROM order_items\n"
            "JOIN products ON order_items.product_id = products.product_id\n"
            "JOIN orders ON order_items.order_id = orders.order_id\n"
            "WHERE orders.status NOT IN ('已取消', '待支付')\n"
            "GROUP BY products.category\n"
            "ORDER BY 销售额 DESC"
        ),
        "tables": ["orders", "order_items", "products"],
        "metric": "销售额+销量",
        "dimensions": ["商品类目"],
        "sort_and_limit": "按销售额降序",
        "explanation": (
            "销售额要按明细算：quantity * unit_price - discount，"
            "不能直接用 orders.total_amount（那是订单级口径，无法拆到类目）。"
            "关联 orders 是为了套用「排除已取消与待支付」的口径。"
        ),
    },
    # ---- 类目去重列举 ----
    {
        "keywords": ("有哪些类目", "哪些类目", "有哪些商品类目", "类目都有", "几种类目"),
        "sql": (
            "SELECT DISTINCT products.category AS 商品类目\n"
            "FROM products\n"
            "ORDER BY 商品类目"
        ),
        "tables": ["products"],
        "metric": "商品类目清单",
        "dimensions": ["商品类目"],
        "explanation": "对 products.category 去重列举，得到库里实际存在的类目清单。",
    },
    # ---- 品牌维度 ----
    {
        "keywords": ("品牌", "各品牌", "每个品牌"),
        "sql": (
            "SELECT products.brand AS 品牌,\n"
            "       ROUND(AVG(products.unit_price), 2) AS 平均价格,\n"
            "       COUNT(*) AS 商品数\n"
            "FROM products\n"
            "GROUP BY products.brand\n"
            "ORDER BY 平均价格 DESC"
        ),
        "tables": ["products"],
        "metric": "平均价格+商品数",
        "dimensions": ["品牌"],
        "sort_and_limit": "按平均价格降序",
        "explanation": "按品牌分组，对 unit_price 求平均并统计商品数。",
    },
    # ---- 商品价格 ----
    {
        "keywords": ("平均价格", "均价", "平均售价"),
        "sql": "SELECT ROUND(AVG(products.unit_price), 2) AS 商品平均价格\nFROM products",
        "tables": ["products"],
        "metric": "商品平均价格",
        "dimensions": [],
        "explanation": "对 products.unit_price 求平均，得到全库商品的均价。",
    },
    {
        "keywords": ("售价", "价格", "单价", "最贵"),
        "sql": (
            "SELECT products.product_name AS 商品名称,\n"
            "       products.category AS 商品类目,\n"
            "       products.unit_price AS 商品单价\n"
            "FROM products\n"
            "ORDER BY products.unit_price DESC\n"
            "LIMIT 10"
        ),
        "tables": ["products"],
        "metric": "商品单价",
        "dimensions": ["商品名称"],
        "sort_and_limit": "按单价降序取前 10",
        "explanation": "按标准单价倒序排列商品，取价格最高的若干条。",
    },
    # ---- 年龄维度 ----
    {
        "keywords": ("年龄", "年纪", "多大"),
        "sql": (
            "SELECT customers.name AS 客户姓名,\n"
            "       customers.age AS 年龄,\n"
            "       customers.city AS 城市,\n"
            "       customers.level AS 会员等级\n"
            "FROM customers\n"
            "WHERE customers.age IS NOT NULL\n"
            "ORDER BY customers.age DESC"
        ),
        "tables": ["customers"],
        "metric": "客户年龄",
        "dimensions": ["客户姓名"],
        "sort_and_limit": "按年龄降序取前 5",
        "explanation": (
            "age 字段允许为空，所以先过滤掉 NULL 再排序；"
            "否则空值会排在结果两端，看起来像是异常数据。"
        ),
    },
    # ---- 库存 ----
    {
        "keywords": ("库存", "缺货", "没货"),
        "sql": (
            "SELECT COUNT(*) AS 零库存商品数\n"
            "FROM products\n"
            "WHERE products.stock_qty = 0"
        ),
        "tables": ["products"],
        "metric": "零库存商品数",
        "dimensions": [],
        "explanation": "筛选 stock_qty = 0 的商品并计数。",
    },
    # ---- 会员等级 ----
    {
        "keywords": ("会员等级", "会员", "金牌", "银牌", "铂金"),
        "sql": (
            "SELECT customers.level AS 会员等级,\n"
            "       COUNT(DISTINCT customers.customer_id) AS 客户数,\n"
            "       ROUND(AVG(orders.total_amount), 2) AS 平均订单金额\n"
            "FROM orders\n"
            "JOIN customers ON orders.customer_id = customers.customer_id\n"
            "WHERE orders.status NOT IN ('已取消', '待支付')\n"
            "GROUP BY customers.level\n"
            "ORDER BY 平均订单金额 DESC"
        ),
        "tables": ["orders", "customers"],
        "metric": "平均订单金额+客户数",
        "dimensions": ["会员等级"],
        "sort_and_limit": "按平均订单金额降序",
        "explanation": (
            "会员等级在 customers 表，订单金额在 orders 表，需要按客户 ID 关联。"
            "客户数用 COUNT(DISTINCT) 去重，因为一个客户会有多笔订单。"
        ),
    },
    # ---- 渠道 ----
    {
        "keywords": ("有哪些渠道", "哪些渠道", "不同的下单渠道", "渠道都有"),
        "sql": (
            "SELECT DISTINCT orders.channel AS 下单渠道\n"
            "FROM orders\n"
            "ORDER BY 下单渠道"
        ),
        "tables": ["orders"],
        "metric": "下单渠道清单",
        "dimensions": ["下单渠道"],
        "explanation": "对 orders.channel 去重列举。",
    },
    {
        "keywords": ("渠道", "App", "小程序", "PC官网"),
        "sql": (
            "SELECT orders.channel AS 下单渠道,\n"
            "       COUNT(*) AS 订单数,\n"
            "       ROUND(SUM(orders.total_amount), 2) AS 销售额\n"
            "FROM orders\n"
            "WHERE orders.status NOT IN ('已取消', '待支付')\n"
            "GROUP BY orders.channel\n"
            "ORDER BY 销售额 DESC"
        ),
        "tables": ["orders"],
        "metric": "销售额+订单数",
        "dimensions": ["下单渠道"],
        "sort_and_limit": "按销售额降序",
        "explanation": "按下单渠道分组，统计订单数与有效销售额。",
    },
    # ---- 支付方式 ----
    {
        "keywords": ("支付方式", "支付"),
        "sql": (
            "SELECT orders.payment_method AS 支付方式,\n"
            "       COUNT(*) AS 订单数\n"
            "FROM orders\n"
            "GROUP BY orders.payment_method\n"
            "ORDER BY 订单数 DESC"
        ),
        "tables": ["orders"],
        "metric": "订单数",
        "dimensions": ["支付方式"],
        "sort_and_limit": "按订单数降序",
        "explanation": "按支付方式分组统计订单数，并按数量倒序排列。",
    },
    # ---- 订单状态 ----
    {
        "keywords": ("订单状态", "状态"),
        "sql": (
            "SELECT orders.status AS 订单状态,\n"
            "       COUNT(*) AS 订单数\n"
            "FROM orders\n"
            "GROUP BY orders.status\n"
            "ORDER BY 订单数 DESC"
        ),
        "tables": ["orders"],
        "metric": "订单数",
        "dimensions": ["订单状态"],
        "sort_and_limit": "按订单数降序",
        "explanation": "按 status 分组统计各状态的订单数量。",
    },
    # ---- 销量最高的商品 ----
    {
        "keywords": ("销量", "卖出多少", "卖得最好", "卖了多少"),
        "sql": (
            "SELECT products.product_name AS 商品名称,\n"
            "       products.category AS 商品类目,\n"
            "       SUM(order_items.quantity) AS 销量\n"
            "FROM order_items\n"
            "JOIN products ON order_items.product_id = products.product_id\n"
            "GROUP BY products.product_id, products.product_name, products.category\n"
            "ORDER BY 销量 DESC\n"
            "LIMIT 10"
        ),
        "tables": ["order_items", "products"],
        "metric": "商品销量",
        "dimensions": ["商品名称"],
        "sort_and_limit": "按销量降序取前 10",
        "explanation": "按商品汇总 order_items.quantity 得到销量，并关联商品名便于阅读。",
    },
    # ---- 客户消费排行 ----
    {
        "keywords": ("消费", "客户姓名", "是谁", "哪位客户", "前 5 名", "前5名", "排名前"),
        "sql": (
            "SELECT customers.name AS 客户姓名,\n"
            "       customers.level AS 会员等级,\n"
            "       ROUND(SUM(orders.total_amount), 2) AS 累计消费金额\n"
            "FROM orders\n"
            "JOIN customers ON orders.customer_id = customers.customer_id\n"
            "WHERE orders.status NOT IN ('已取消', '待支付')\n"
            "GROUP BY customers.customer_id, customers.name, customers.level\n"
            "ORDER BY 累计消费金额 DESC\n"
            "LIMIT 10"
        ),
        "tables": ["orders", "customers"],
        "metric": "累计消费金额",
        "dimensions": ["客户姓名", "会员等级"],
        "sort_and_limit": "按累计消费金额降序取前 5",
        "explanation": (
            "按客户聚合有效订单金额。分组时带上 customer_id 而不是只用姓名，"
            "避免重名客户被错误合并。"
        ),
    },
    # ---- 订单金额排行 ----
    {
        "keywords": ("订单", "下单", "客单价"),
        "sql": (
            "SELECT orders.order_id AS 订单号,\n"
            "       customers.name AS 客户姓名,\n"
            "       ROUND(orders.total_amount, 2) AS 订单金额,\n"
            "       orders.order_date AS 下单日期\n"
            "FROM orders\n"
            "JOIN customers ON orders.customer_id = customers.customer_id\n"
            "WHERE orders.status NOT IN ('已取消', '待支付')\n"
            "ORDER BY orders.total_amount DESC\n"
            "LIMIT 10"
        ),
        "tables": ["orders", "customers"],
        "metric": "订单金额",
        "dimensions": ["订单号", "客户姓名"],
        "sort_and_limit": "按订单金额降序取前 10",
        "explanation": "按订单金额倒序排列，关联客户表取出下单人姓名。",
    },
    # ---- 省份 ----
    {
        "keywords": ("省份", "各省", "每个省", "省"),
        "sql": (
            "SELECT customers.province AS 省份,\n"
            "       COUNT(DISTINCT customers.customer_id) AS 客户数\n"
            "FROM customers\n"
            "GROUP BY customers.province\n"
            "ORDER BY 客户数 DESC"
        ),
        "tables": ["customers"],
        "metric": "客户数",
        "dimensions": ["省份"],
        "sort_and_limit": "按客户数降序",
        "explanation": "按 province 分组统计客户数量。",
    },
    # ---- 城市（含 HAVING 过滤）----
    {
        "keywords": ("城市", "每个城市", "各城市"),
        "sql": (
            "SELECT customers.city AS 城市,\n"
            "       COUNT(DISTINCT customers.customer_id) AS 客户数\n"
            "FROM customers\n"
            "GROUP BY customers.city\n"
            "HAVING COUNT(DISTINCT customers.customer_id) > 5\n"
            "ORDER BY 客户数 DESC"
        ),
        "tables": ["customers"],
        "metric": "客户数",
        "dimensions": ["城市"],
        "sort_and_limit": "按客户数降序，仅保留客户数大于 5 的城市",
        "explanation": (
            "分组后的过滤条件要用 HAVING，不能用 WHERE："
            "WHERE 在分组前生效，拿不到聚合结果。"
        ),
    },
    # ---- 平均订单金额 ----
    {
        "keywords": ("平均每个订单", "平均订单金额", "平均", "均值"),
        "sql": (
            "SELECT ROUND(AVG(orders.total_amount), 2) AS 平均订单金额\n"
            "FROM orders\n"
            "WHERE orders.status NOT IN ('已取消', '待支付')"
        ),
        "tables": ["orders"],
        "metric": "平均订单金额",
        "dimensions": [],
        "explanation": "排除无效订单后对 total_amount 求平均。",
    },
    # ---- 客户数 / 商品数（基础统计兜底）----
    {
        "keywords": ("客户", "用户", "多少人"),
        "sql": "SELECT COUNT(DISTINCT customers.customer_id) AS 客户总数\nFROM customers",
        "tables": ["customers"],
        "metric": "客户总数",
        "dimensions": [],
        "explanation": "对 customers 主键去重计数得到客户总数。",
    },
]

# 完全没命中关键词时的兜底查询。
# 给一条"最近订单列表"而不是 COUNT(*)，是因为返回多行数据
# 才能让下游的画图节点真的画出图来，演示效果更完整。
_MOCK_SQL_FALLBACK: dict[str, Any] = {
    "keywords": (),
    "sql": (
        "SELECT orders.order_id AS 订单号,\n"
        "       orders.order_date AS 下单日期,\n"
        "       orders.status AS 订单状态,\n"
        "       ROUND(orders.total_amount, 2) AS 订单金额\n"
        "FROM orders\n"
        "WHERE orders.status NOT IN ('已取消', '待支付')\n"
        "ORDER BY orders.order_date DESC"
    ),
    "tables": ["orders"],
    "metric": "订单明细",
    "dimensions": ["订单号"],
    "sort_and_limit": "按下单日期降序",
    "explanation": "未识别到明确的分析意图，返回最近的有效订单明细供进一步查看。",
}


# 从提示词里抠出用户原始提问的正则。
# 两种写法都要覆盖：意图解析节点写的是"用户提问："，
# SQL 生成节点写的是"用户的原始提问："。用交替分支一次匹配。
_QUESTION_PATTERN = re.compile(r"(?:用户的原始提问|用户提问)[：:]\s*(.+)")


def _extract_question(prompt_text: str) -> str:
    """
    从提示词全文里提取用户的原始提问。

    为什么要从提示词里抠，而不是让调用方传参：
        模拟层要冒充的是"模型"，模型只能看到提示词。
        如果绕过提示词去拿提问，就模拟不出真实的调用形态。
        另外提问只占提示词的一行，必须精确定位，
        否则 schema 文本里的"销售额""客户"等词也会参与关键词打分，
        导致选错样例。

    参数:
        prompt_text 提示词全文

    返回:
        提问文本；没匹配到返回空字符串
    """
    match = _QUESTION_PATTERN.search(prompt_text or "")
    if not match:
        return ""
    # 提问在提示词里只占一行，按行切开防止把后面的说明文字带进来
    return match.group(1).splitlines()[0].strip()


def _select_sql_scene(question: str) -> dict[str, Any]:
    """
    按提问内容挑一条最贴切的样例查询。

    打分规则：命中关键词的**长度之和**。
        为什么不用命中个数：个数相同无法区分具体与宽泛。
        「有哪些商品类目」同时命中「类目」（2 字）和「有哪些商品类目」（7 字），
        按长度加权后「列举类目」这条得 9 分，
        「按类目统计销售额」只得 2 分，前者胜出——这正是期望的行为。

    参数:
        question 用户提问

    返回:
        命中的样例字典；一个都没命中时返回兜底样例
    """
    best_scene: dict[str, Any] | None = None
    best_score = 0

    for scene in _MOCK_SQL_SCENES:
        score = sum(len(keyword) for keyword in scene["keywords"] if keyword in question)
        # 严格大于：分值相同时保留先出现的那条，保证结果稳定可复现
        if score > best_score:
            best_score = score
            best_scene = scene

    return best_scene if best_scene is not None else _MOCK_SQL_FALLBACK


def _build_sql_scene_overrides(
    schema: type[BaseModel],
    prompt_text: str,
) -> dict[str, Any]:
    """
    为 SQL 相关模型生成字段覆盖值。

    只对两个模型生效：
        SQLDraft     —— 生成可执行的样例 SQL
        QueryIntent  —— 生成合理的意图解析结果（界面会展示这部分）

    参数:
        schema      目标 Pydantic 模型类
        prompt_text 提示词全文，用于提取提问

    返回:
        字段名 -> 值的映射；不适用时返回空字典
    """
    # 只处理 SQL 链路上的两个模型，其他模型（如研究报告、审核结果）不受影响
    if schema.__name__ not in ("SQLDraft", "QueryIntent"):
        return {}

    question = _extract_question(prompt_text)
    if not question:
        return {}

    scene = _select_sql_scene(question)

    # 生成节点：给出可执行 SQL + 用到的表 + 思路说明
    if schema.__name__ == "SQLDraft":
        return {
            "sql": scene["sql"],
            "explanation": scene["explanation"],
            "tables_used": list(scene["tables"]),
        }

    # 意图解析节点：把样例里写好的意图字段填进去
    return {
        "metric": scene["metric"],
        "dimensions": list(scene["dimensions"]),
        "filters": [
            "排除订单状态为「已取消」和「待支付」的订单",
            "销售额按 order_items 的 quantity * unit_price - discount 计算",
        ],
        "time_range": scene.get("time_range", ""),
        "sort_and_limit": scene.get("sort_and_limit", ""),
    }


def _fill_model(schema: type[BaseModel], prompt_text: str = "") -> BaseModel:
    """
    按 Pydantic 模型的字段定义造一份完整数据。

    这里包含三块逻辑：
        1. 通用填充：遍历字段，按类型造值
        2. 审核剧情：如果模型里有 verdict 字段，按预设剧情给出
           "第 N 次不通过 / 之后通过"，让反馈循环可以被观察到
        3. SQL 场景：如果是 SQLDraft / QueryIntent，
           按提问内容给出一条真实可执行的样例查询

    参数:
        schema      目标 Pydantic 模型类
        prompt_text 提示词全文。SQL 场景需要它来提取用户提问；
                    其他场景传不传都不影响

    返回:
        该模型的一个实例
    """
    # 取出所有字段定义。model_fields 是 Pydantic v2 的接口
    # （v1 叫 __fields__，这里用 v2 写法，与 requirements 里 pydantic>=2.9 对应）
    fields = schema.model_fields
    field_names = set(fields.keys())

    # ---- 先处理"审核剧情"这个特例 ----
    # 只要模型里有 verdict 字段，就认为这是一次审核结果
    review_override: dict[str, Any] = {}
    if "verdict" in field_names:
        _MOCK_SCENARIO["review_calls"] += 1
        attempt = _MOCK_SCENARIO["review_calls"]
        # 前 fail_first_n 次判"需要修订"，之后判"通过"
        if attempt <= _MOCK_SCENARIO["fail_first_n"]:
            review_override["verdict"] = "revise"
        else:
            review_override["verdict"] = "pass"

    # ---- 逐个字段填充 ----
    payload: dict[str, Any] = {}
    # SQL 场景覆盖值。只在提示词命中 SQL 生成/意图解析场景时非空，
    # 其余情况是一个空字典，对原有行为零影响
    scene_overrides = _build_sql_scene_overrides(schema, prompt_text)

    for name, field_info in fields.items():
        # 剧情覆盖优先：这是本次调用的"特殊剧本"
        if name in review_override:
            payload[name] = review_override[name]
            continue

        # 其次是 SQL 场景覆盖。必须放在通用分支之前，
        # 否则 sql 字段会被填成一句中文占位文本，安全校验必然拦截，
        # 整条链路就断在这里
        if name in scene_overrides:
            payload[name] = scene_overrides[name]
            continue

        # 与 verdict 配套的字段也要跟着剧情走，否则会出现
        # "verdict=revise 但 issues 为空"这种自相矛盾的数据，
        # 修订节点拿到空意见就无事可做，反馈循环空转。
        if name == "score" and "verdict" in review_override:
            payload[name] = 68 if review_override["verdict"] == "revise" else 88
            continue
        if name == "passed" and "verdict" in review_override:
            payload[name] = review_override["verdict"] == "pass"
            continue
        if name == "issues" and "verdict" in review_override:
            payload[name] = (
                ["示例问题1：第2节的论述缺少数据支撑", "示例问题2：引用编号 S9 未在资料列表中出现"]
                if review_override["verdict"] == "revise"
                else []
            )
            continue
        if name in ("revision_instructions", "instructions") and "verdict" in review_override:
            payload[name] = (
                ["示例建议：在第2节补充 2024 年出口量数据，可引用 S1 与 S3"]
                if review_override["verdict"] == "revise"
                else []
            )
            continue
        if name == "dimensions" and "verdict" in review_override:
            payload[name] = {
                "逻辑性": 85,
                "数据支撑": 45 if review_override["verdict"] == "revise" else 82,
                "结构完整": 90,
                "引用规范": 70,
                "语言表达": 88,
            }
            continue

        # 字段名里带 "url" / "link" 的，给一个明显是假地址的 URL，
        # 避免看起来像真实来源被人误信
        if "url" in name or "link" in name:
            payload[name] = f"https://mock.example.com/{name}"
            continue
        # id 类字段给固定前缀，便于识别
        if name.endswith("_id") or name == "id":
            payload[name] = f"{name.upper()}-MOCK-1"
            continue
        # 时间类字段
        if "time" in name or "date" in name or name.endswith("_at"):
            payload[name] = "2025-09-14T10:00:00"
            continue

        # 剩下的走通用分支
        payload[name] = _make_value(field_info.annotation, name)

    # 用构造器实例化。这里不吞异常——如果 Mock 造的数据都不合法，
    # 说明 Schema 本身有问题，应该立刻暴露而不是掩盖。
    return schema(**payload)


# ===============================================================
# 四、模拟对话模型
# ===============================================================


class MockStructuredRunnable:
    """
    冒充 with_structured_output() 返回的可调用对象。

    真实场景里 `llm.with_structured_output(Schema)` 返回一个 Runnable，
    对它 invoke 会得到 Schema 的实例。这里模拟同样的接口。
    """

    def __init__(
        self,
        schema: type[BaseModel],
        include_raw: bool = False,
        label: str = "mock",
    ) -> None:
        """
        参数:
            schema      目标结构化输出的 Pydantic 模型
            include_raw 是否像真实 LangChain 那样返回 {"raw": 消息, "parsed": 模型, "parsing_error": None}。
                        本项目里为了拿 token 用量会传 True，所以这里也要支持。
            label       标识，用于日志区分是哪个节点在调用
        """
        self.schema = schema
        self.include_raw = include_raw
        self.label = label

    def invoke(self, input: Any = None, config: Any = None) -> Any:
        """
        模拟调用。

        参数:
            input  真实场景是 Prompt；Mock 不使用它，但保留参数保证接口兼容
            config 运行配置（重试、回调等），Mock 忽略

        返回:
            按 include_raw 决定返回 Schema 实例，还是 {"raw", "parsed", "parsing_error"} 字典
        """
        # 造数据 + 顺便估算 token 用量，让用量统计在 Mock 模式下也有数字可看。
        # 把提示词原文传进去，是为了让 SQL 场景剧本能按"用户到底问了什么"
        # 挑一条对应的样例查询，而不是每条提问都返回同一个占位结果
        parsed = _fill_model(self.schema, prompt_text=_to_text(input))
        prompt_tokens = _estimate_tokens(str(input))
        completion_tokens = _estimate_tokens(str(parsed))

        if self.include_raw:
            # 真实 LangChain 的返回结构：
            #   {"raw": AIMessage, "parsed": BaseModel | None, "parsing_error": Exception | None}
            # 业务代码通常读 ["parsed"] 和 ["raw"]，这里保持一致。
            return {
                "raw": MockAIMessage("", prompt_tokens, completion_tokens),
                "parsed": parsed,
                "parsing_error": None,
            }

        # 包一层属性，让调用方如果想拿 token 用量也有地方取
        self.last_usage = {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens}
        return parsed

    # 让这个对象可以被当成函数调用：runnable(prompt)
    # 真实 Runnable 支持 __call__，业务代码里偶尔会这么用
    def __call__(self, input: Any = None, config: Any = None) -> Any:
        return self.invoke(input, config)


class MockChatModel:
    """
    冒充 LangChain 的 ChatOpenAI。

    支持的接口（只覆盖业务代码真正用到的部分）：
        .invoke(prompt)                              返回 MockAIMessage
        .with_structured_output(Schema, include_raw) 返回 MockStructuredRunnable
        .bind_tools(tools)                           返回自身（CrewAI 场景不会真的执行工具）

    设计原则：**不做多余的事**。模拟层越简单越可靠，
    一旦它自己变复杂，就会变成新的 bug 来源。
    """

    def __init__(
        self,
        temperature: float = 0.0,
        max_tokens: int = 2000,
        label: str = "mock-chat",
    ) -> None:
        """
        参数:
            temperature 采样温度（Mock 忽略，保留是为了签名兼容）
            max_tokens  回复上限（Mock 忽略）
            label       标识，打印时用来确认当前跑的是 Mock
        """
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.label = label

    # -----------------------------------------------------------
    # 普通文本生成
    # -----------------------------------------------------------

    def invoke(self, input: Any = None, config: Any = None) -> MockAIMessage:
        """
        模拟一次文本生成。

        Mock 的策略是"看 Prompt 里有什么关键词，就给对应的占位文本"。
        这样不同节点的输出有显著区别，你观察状态流转和界面时一眼能区分。

        参数:
            input  Prompt，可以是字符串、消息列表、或 PromptValue
            config 运行配置，忽略

        返回:
            MockAIMessage 实例
        """
        text = _to_text(input)
        content = _generate_text_for_prompt(text)
        return MockAIMessage(
            content,
            prompt_tokens=_estimate_tokens(text),
            completion_tokens=_estimate_tokens(content),
        )

    # -----------------------------------------------------------
    # 结构化输出
    # -----------------------------------------------------------

    def with_structured_output(self, schema: type[BaseModel], **kwargs: Any) -> MockStructuredRunnable:
        """
        模拟结构化输出。

        参数:
            schema 目标 Pydantic 模型
            kwargs 真实接口支持 include_raw / method / strict 等参数，
                   这里只关心 include_raw（因为它影响返回结构）

        返回:
            MockStructuredRunnable 实例
        """
        return MockStructuredRunnable(
            schema=schema,
            # include_raw 默认 False，和真实接口一致
            include_raw=bool(kwargs.get("include_raw", False)),
            label=kwargs.get("label", self.label),
        )

    # -----------------------------------------------------------
    # 工具绑定（CrewAI / Agent 场景会调用）
    # -----------------------------------------------------------

    def bind_tools(self, tools: Any = None, **kwargs: Any) -> MockChatModel:
        """
        模拟工具绑定。

        真实场景会返回一个"知道有哪些工具可调"的模型实例。
        因为 Mock 不会真的发起工具调用，所以直接返回自身即可。
        """
        # 把工具名记下来，方便调试时确认工具有没有正确传给 Agent
        self.bound_tool_names = [
            getattr(t, "name", getattr(t, "__name__", str(t))) for t in (tools or [])
        ]
        return self

    # -----------------------------------------------------------
    # 让实例本身可调用：runnable(prompt)
    # -----------------------------------------------------------

    def __call__(self, input: Any = None, config: Any = None) -> MockAIMessage:
        """
        支持 `prompt | llm` 这种 LangChain 链式写法。

        为什么要这个方法：Runnable.__or__ 只接受 Runnable / 可调用对象 / dict，
        普通类实例会被拒掉并抛
        "Expected a Runnable, callable or dict"。业务代码里有
        `chain = INTERPRET_PROMPT | llm` 这样的写法（真实模型是 Runnable 所以没事），
        模拟模型必须自己也"看起来像可调用对象"，才能等价地参与管道。
        """
        return self.invoke(input, config)

    def __repr__(self) -> str:
        return f"MockChatModel(label={self.label!r}, temperature={self.temperature})"


def get_mock_chat_model(
    temperature: float = 0.0,
    max_tokens: int = 2000,
    label: str = "mock-chat",
) -> MockChatModel:
    """
    获取模拟对话模型。

    参数:
        temperature 采样温度，透传给 MockChatModel
        max_tokens  回复上限，透传
        label       标识，建议填节点名，排查问题时能看出是谁在调用

    返回:
        MockChatModel 实例
    """
    return MockChatModel(temperature=temperature, max_tokens=max_tokens, label=label)


# ===============================================================
# 五、模拟向量化模型
# ===============================================================


class MockEmbeddings:
    """
    冒充 OpenAIEmbeddings。

    实现思路（这是一个真实可用的"文本哈希向量化"方案，不只是占位）：
        1. 把文本按字符切分成二元组（bigram），中文没有空格，
           用字级别的二元组比按空格分词更通用
        2. 每个二元组用 md5 哈希成一个整数，取模映射到向量某一维
        3. 该维度累加 1
        4. 最后做 L2 归一化，使余弦相似度可以直接用点积计算

    为什么这样做是有意义的：
        相同的二元组会落到相同的维度上，所以
        "新能源汽车出海" 和 "新能源汽车出口" 的向量会高度相似，
        "今天天气不错" 则完全不同。
        这意味着 Mock 模式下**语义去重功能是真的能工作的**——
        你可以看到重复资料被真正过滤掉，而不是走个过场。

    局限（必须说清楚，面试时别吹过头）：
        它捕捉的是字面重叠，不理解同义词。
        "汽车" 和 "车辆" 在它眼里毫不相关。
        所以它只适合离线跑通流程，真实语义理解仍需真正的 embedding 模型。
    """

    def __init__(self, dim: int | None = None) -> None:
        """
        参数:
            dim 向量维度。传 None 则使用 .env 里的 EMBEDDING_DIM。
                显式传值的好处是测试时可以指定一个小维度（如 64），跑得更快。
        """
        # 维度必须与 Chroma 建库时声明的一致，否则写入会报维度不匹配
        self.dim = int(dim if dim is not None else settings.embedding_dim)

    def _embed_one(self, text: str) -> list[float]:
        """
        把单条文本转成向量。

        参数:
            text 输入文本

        返回:
            长度为 self.dim 的浮点列表（已 L2 归一化）
        """
        # 初始化全零向量。用 float 而不是 int，避免后续除法产生整数截断
        vector = [0.0] * self.dim

        # 统一小写并去掉空白，让 "AI" 和 "ai" 落到同一维度
        normalized = re.sub(r"\s+", "", (text or "").lower())

        # 文本为空时返回零向量。零向量无法归一化（除零），
        # 所以直接返回，避免抛 ZeroDivisionError
        if not normalized:
            return vector

        # ---- 第一步：切二元组 ----
        # 用滑动窗口取相邻两个字符：len 为 n 的文本生成 n-1 个二元组。
        # 只有 1 个字符时退化成单元组，也要处理，否则向量全为零。
        if len(normalized) == 1:
            grams = [normalized]
        else:
            grams = [normalized[i : i + 2] for i in range(len(normalized) - 1)]

        # ---- 第二步：哈希落桶 ----
        for gram in grams:
            # md5 结果转成整数再取模。用 hashlib 而不是内置 hash()，
            # 因为内置 hash() 对字符串在不同进程间结果不同（有随机盐），
            # 会导致"重启后同一文本算出不同向量"，索引对不上。
            digest = hashlib.md5(gram.encode("utf-8")).hexdigest()
            index = int(digest, 16) % self.dim
            vector[index] += 1.0

        # ---- 第三步：L2 归一化 ----
        # 归一化后，两个向量的余弦相似度 = 点积，Chroma 内部也是按余弦算的
        norm = sum(value * value for value in vector) ** 0.5
        if norm > 0:
            vector = [value / norm for value in vector]

        return vector

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """
        批量向量化（Chroma 写入时调用）。

        参数:
            texts 文本列表

        返回:
            与输入等长的向量列表
        """
        return [self._embed_one(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        """
        单条查询向量化（检索时调用）。

        参数:
            text 查询文本

        返回:
            一个向量
        """
        return self._embed_one(text)


def get_mock_embeddings(dim: int | None = None) -> MockEmbeddings:
    """
    获取模拟向量化模型。

    参数:
        dim 向量维度，None 表示使用 .env 配置的 EMBEDDING_DIM

    返回:
        MockEmbeddings 实例
    """
    return MockEmbeddings(dim=dim)


# ===============================================================
# 六、内部辅助函数
# ===============================================================


def _to_text(input: Any) -> str:
    """
    把各种形态的 Prompt 统一转成纯文本。

    为什么要这个函数：LangChain 里传给模型的 input 有三种常见形态，
    如果直接 str(input) 会得到一堆对象内存地址，取不到关键词。

        "直接字符串"
        [SystemMessage(...), HumanMessage(...)]   消息列表
        ChatPromptValue(...)                      模板渲染结果

    参数:
        input 任意形态的 Prompt

    返回:
        拼接后的纯文本
    """
    # 已经是字符串，直接用
    if isinstance(input, str):
        return input

    # 消息列表：逐个取 .content 拼接
    if isinstance(input, (list, tuple)):
        parts: list[str] = []
        for item in input:
            content = getattr(item, "content", None)
            # content 可能是字符串，也可能是一段结构化内容（列表），逐层兜底
            if isinstance(content, str):
                parts.append(content)
            elif isinstance(content, list):
                parts.append(str(content))
            else:
                parts.append(str(item))
        return "\n".join(parts)

    # PromptValue / 其他对象：优先取 .to_string()（LangChain 的标准方法），
    # 没有就退回 str()
    to_string = getattr(input, "to_string", None)
    if callable(to_string):
        try:
            return to_string()
        except Exception:  # noqa: BLE001 - Mock 层要足够健壮，绝不能因为取文本失败而中断流程
            pass

    return str(input)


def _estimate_tokens(text: str) -> int:
    """
    粗略估算 token 数。

    为什么是"估算"：真实 token 数要跑分词器才知道。
    对中文来说，1 个汉字大约 1-1.5 个 token；对英文，1 个 token 约 4 个字符。
    这里用 "字符数 / 3" 做一个粗估，量级正确即可——
    用量的作用主要是"看趋势、做预算熔断"，不需要精确到最后一位。

    参数:
        text 文本

    返回:
        估算的 token 数（最少 1，避免出现全 0 导致用量统计看起来没生效）
    """
    return max(1, len(text or "") // 3)


def _generate_text_for_prompt(prompt: str) -> str:
    """
    根据 Prompt 内容返回对应的占位文本。

    参数:
        prompt 输入 Prompt 全文

    返回:
        占位文本（Markdown 格式）
    """
    # 判断顺序很重要：先判断更具体的关键词。
    # 比如撰写 Prompt 里也会出现"分析"二字，如果先判断"分析"就会走错分支。
    if any(kw in prompt for kw in ("研究报告", "撰写", "正文", "Markdown", "报告初稿")):
        return _mock_report_draft()

    if any(kw in prompt for kw in ("审核", "评审", "打分", "评价")):
        return "（MOCK）审核意见：整体结构完整，但第 2 节数据支撑不足，建议补充来源后修订。"

    if any(kw in prompt for kw in ("分析", "提炼", "归纳", "要点")):
        return "（MOCK）分析结论：资料显示该领域处于快速增长期，主要驱动因素为政策支持与成本下降。"

    if any(kw in prompt for kw in ("摘要", "小结", "总结", "概括")):
        return "（MOCK）小结：本轮共覆盖 3 个子问题，其中 2 个找到充分证据，1 个证据不足。"

    if any(kw in prompt for kw in ("规划", "大纲", "拆解", "子问题")):
        # 这个分支正常不会走到（规划节点用的是结构化输出），
        # 保留是为了万一有人用普通 invoke 做规划时的兜底
        return "（MOCK）规划结果：将主题拆解为 4 个子问题，覆盖市场、政策、竞争、趋势四个维度。"

    # 兜底文案
    return f"（MOCK）这是模拟模型的占位回复。未识别的 Prompt 片段：{prompt[:80]}"


def _mock_report_draft() -> str:
    """
    生成一份结构完整的假报告初稿。

    为什么要费劲写一份"看起来像真的"报告：
        下游的审核节点会检查"有没有章节标题""有没有引用标注""字数够不够"。
        如果 Mock 返回一句"这是占位文本"，审核节点的所有规则都会判失败，
        流程图就会一直回退，你看不到正常路径。
        造一份结构合法的假报告，才能让审核逻辑被真正执行一遍。

    为什么每一节都要写够 100 字以上（这不是凑字数，是必要的）：
        审核节点里有一条**确定性**规则：某一节少于 60 字就判定为"内容缺失"，
        并把模型的 pass 结论强制纠正为 revise。
        如果 mock 报告的章节写得太短，那么无论审核模型怎么判，
        代码都会一直把结论纠正成 revise，
        结果就是流程永远走到"达到迭代上限 → 降级放弃"，
        你**永远看不到 finalize（定稿）那条正常路径**。
        所以 mock 夹具的职责是"结构完全合法"，让正常路径可被观察；
        想考察反馈循环，靠的是 _MOCK_SCENARIO 里的审核剧情
        （前 N 次判 revise），而不是靠让夹具本身不合格。

    返回:
        Markdown 格式的报告正文
    """
    return """# 示例研究报告（MOCK）

> 本文由离线模拟模型生成，仅用于验证流程可跑通，**不具备任何事实参考价值**。
> 配置真实 `LLM_API_KEY` 并设置 `LLM_MOCK=false` 后会替换为模型输出。

## 1. 研究背景

本节说明研究主题的背景与意义 [S1]。根据检索到的资料，该领域目前处于快速发展阶段，
相关政策与市场环境均在持续变化，参与者的数量在过去两年里明显增加 [S2]。
需要特别说明的是，离线样例资料只覆盖公开可获取的信息，不包含任何企业内部数据，
因此本节以及后续各节的判断都只适用于宏观层面，不构成投资或经营建议。

## 2. 市场格局

本节梳理主要参与方与市场分布情况 [S1][S3]。资料显示，头部企业的集中度较高，
前三名合计占据了相当大的份额，但新兴参与者的增长速度明显快于行业平均水平，
竞争格局尚未最终定型 [S3]。从区域分布来看，不同市场的成熟度差异较大，
发达市场的渗透率已经处于较高水平，而新兴市场仍处在早期放量阶段 [S1]。
这种区域差异意味着，同一条竞争策略在不同市场里的效果可能完全不同。

## 3. 关键驱动因素

驱动因素可以归纳为三点：

1. 政策支持力度加大，相关补贴与准入规则持续调整 [S2]
2. 上游成本持续下降，使得整体价格带不断下移 [S1]
3. 下游需求结构变化，用户对使用成本与效率的关注度上升 [S4]

上述三点当中，前两点有多个来源互相印证，第三点的证据相对单一，
因此本节整体判断的信心等级定为中等 [S4]。若后续能补充更多一手调研资料，
第三点的确定性还有提升空间。

## 4. 风险与不确定性

需要关注的风险包括政策口径变化、供应链波动以及市场增速不及预期 [S4]。
此外，行业标准尚未统一，不同参与者的技术路线存在明显差异，
这会在中期内增加竞争的不确定性，也可能导致部分前期投入难以按期回收 [S3]。
建议在后续跟踪中重点关注监管口径的变化，以及主要参与者的产能投放节奏。

## 5. 结论

综合来看，该领域具备中长期增长的基础，但短期波动风险不容忽视 [S1][S2][S3][S4]。
在资料覆盖范围有限的前提下，本文给出的判断属于方向性结论，
具体到细分市场与具体参与者，仍然需要更细颗粒度的数据支撑才能下结论。
后续可通过补充一手调研资料来提升结论的确定性。

## 参考资料

- [S1] 示例来源一（MOCK）
- [S2] 示例来源二（MOCK）
- [S3] 示例来源三（MOCK）
- [S4] 示例来源四（MOCK）
"""


# ---------------------------------------------------------------
# 自测入口：用法 python -m app.core.mock_llm
# ---------------------------------------------------------------
if __name__ == "__main__":
    print("=" * 68)
    print("离线模拟层自检")
    print("=" * 68)

    # ---- 1. 普通文本生成 ----
    llm = get_mock_chat_model(label="self-test")
    message = llm.invoke("请撰写一份研究报告的正文")
    print("[1] 普通文本生成：")
    print(f"    返回类型：{type(message).__name__}")
    print(f"    内容前 80 字：{message.content[:80].replace(chr(10), ' ')}")
    print(f"    用量信息：{message.usage_metadata}")

    # ---- 2. 结构化输出（用一个内联的小模型演示）----
    class DemoSection(BaseModel):
        """演示用的章节模型。"""
        section_id: str
        title: str
        target_words: int

    class DemoPlan(BaseModel):
        """演示用的大纲模型。"""
        research_questions: list[str]
        outline: list[DemoSection]
        difficulty: Literal["easy", "medium", "hard"]

    print("-" * 68)
    print("[2] 结构化输出：")
    runnable = llm.with_structured_output(DemoPlan, include_raw=True)
    result = runnable.invoke("请规划这份报告")
    plan = result["parsed"]
    print(f"    子问题数量：{len(plan.research_questions)}")
    print(f"    第一个子问题：{plan.research_questions[0]}")
    print(f"    大纲章节数：{len(plan.outline)}")
    print(f"    第一章标题：{plan.outline[0].title}")
    print(f"    Literal 字段取值：{plan.difficulty}（必须是合法枚举值，Mock 已自动挑选）")

    # ---- 3. 审核剧情验证 ----
    print("-" * 68)
    print("[3] 审核剧情（前 1 次不通过，之后通过）：")

    class DemoReview(BaseModel):
        """演示用的审核结果模型。"""
        verdict: str
        score: int
        passed: bool
        issues: list[str]
        revision_instructions: list[str]

    reset_mock_scenario(fail_first_n=1)
    reviewer = llm.with_structured_output(DemoReview)
    for i in range(1, 4):
        outcome = reviewer.invoke("请审核这份报告")
        print(
            f"    第 {i} 次审核 -> verdict={outcome.verdict}, score={outcome.score}, "
            f"passed={outcome.passed}, 问题数={len(outcome.issues)}"
        )

    # ---- 4. 模拟向量化：验证"相似文本向量相似" ----
    print("-" * 68)
    print("[4] 模拟向量化（验证语义相近的文本向量也相近）：")
    embeddings = get_mock_embeddings()

    def cosine(a: list[float], b: list[float]) -> float:
        """余弦相似度。因为向量已归一化，点积就是余弦值。"""
        return sum(x * y for x, y in zip(a, b))

    vec_a = embeddings.embed_query("2024年新能源汽车出海情况")
    vec_b = embeddings.embed_query("2024年新能源汽车出口情况")
    vec_c = embeddings.embed_query("今天天气不错适合出门散步")

    print(f"    向量维度：{len(vec_a)}")
    print(f"    '出海' vs '出口' 相似度：{cosine(vec_a, vec_b):.4f}（应当明显偏高）")
    print(f"    '出海' vs '天气' 相似度：{cosine(vec_a, vec_c):.4f}（应当接近 0）")

    print("=" * 68)
    print("自检完成。把 .env 里的 LLM_MOCK 设为 true 即可离线跑通全部流程。")
