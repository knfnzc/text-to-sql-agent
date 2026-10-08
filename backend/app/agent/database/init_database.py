"""
建表并灌入测试数据。

本模块做三件事：
    1. 按 schema_definition.py 的定义创建 5 张表（会先删旧表，可反复运行）
    2. 生成一批"有业务规律"的测试数据，让后续查询能跑出有意义的图表
    3. 用几条抽样查询验证数据确实灌进去了

为什么造数据要"有规律"而不是纯随机：
    如果订单金额、日期、状态全是均匀随机，那么"最近三个月销售额趋势"这类查询
    画出来的图会是一条平线，看不出效果，也没法验证 SQL 写得对不对。
    所以这里刻意制造了季节性（旺季订单多）、等级差异（金牌客户客单价高）、
    类目偏好（数码客单价高）这些规律，查出来的图才像真实业务。

可重复性说明：
    固定了随机种子，所以每次生成的数据完全一样。
    这对验证很重要——不然你改了 Prompt 之后效果变了，分不清是 Prompt 的功劳还是数据变了。

运行方式：
    python -m app.agent.database.init_database
"""

import random
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import func, insert, select

from app.core.config import ensure_data_dirs, settings
from app.core.db import execute_query, get_engine

from app.agent.database.schema_definition import (
    customers,
    metadata,
    order_items,
    orders,
    products,
    refunds,
)

# ===============================================================
# 生成参数
# ===============================================================

# 随机种子。固定值保证每次生成的数据完全相同，便于对比不同 Prompt 的效果。
# 改这个数字就能得到一套全新的测试数据。
RANDOM_SEED: int = 20240914

# 各类数据的生成条数。
# 数量级选择说明：订单上千条才能让聚合查询有统计意义，
# 但也不必太大，否则建库变慢，反而影响学习节奏。
CUSTOMER_COUNT: int = 200
PRODUCT_COUNT: int = 60
ORDER_COUNT: int = 1200

# 数据时间范围。留出约 20 个月，这样"最近一个月""最近一季度""去年同期"都能查到数据。
DATA_START_DATE: date = date(2025, 1, 1)
DATA_END_DATE: date = date(2026, 9, 10)


# ===============================================================
# 取值池：模拟真实业务的数据分布
# ===============================================================

# 省份与主要城市。用"省份 -> 城市列表"的结构，保证城市和省份是匹配的，
# 避免出现"广东省-哈尔滨"这种不合理组合导致查询结果奇怪
PROVINCE_CITIES: dict[str, list[str]] = {
    "广东省": ["广州市", "深圳市", "东莞市", "佛山市"],
    "浙江省": ["杭州市", "宁波市", "温州市", "嘉兴市"],
    "江苏省": ["南京市", "苏州市", "无锡市", "常州市"],
    "北京市": ["北京市"],
    "上海市": ["上海市"],
    "四川省": ["成都市", "绵阳市"],
    "湖北省": ["武汉市", "宜昌市"],
    "山东省": ["青岛市", "济南市"],
}

# 会员等级及其权重。
# 权重设计意图：让"普通"占多数（符合真实业务的金字塔结构），
# 同时保证高等级有足够样本量，按等级分组分析时不会出现只有 1 个人的组。
# 每个元素是 (等级, 权重)，权重越大越容易被抽中
MEMBER_LEVELS: list[tuple[str, int]] = [
    ("普通", 50),
    ("银牌", 25),
    ("金牌", 18),
    ("铂金", 7),
]

# 商品类目与品牌。
# 每个类目给出品牌池，避免出现"食品类-苹果"这种跨类目的错误组合
CATEGORY_BRANDS: dict[str, list[str]] = {
    "数码": ["华为", "小米", "苹果", "OPPO", "vivo"],
    "家电": ["美的", "格力", "海尔", "西门子", "松下"],
    "服饰": ["优衣库", "李宁", "安踏", "太平鸟", "海澜之家"],
    "食品": ["三只松鼠", "良品铺子", "蒙牛", "伊利", "五芳斋"],
    "家居": ["宜家", "林氏木业", "顾家家居", "网易严选", "全友"],
}

# 各类目的价格区间（单位：元）。
# 区间差异是刻意做的：数码家电客单价高，食品低。
# 这样"各品类销售额占比"才会呈现明显的结构差异，饼图才有看点。
CATEGORY_PRICE_RANGE: dict[str, tuple[int, int]] = {
    "数码": (800, 8000),
    "家电": (500, 6000),
    "服饰": (99, 899),
    "食品": (19, 299),
    "家居": (199, 2999),
}

