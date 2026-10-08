"""
测试问题集。

这是本项目的"标尺"。没有它，你改了 Prompt 之后只能凭感觉说"好像变好了"，
面试官问"你怎么衡量 SQL 生成质量的"时也无话可说。

为什么要手写问题集而不是让模型自动生成：
    自动生成的问题往往偏向简单（都是单表查询），覆盖不到多表关联、
    时间计算、口径判断这些真正容易出错的场景。
    手写这 30 条花不了 1 小时，但它是你评估工作的基础。

每条问题的字段说明：
    id              编号，便于评估时定位是哪一个问题错了
    question        自然语言提问，就是用户会输入的那种话
    difficulty      难度：简单（单表）/ 中等（两表关联）/ 困难（三表或口径判断）
    tables          正确 SQL 应该用到哪些表。用来评估"表选择"是否准确
    key_points      这条问题的考察点。评估时人肉看 SQL 有没有满足这些条件
    category        问题类型，便于分类统计（哪类问题错得最多）

评估方法（初版 Streamlit 界面里实现了自动化部分）：
    1. 表选择准确率：生成的 SQL 用到的表 是否等于 tables 字段
    2. 执行成功率：SQL 能不能在数据库里跑通
    3. 结果合理性：把结果给人看，判断是否符合预期（这部分目前只能人工）
    4. 口径正确率：key_points 里的条件是否被满足

运行方式：
    python -m app.agent.database.test_questions
"""

from dataclasses import dataclass, field


@dataclass
class TestQuestion:
    """
    单条测试问题的数据结构。

    用 dataclass 而不是字典，好处是字段名有类型提示、写错字段名会立刻报错，
    而且 IDE 能自动补全。
    """

    # 问题编号
    id: int

    # 自然语言提问内容
    question: str

    # 难度等级：简单 / 中等 / 困难
    difficulty: str

    # 正确 SQL 需要用到的表名列表
    tables: list[str] = field(default_factory=list)

    # 考察点，评估时逐条核对
    key_points: list[str] = field(default_factory=list)

    # 问题类型，用于分类统计
    category: str = ""


# ===============================================================
# 问题集
# ===============================================================
# 覆盖设计说明：
#   简单题（10 条）：单表查询、聚合、排序、筛选。用来确认基础链路通畅。
#   中等题（12 条）：两表关联、分组聚合、时间范围筛选。这是真实使用中最常见的形态。
#   困难题（8 条）：三表关联、口径判断（排除取消订单）、空值处理、相对时间计算。
#                  这些是拉开差距的地方，也是面试时可以重点聊的部分。
#
# 关于"相对时间"的说明：
#   数据时间范围是 2025-01-01 到 2026-09-10。
#   涉及"最近""本月"这类表述时，正确做法是基于数据库里的最大日期推算，
#   而不是用 CURRENT_DATE。这一点写进了 key_points，评估时要检查。

TEST_QUESTIONS: list[TestQuestion] = [
    # ---------------------------------------------------------------
    # 简单：单表查询
    # ---------------------------------------------------------------
    TestQuestion(
        id=1,
        question="一共有多少个客户？",
        difficulty="简单",
        tables=["customers"],
        key_points=["使用 COUNT 聚合", "结果只有一个数值"],
        category="基础统计",
    ),
    TestQuestion(
        id=2,
        question="列出售价最高的 10 个商品名称和价格",
        difficulty="简单",
        tables=["products"],
        key_points=["按 unit_price 降序", "限制返回 10 行"],
        category="排序取前N",
    ),
    TestQuestion(
        id=3,
        question="有哪些商品类目？",
        difficulty="简单",
        tables=["products"],
        key_points=["对 category 去重", "不应出现重复类目"],
        category="去重列举",
    ),
    TestQuestion(
        id=4,
        question="订单状态一共有哪几种？各有多少笔订单？",
        difficulty="简单",
        tables=["orders"],
        key_points=["按 status 分组", "统计每组订单数"],
        category="分组统计",
    ),
    TestQuestion(
        id=5,
        question="库存为 0 的商品有多少个？",
        difficulty="简单",
        tables=["products"],
        key_points=["筛选 stock_qty = 0", "使用 COUNT"],
        category="条件筛选",
    ),
    TestQuestion(
        id=6,
        question="平均每个订单的金额是多少？",
        difficulty="简单",
        tables=["orders"],
        key_points=["对 total_amount 求平均", "结果保留合理的小数位"],
        category="基础统计",
    ),
    TestQuestion(
        id=7,
        question="按支付方式统计各自的订单数量，从多到少排列",
        difficulty="简单",
        tables=["orders"],
        key_points=["按 payment_method 分组", "按数量降序排列"],
        category="分组统计",
    ),
    TestQuestion(
        id=8,
        question="年龄最大的 5 位客户是谁？",
        difficulty="简单",
        tables=["customers"],
        key_points=["处理 age 为空的情况", "按 age 降序取 5 条"],
        category="排序取前N",
    ),
    TestQuestion(
        id=9,
        question="所有商品的平均价格是多少？",
        difficulty="简单",
        tables=["products"],
        key_points=["对 unit_price 求平均"],
        category="基础统计",
    ),
    TestQuestion(
        id=10,
        question="有哪些不同的下单渠道？",
        difficulty="简单",
        tables=["orders"],
        key_points=["对 channel 去重"],
        category="去重列举",
    ),

    # ---------------------------------------------------------------
    # 中等：两表关联
    # ---------------------------------------------------------------
    TestQuestion(
        id=11,
        question="每个省的客户数量是多少？按客户数从多到少排列",
        difficulty="中等",
        tables=["customers"],
        key_points=["按 province 分组", "降序排列"],
        category="分组统计",
    ),
    TestQuestion(
        id=12,
        question="销售额最高的 10 个订单，分别是哪个客户下的单？请给出客户姓名",
        difficulty="中等",
        tables=["orders", "customers"],
        key_points=["orders 关联 customers", "按 total_amount 降序取 10 条"],
        category="多表关联",
    ),
    TestQuestion(
        id=13,
        question="各个商品类目的总销售额分别是多少？",
        difficulty="中等",
        tables=["orders", "order_items", "products"],
        key_points=["三表关联", "排除已取消和待支付订单", "按类目汇总金额"],
        category="多表关联",
    ),
    TestQuestion(
        id=14,
        question="各会员等级的平均订单金额是多少？",
        difficulty="中等",
        tables=["orders", "customers"],
        key_points=["orders 关联 customers", "按 level 分组求平均"],
        category="多表关联",
    ),
    TestQuestion(
        id=15,
        question="销量最高的 10 个商品是哪些？卖出多少件？",
        difficulty="中等",
        tables=["order_items", "products"],
        key_points=["按 product_id 汇总 quantity", "降序取 10 条", "关联商品名"],
        category="多表关联",
    ),
    TestQuestion(
        id=16,
        question="按月统计 2025 年每个月的订单数量和销售额",
        difficulty="中等",
        tables=["orders"],
        key_points=["时间截断到月", "限定 2025 年", "同时输出订单数和金额"],
        category="时间趋势",
    ),
    TestQuestion(
        id=17,
        question="有哪些订单发生过退款？请给出订单号和退款金额",
        difficulty="中等",
        tables=["orders", "refunds"],
        key_points=["关联 refunds 表", "不能漏掉订单号"],
        category="多表关联",
    ),
    TestQuestion(
        id=18,
        question="各城市的客户数量，只看客户数超过 5 个的城市",
        difficulty="中等",
        tables=["customers"],
        key_points=["按 city 分组", "用 HAVING 过滤分组结果"],
        category="分组过滤",
    ),
    TestQuestion(
        id=19,
        question="App 渠道和 小程序 渠道的销售额分别是多少？",
        difficulty="中等",
        tables=["orders"],
        key_points=["按 channel 分组", "过滤指定两个渠道"],
        category="分组统计",
    ),
    TestQuestion(
        id=20,
        question="每个品牌的商品平均价格是多少？按均价从高到低排列",
        difficulty="中等",
        tables=["products"],
        key_points=["按 brand 分组对 unit_price 求平均", "降序排列"],
        category="分组统计",
    ),
    TestQuestion(
        id=21,
        question="退款原因都有哪些？各占多少笔？",
        difficulty="中等",
        tables=["refunds"],
        key_points=["按 refund_reason 分组", "统计数量"],
        category="分组统计",
    ),
    TestQuestion(
        id=22,
        question="购买了商品的订单中，平均每笔订单包含几种商品？",
        difficulty="中等",
        tables=["order_items"],
        key_points=["需要先按 order_id 统计条目数", "再对结果求平均"],
        category="嵌套聚合",
    ),

    # ---------------------------------------------------------------
    # 困难：三表关联、口径判断、相对时间
    # ---------------------------------------------------------------
    TestQuestion(
        id=23,
        question="最近三个月每个月的销售额是多少？",
        difficulty="困难",
        tables=["orders"],
        key_points=[
            "相对时间需基于数据中的最大日期计算，不能用 CURRENT_DATE",
            "排除已取消和待支付订单",
            "按月分组",
        ],
        category="相对时间",
    ),
    TestQuestion(
        id=24,
        question="各会员等级的客户中，谁的累计消费金额最高？列出前 5 名客户姓名和金额",
        difficulty="困难",
        tables=["orders", "customers"],
        key_points=["按客户汇总消费额", "排除无效订单", "降序取 5 条"],
        category="多表关联",
    ),
    TestQuestion(
        id=25,
        question="哪些商品从来没有被卖出过？",
        difficulty="困难",
        tables=["products", "order_items"],
        key_points=["必须用 LEFT JOIN 或 NOT EXISTS", "不能用 INNER JOIN"],
        category="反连接",
    ),
    TestQuestion(
        id=26,
        question="退款率最高的商品类目是哪个？退款率 = 退款金额 / 销售额",
        difficulty="困难",
        tables=["orders", "order_items", "products", "refunds"],
        key_points=["四表关联", "分别算出销售额和退款额", "注意除零保护"],
        category="复合指标",
    ),
    TestQuestion(
        id=27,
        question="2026 年各季度的销售额环比增长率是多少？",
        difficulty="困难",
        tables=["orders"],
        key_points=["季度截断", "用窗口函数或自关联计算环比", "处理首季度没有上期的情况"],
        category="同环比",
    ),
    TestQuestion(
        id=28,
        question="每个省份销售额排名前 3 的客户是谁？",
        difficulty="困难",
        tables=["orders", "customers"],
        key_points=["需要窗口函数或相关子查询", "分组内排序取前 3"],
        category="分组取前N",
    ),
    TestQuestion(
        id=29,
        question="复购客户（下过 2 笔以上订单）占了总客户数的多少比例？",
        difficulty="困难",
        tables=["orders", "customers"],
        key_points=["先筛选出订单数 >= 2 的客户", "再计算占比", "注意分母口径"],
        category="复合指标",
    ),
    TestQuestion(
        id=30,
        question="哪个品牌的商品退货最多？请给出品牌名和退货订单数",
        difficulty="困难",
        tables=["products", "order_items", "orders", "refunds"],
        key_points=["四表关联", "按品牌汇总退款订单数", "注意去重（一个订单可能有多条明细）"],
        category="多表关联",
    ),
]