# 商品名称后缀，用来拼出"华为 Mate60 Pro"这类像样的商品名
PRODUCT_NAME_SUFFIX: list[str] = [
    "入门版", "标准版", "高配版", "旗舰版", "Pro", "Max", "青春版", "经典款",
]

# 订单状态及其权重。
# 设计意图：已完成占大头（便于统计销售额），同时保留一定比例的已取消和待支付，
# 用来测试"业务口径"——模型会不会正确地排除不该统计的订单。
ORDER_STATUSES: list[tuple[str, int]] = [
    ("已完成", 55),
    ("已发货", 15),
    ("已支付", 12),
    ("待支付", 10),
    ("已取消", 8),
]

# 支付方式权重
PAYMENT_METHODS: list[tuple[str, int]] = [
    ("支付宝", 40),
    ("微信", 35),
    ("银行卡", 15),
    ("余额", 10),
]

# 下单渠道权重
CHANNELS: list[tuple[str, int]] = [
    ("App", 50),
    ("小程序", 32),
    ("PC官网", 18),
]

# 退款原因权重
REFUND_REASONS: list[tuple[str, int]] = [
    ("七天无理由", 40),
    ("质量问题", 25),
    ("发错货", 15),
    ("物流延迟", 12),
    ("其他", 8),
]

# 中文姓氏与名字用字，用于生成客户姓名
SURNAMES: list[str] = list("王李张刘陈杨黄赵周吴徐孙马朱胡林郭何高罗郑梁谢宋唐许韩冯邓曹彭曾肖田董袁潘于蒋蔡余杜叶程苏魏吕丁任沈姚卢姜崔钟谭陆汪范金石廖贾夏韦付方白邹孟熊秦邱江尹薛闫段雷侯龙史陶黎贺顾毛郝龚邵万钱严覃武戴莫孔向汤")
GIVEN_CHARS: list[str] = list("伟芳娜秀英敏静丽强磊军洋勇艳杰娟涛明超秀霞平刚桂英建华文斌辉力宁鹏宇晨阳雨欣佳琳思远子墨浩然若曦梓涵俊杰雅琴雪梅建国志强晓东春燕秋月冬梅海燕")


# ===============================================================
# 工具函数
# ===============================================================


def _weighted_choice(options: list[tuple[str, int]]) -> str:
    """
    按权重随机抽取一个选项。

    参数:
        options 形如 [("已完成", 55), ("已取消", 8)] 的列表，第二项是权重

    返回:
        被抽中的字符串

    实现说明：
        用 random.choices 而不是 random.choice，前者支持权重。
        k=1 表示抽一个，取出列表第一个元素。
    """
    values = [item[0] for item in options]
    weights = [item[1] for item in options]
    return random.choices(values, weights=weights, k=1)[0]


def _random_date(start: date, end: date) -> date:
    """
    在指定区间内随机取一个日期。

    参数:
        start 起始日期（含）
        end   结束日期（含）

    返回:
        区间内的随机日期
    """
    # 换算成"从 start 起偏移多少天"，再随机取偏移量。
    # 用 (end - start).days 得到区间总天数
    offset = random.randint(0, max((end - start).days, 0))
    return start + timedelta(days=offset)


def _make_order_date() -> date:
    """
    生成一个"有季节性规律"的下单日期。

    业务规律设计：
        电商有明显的大促节点。这里模拟两个旺季——每年 6 月和 11 月，
        这两月被抽中的概率是其他月份的 3 倍。
        这样"按月统计销售额"画出来会有两个明显的峰，图表才有信息量。

    返回:
        一个下单日期
    """
    while True:
        # 先在总区间里随机取一天
        candidate = _random_date(DATA_START_DATE, DATA_END_DATE)

        # 6 月和 11 月是大促月，直接接受（相当于提高被抽中的概率）
        if candidate.month in (6, 11):
            return candidate

        # 其他月份有 1/3 的概率被接受，另外 2/3 概率重新抽，
        # 这样就实现了"大促月权重约 3 倍"的效果
        if random.random() < 1 / 3:
            return candidate


# ===============================================================
# 建表
# ===============================================================


def create_all_tables(drop_first: bool = True) -> None:
    """
    创建所有表。

    参数:
        drop_first 是否先删除已有的表。
                   True 表示每次运行都重建（数据会被清空，适合反复调试）；
                   False 表示表已存在就跳过（保留原有数据）。

    返回:
        无
    """
    engine = get_engine()

    if drop_first:
        # drop_all 会按外键依赖的逆序删除，不用手动排序
        metadata.drop_all(engine)
        print("已删除旧表（如果存在）")

    # create_all 同样会处理依赖顺序，并且对已存在的表会自动跳过
    metadata.create_all(engine)
    print(f"已创建表：{list(metadata.tables.keys())}")


# ===============================================================
# 生成数据
# ===============================================================


def build_customers(count: int) -> list[dict]:
    """
    生成客户数据。

    参数:
        count 生成条数

    返回:
        可直接用于批量插入的字典列表
    """
    rows: list[dict] = []

    # provinces 和 city_pool 提前算好，避免在循环里反复取 keys
    provinces = list(PROVINCE_CITIES.keys())

    for customer_id in range(1, count + 1):
        # 随机选省份，再从该省份的城市列表里选城市，保证省市匹配
        province = random.choice(provinces)
        city = random.choice(PROVINCE_CITIES[province])

        # 注册日期：用普通随机（不加大促权重），注册行为没有明显季节性
        register_date = _random_date(date(2024, 1, 1), DATA_END_DATE)

        # 年龄：主力客群 18-55 岁。
        # 留出约 5% 的概率为 None，模拟"未填写年龄"的真实情况，
        # 这样测试问题里涉及年龄时，模型需要正确处理 NULL
        age = random.randint(18, 55) if random.random() > 0.05 else None

        rows.append(
            {
                "customer_id": customer_id,
                "name": random.choice(SURNAMES) + "".join(
                    random.choice(GIVEN_CHARS) for _ in range(random.choice([1, 2]))
                ),
                # 用客户 ID 拼邮箱，保证唯一约束不会冲突
                "email": f"user{customer_id:04d}@example.com",
                "city": city,
                "province": province,
                "register_date": register_date,
                "level": _weighted_choice(MEMBER_LEVELS),
                "age": age,
            }
        )

    return rows


def build_products(count: int) -> list[dict]:
    """
    生成商品数据。

    参数:
        count 生成条数

    返回:
        字典列表
    """
    rows: list[dict] = []
    categories = list(CATEGORY_BRANDS.keys())

    for product_id in range(1, count + 1):
        # 类目用轮转方式分配（而不是纯随机），保证每个类目下的商品数量接近，
        # 做"各品类销售额对比"时不会出现某个类目只有 2 个商品的尴尬情况
        category = categories[product_id % len(categories)]
        brand = random.choice(CATEGORY_BRANDS[category])

        # 在该类目的价格区间内取价，保留两位小数
        low, high = CATEGORY_PRICE_RANGE[category]
        unit_price = Decimal(round(random.uniform(low, high), 2))

        rows.append(
            {
                "product_id": product_id,
                "product_name": f"{brand} {category}商品{product_id:03d} "
                f"{random.choice(PRODUCT_NAME_SUFFIX)}",
                "category": category,
                "brand": brand,
                "unit_price": unit_price,
                # 库存 0-999，可能为 0（缺货），可测试"库存不足"类查询
                "stock_qty": random.randint(0, 999),
                "created_at": _random_date(date(2024, 1, 1), date(2026, 6, 30)),
            }
        )

    return rows