# ===============================================================
# 辅助函数
# ===============================================================


def get_by_difficulty(difficulty: str) -> list[TestQuestion]:
    """
    按难度筛选问题。

    用途：分阶段测试。基础链路版先跑简单题确认链路通，状态图版再上困难题看校验层效果。

    参数:
        difficulty 难度等级，取值：简单 / 中等 / 困难

    返回:
        符合条件的问题列表
    """
    return [q for q in TEST_QUESTIONS if q.difficulty == difficulty]


def get_by_category(category: str) -> list[TestQuestion]:
    """
    按类型筛选问题。

    用途：定位薄弱环节。如果"多表关联"类错得多，
    说明 Schema 描述或关联关系说明写得不够好，需要重点优化那部分。

    参数:
        category 问题类型

    返回:
        符合条件的问题列表
    """
    return [q for q in TEST_QUESTIONS if q.category == category]


def summarize() -> dict[str, int]:
    """
    统计问题集构成。

    返回:
        形如 {"简单": 10, "中等": 12, "困难": 8} 的字典
    """
    stats: dict[str, int] = {}
    for question in TEST_QUESTIONS:
        stats[question.difficulty] = stats.get(question.difficulty, 0) + 1
    return stats


def export_as_evaluation_set() -> list[dict]:
    """
    导出成便于程序处理的格式，供初版 Streamlit 界面的自动化评估使用。

    返回:
        字典列表，每条包含问题、期望表、考察点等字段
    """
    return [
        {
            "id": q.id,
            "question": q.question,
            "difficulty": q.difficulty,
            "expected_tables": q.tables,
            "key_points": q.key_points,
            "category": q.category,
        }
        for q in TEST_QUESTIONS
    ]


# ---------------------------------------------------------------
# 自测入口
# ---------------------------------------------------------------
if __name__ == "__main__":
    print("=" * 78)
    print(f"测试问题集：共 {len(TEST_QUESTIONS)} 条")
    print("=" * 78)
    print(f"难度分布：{summarize()}")
    print("-" * 78)

    # 按难度分组打印，便于检查覆盖是否均衡
    for level in ("简单", "中等", "困难"):
        questions = get_by_difficulty(level)
        print(f"\n【{level}】共 {len(questions)} 条")
        for q in questions:
            # 表名列表拼成 "+" 形式，一眼能看出关联了几张表
            tables = "+".join(q.tables)
            print(f"  {q.id:>2}. {q.question}")
            print(f"      涉及表：{tables}")
            print(f"      考察点：{'; '.join(q.key_points)}")

    print("\n" + "-" * 78)
    print("按类型统计：")
    categories: dict[str, int] = {}
    for q in TEST_QUESTIONS:
        categories[q.category] = categories.get(q.category, 0) + 1
    for name, count in sorted(categories.items(), key=lambda item: -item[1]):
        print(f"  {name:<10} {count} 条")