def build_orders_and_items(
    order_count: int,
    customer_rows: list[dict],
    product_rows: list[dict],
) -> tuple[list[dict], list[dict]]:
    """
    生成订单主表和明细表数据。

    这两张表必须一起生成，因为明细依赖订单的 ID 和金额。

    参数:
        order_count    订单数量
        customer_rows  已生成的客户数据，用于随机挑客户
        product_rows   已生成的商品数据，用于随机挑商品并取单价

    返回:
        (订单行列表, 明细行列表)
    """
    order_rows: list[dict] = []
    item_rows: list[dict] = []

    # 白金/金牌客户的 ID 集合，用于让高等级客户下单更频繁、客单价更高。
    # 这样做是为了制造"高等级客户贡献更多销售额"这个业务结论，
    # 让按会员等级分析的查询能得出有意义的洞察
    high_level_ids = [
        row["customer_id"]
        for row in customer_rows
        if row["level"] in ("金牌", "铂金")
    ]

    # item_id 需要全局唯一，用独立计数器而不用订单内序号，避免主键冲突
    item_id = 1

    for order_id in range(1, order_count + 1):
        # 70% 的订单来自高等级客户（如果高等级客户集合非空），制造客户分层差异
        if high_level_ids and random.random() < 0.7:
            customer_id = random.choice(high_level_ids)
        else:
            customer_id = random.choice(customer_rows)["customer_id"]

        order_date = _make_order_date()
        status = _weighted_choice(ORDER_STATUSES)

        # 每个订单买 1 到 4 种商品
        item_count = random.randint(1, 4)
        # sample 不重复抽样，保证一个订单里不会出现两条相同商品的明细
        chosen_products = random.sample(product_rows, item_count)

        order_total = Decimal("0.00")

        for product in chosen_products:
            quantity = random.randint(1, 3)

            # 成交价在标准单价基础上打 85 折到原价之间。
            # 这样明细里的成交价与商品表标准价不同，更贴近真实促销场景，
            # 也让"用哪个价格算销售额"成为一个需要想清楚的口径问题
            discount_rate = Decimal(str(round(random.uniform(0.85, 1.0), 2)))
            deal_price = (product["unit_price"] * discount_rate).quantize(Decimal("0.01"))

            # 约 30% 的明细享受额外折扣
            discount = (
                (deal_price * Decimal("0.1")).quantize(Decimal("0.01"))
                if random.random() < 0.3
                else Decimal("0.00")
            )

            item_rows.append(
                {
                    "item_id": item_id,
                    "order_id": order_id,
                    "product_id": product["product_id"],
                    "quantity": quantity,
                    "unit_price": deal_price,
                    "discount": discount,
                }
            )

            # 累加订单金额：数量 × 成交价 - 折扣
            order_total += deal_price * quantity - discount
            item_id += 1

        order_rows.append(
            {
                "order_id": order_id,
                "customer_id": customer_id,
                "order_date": order_date,
                "status": status,
                "total_amount": order_total.quantize(Decimal("0.01")),
                "payment_method": _weighted_choice(PAYMENT_METHODS),
                "channel": _weighted_choice(CHANNELS),
            }
        )

    return order_rows, item_rows


def build_refunds(order_rows: list[dict]) -> list[dict]:
    """
    生成退款数据。

    重要设计：只给"已完成"和"已发货"的订单生成退款记录，且只有约 8% 的订单有退款。
    这叫"部分关联"——不是每个订单都有退款。这样测试问题里问"有退款的订单"
    时，模型必须用 LEFT JOIN 或 EXISTS 才能写对，是很好的区分度设计。

    参数:
        order_rows 已生成的订单数据

    返回:
        退款行列表
    """
    rows: list[dict] = []
    refund_id = 1

    for order in order_rows:
        # 只有进入过履约流程的订单才可能退款，待支付和已取消的不会
        if order["status"] not in ("已完成", "已发货"):
            continue

        # 约 8% 的概率产生退款
        if random.random() > 0.08:
            continue

        # 退款金额取订单金额的 30% 到 100%，模拟部分退款和全额退款
        ratio = Decimal(str(round(random.uniform(0.3, 1.0), 2)))
        refund_amount = (order["total_amount"] * ratio).quantize(Decimal("0.01"))

        # 退款日期在下单日期之后 1 到 30 天
        refund_date = order["order_date"] + timedelta(days=random.randint(1, 30))

        rows.append(
            {
                "refund_id": refund_id,
                "order_id": order["order_id"],
                "refund_date": refund_date,
                "refund_amount": refund_amount,
                "refund_reason": _weighted_choice(REFUND_REASONS),
            }
        )
        refund_id += 1

    return rows


# ===============================================================
# 写入数据库
# ===============================================================


def insert_rows(table_name: str, rows: list[dict]) -> int:
    """
    把一批数据写入指定表。

    参数:
        table_name 目标表名
        rows       数据字典列表

    返回:
        实际写入的行数
    """
    if not rows:
        return 0

    # 从模块全局取 Table 对象
    table = {
        "customers": customers,
        "products": products,
        "orders": orders,
        "order_items": order_items,
        "refunds": refunds,
    }[table_name]

    engine = get_engine()
    # 用 with engine.begin() 保证整批数据要么全成功要么全回滚，
    # 不会出现"灌了一半失败导致数据不完整"的情况
    with engine.begin() as conn:
        conn.execute(insert(table), rows)

    return len(rows)


def init_database(drop_first: bool = True) -> None:
    """
    初始化数据库：建表 + 灌数据。整个建库与数据准备的一键入口。

    参数:
        drop_first 是否先删除旧表，默认 True（每次重新生成一套干净数据）

    返回:
        无
    """
    # 固定随机种子，保证数据可复现。
    # 这行必须在任何随机调用之前执行
    random.seed(RANDOM_SEED)

    # 确保 data 目录存在。SQLite 不会自动创建目录，缺目录会报 "unable to open database file"
    ensure_data_dirs()

    print("=" * 70)
    print("初始化示例数据库")
    print("=" * 70)
    print(f"数据库地址：{settings.db_url}")
    print(f"随机种子  ：{RANDOM_SEED}（固定值，保证数据可复现）")
    print("-" * 70)

    # 第一步：建表
    create_all_tables(drop_first=drop_first)

    # 第二步：生成数据
    print("-" * 70)
    print("正在生成测试数据...")
    customer_rows = build_customers(CUSTOMER_COUNT)
    product_rows = build_products(PRODUCT_COUNT)
    order_rows, item_rows = build_orders_and_items(
        ORDER_COUNT, customer_rows, product_rows
    )
    refund_rows = build_refunds(order_rows)

    # 第三步：写入。
    # 顺序很重要：必须先写被依赖的表（customers、products），
    # 再写依赖它们的表（orders、order_items、refunds），否则外键约束会失败
    print("-" * 70)
    print("正在写入数据库...")
    inserted = {
        "customers": insert_rows("customers", customer_rows),
        "products": insert_rows("products", product_rows),
        "orders": insert_rows("orders", order_rows),
        "order_items": insert_rows("order_items", item_rows),
        "refunds": insert_rows("refunds", refund_rows),
    }

    for table_name, count in inserted.items():
        print(f"  {table_name:<12} 写入 {count:>5} 行")

    # 第四步：验证
    print("-" * 70)
    verify_data()

    print("-" * 70)
    print("完成。下一步可以运行测试问题集：")
    print("  python -m app.agent.database.test_questions")


def verify_data() -> None:
    """
    验证数据是否灌入成功，并确认数据里确实存在业务规律。

    为什么要做这一步：
        灌完数据不验证，后面查询结果不对时你会怀疑是链路问题还是数据问题。
        这里用几条最简单的 SQL 把关键规律打印出来，先排除数据层面的疑问。

    返回:
        无
    """
    checks: list[tuple[str, str]] = [
        ("各表行数", """
            SELECT 'customers' AS 表名, COUNT(*) AS 行数 FROM customers
            UNION ALL SELECT 'products', COUNT(*) FROM products
            UNION ALL SELECT 'orders', COUNT(*) FROM orders
            UNION ALL SELECT 'order_items', COUNT(*) FROM order_items
            UNION ALL SELECT 'refunds', COUNT(*) FROM refunds
        """),
        ("订单状态分布", """
            SELECT status AS 状态, COUNT(*) AS 订单数
            FROM orders GROUP BY status ORDER BY 订单数 DESC
        """),
        ("按月销售额（验证季节性）", """
            SELECT substr(order_date, 1, 7) AS 月份,
                   ROUND(SUM(total_amount), 2) AS 销售额
            FROM orders
            WHERE status NOT IN ('已取消', '待支付')
            GROUP BY 月份 ORDER BY 月份
        """),
        ("各会员等级客户数", """
            SELECT level AS 会员等级, COUNT(*) AS 人数
            FROM customers GROUP BY level ORDER BY 人数 DESC
        """),
    ]

    for title, sql in checks:
        print(f"\n[验证] {title}")
        result = execute_query(sql)
        print(result.preview(max_rows=12))


# ---------------------------------------------------------------
# 自测入口
# ---------------------------------------------------------------
if __name__ == "__main__":
    init_database(drop_first=True)

    print("\n" + "=" * 70)
    print("数据抽样（确认字段可读、中文无乱码、金额精度正常）")
    print("=" * 70)

    for sql in [
        "SELECT * FROM customers LIMIT 3",
        "SELECT * FROM orders LIMIT 3",
        "SELECT * FROM order_items LIMIT 3",
    ]:
        print(f"\n{sql}")
        print(execute_query(sql).preview(max_rows=3))

    # 用 SQLAlchemy 的 count 再校验一次总行数，
    # 顺带演示"用 ORM 表达式而不是裸 SQL"的写法
    engine = get_engine()
    with engine.begin() as conn:
        total = conn.execute(select(func.count()).select_from(orders)).scalar()
    print(f"\norders 表总行数（SQLAlchemy 表达式方式）：{total}")
