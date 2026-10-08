"""
数据库表结构定义。

本模块只做一件事——把"电商场景有哪些表、每张表有哪些字段、字段什么含义"定义清楚。
它是后面所有环节的源头：
    建表          用这里的 Table 对象
    灌测试数据    用这里的 Table 对象
    Schema 检索   用这里的 SCHEMA_DOCS 中文描述（这是让模型看懂库的关键）
    Prompt 组装   用 SCHEMA_DOCS 拼表结构说明

为什么要单独抽一个模块：
    很多入门教程把表结构写在 SQL 文件里，然后 Prompt 里再手写一遍中文说明，
    两处内容必须人工保持一致，改一处忘一处就会出错。
    这里用 Python 定义，表结构和中文描述放在一起，天然不会不同步。

设计说明：
    1. 选 5 张表覆盖"单表查询、两表关联、三表关联、聚合分组、时间筛选"五类典型场景，
       这样测试问题集才能覆盖足够的难度梯度。
    2. 字段名用英文，但每个字段都配了中文描述。真实企业库里字段常有拼音或缩写，
       把描述补全就是 Text-to-SQL 效果的关键，这一步不能省。
"""

from sqlalchemy import (
    Column,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    MetaData,
    Numeric,
    String,
    Table,
)

# ===============================================================
# 元数据容器
# ===============================================================

# MetaData 是 SQLAlchemy 的"表结构注册中心"。
# 所有 Table 对象都挂在它下面，调用 metadata.create_all(engine) 就能一次性建出所有表。
metadata = MetaData()


# ===============================================================
# 表定义
# ===============================================================
# 命名习惯说明：
#   主键统一叫 xxx_id，且用 Integer 自增，简单直观
#   金额字段统一用 Numeric(10, 2)，即最多 10 位数字、其中 2 位小数
#   （金额绝不能用 Float，浮点数在累加求和时会出现 0.1 + 0.2 = 0.30000000000000004 这类误差，
#     财务数据出错是严重问题）
#   日期用 Date，时间点用 DateTime，按业务语义选，不要一律用字符串


# ---------------------------------------------------------------
# 表 1：customers 客户表
# ---------------------------------------------------------------
customers = Table(
    # 表名
    "customers",
    metadata,
    # 客户 ID，主键自增
    Column("customer_id", Integer, primary_key=True, autoincrement=True, comment="客户ID"),
    # 客户姓名
    Column("name", String(50), nullable=False, comment="客户姓名"),
    # 邮箱，加唯一约束防止重复注册
    Column("email", String(100), unique=True, comment="邮箱"),
    # 所在城市
    Column("city", String(50), comment="城市"),
    # 所在省份，用于按大区做聚合分析
    Column("province", String(50), comment="省份"),
    # 注册日期，用于统计新增客户、做时间趋势分析
    Column("register_date", Date, comment="注册日期"),
    # 会员等级：普通 / 银牌 / 金牌 / 铂金
    # 注意这是"枚举值"，必须把可选值写进描述里，否则模型会自己编出"钻石会员"这种不存在的值
    Column("level", String(20), comment="会员等级"),
    # 客户年龄，可能为空（未填写），这类字段让模型学会处理 NULL
    Column("age", Integer, comment="年龄"),
)


# ---------------------------------------------------------------
# 表 2：products 商品表
# ---------------------------------------------------------------
products = Table(
    "products",
    metadata,
    # 商品 ID，主键自增
    Column("product_id", Integer, primary_key=True, autoincrement=True, comment="商品ID"),
    # 商品名称
    Column("product_name", String(100), nullable=False, comment="商品名称"),
    # 商品类目：数码 / 家电 / 服饰 / 食品 / 家居
    Column("category", String(50), comment="商品类目"),
    # 品牌
    Column("brand", String(50), comment="品牌"),
    # 标准单价（原价），实际成交价要看 order_items.unit_price
    Column("unit_price", Numeric(10, 2), comment="商品单价"),
    # 当前库存数量
    Column("stock_qty", Integer, comment="库存数量"),
    # 上架日期
    Column("created_at", Date, comment="上架日期"),
)


# ---------------------------------------------------------------
# 表 3：orders 订单主表
# ---------------------------------------------------------------
orders = Table(
    "orders",
    metadata,
    Column("order_id", Integer, primary_key=True, autoincrement=True, comment="订单ID"),
    # 外键指向 customers，这是"按客户维度分析订单"时必须知道的关联关系
    Column(
        "customer_id",
        Integer,
        ForeignKey("customers.customer_id"),
        comment="客户ID，关联 customers 表",
    ),
    # 下单日期，时间维度分析的主字段
    Column("order_date", Date, comment="下单日期"),
    # 订单状态：待支付 / 已支付 / 已发货 / 已完成 / 已取消
    Column("status", String(20), comment="订单状态"),
    # 订单总金额
    Column("total_amount", Numeric(10, 2), comment="订单总金额"),
    # 支付方式：支付宝 / 微信 / 银行卡 / 余额
    Column("payment_method", String(20), comment="支付方式"),
    # 下单渠道：App / 小程序 / PC 官网
    # 这个字段适合做分渠道对比分析，是很好的演示场景
    Column("channel", String(20), comment="下单渠道"),
)


# ---------------------------------------------------------------
# 表 4：order_items 订单明细表
# ---------------------------------------------------------------
# 为什么订单要拆成主表 + 明细表：
#   一个订单可以包含多个商品，如果塞在一张表里就得用"商品1,商品2"这种字符串存，
#   没法做"哪个商品卖得最好"这类分析。拆表是关系型数据库的基本功。
order_items = Table(
    "order_items",
    metadata,
    Column("item_id", Integer, primary_key=True, autoincrement=True, comment="明细ID"),
    # 外键指向订单主表
    Column("order_id", Integer, ForeignKey("orders.order_id"), comment="订单ID，关联 orders 表"),
    # 外键指向商品表
    Column(
        "product_id",
        Integer,
        ForeignKey("products.product_id"),
        comment="商品ID，关联 products 表",
    ),
    # 购买数量
    Column("quantity", Integer, comment="购买数量"),
    # 成交单价，可能因为促销低于商品表里的标准单价
    Column("unit_price", Numeric(10, 2), comment="成交单价"),
    # 折扣金额，用于分析促销力度
    Column("discount", Numeric(10, 2), comment="折扣金额"),
)


# ---------------------------------------------------------------
# 表 5：refunds 退款表
# ---------------------------------------------------------------
# 这张表的存在是为了制造"一对多"和"可能没有记录"的关联场景：
#   不是每个订单都有退款，所以 JOIN 时要用 LEFT JOIN。
#   模型很容易在这里犯错（写成 INNER JOIN 导致有退款的订单才算进来），
#   这正是测试问题集里要覆盖的难点。
refunds = Table(
    "refunds",
    metadata,
    Column("refund_id", Integer, primary_key=True, autoincrement=True, comment="退款ID"),
    # 外键指向订单主表
    Column("order_id", Integer, ForeignKey("orders.order_id"), comment="订单ID，关联 orders 表"),
    # 退款日期
    Column("refund_date", Date, comment="退款日期"),
    # 退款金额
    Column("refund_amount", Numeric(10, 2), comment="退款金额"),
    # 退款原因：质量问题 / 七天无理由 / 发错货 / 物流延迟 / 其他
    Column("refund_reason", String(50), comment="退款原因"),
)


# ===============================================================
# 中文语义描述（Schema 检索与 Prompt 组装的核心素材）
# ===============================================================
# 为什么这部分是 Text-to-SQL 效果的关键：
#   模型不认识你的业务。光给它 customer_id INTEGER 这种信息，它只能猜。
#   把"表是干什么的、字段是什么含义、有哪些可选值"写清楚，
#   SQL 生成准确率的差别非常明显——这比换更贵的模型有效得多。
#
# 维护约定：新增字段时，这里和上面的 Table 定义要同步补充。


# 表级描述：说明这张表回答什么业务问题
TABLE_DESCRIPTIONS: dict[str, str] = {
    "customers": "客户信息表。记录注册客户的基本属性，用于客户画像、地域分布、会员等级分析。",
    "products": "商品信息表。记录在售商品的价格、类目、品牌与库存，用于商品维度分析。",
    "orders": "订单主表。每行是一笔订单，记录下单时间、金额、状态、支付方式与渠道。",
    "order_items": "订单明细表。每行是订单中的一个商品条目，一个订单可对应多行。用于分析具体商品销量。",
    "refunds": "退款记录表。记录发生过退款的订单及其金额与原因，并非所有订单都有退款记录。",
}


# 字段级描述：说明字段含义与取值范围
COLUMN_DESCRIPTIONS: dict[str, dict[str, str]] = {
    "customers": {
        "customer_id": "客户唯一标识，主键",
        "name": "客户姓名",
        "email": "客户邮箱，唯一",
        "city": "客户所在城市",
        "province": "客户所在省份",
        "register_date": "注册日期，格式 YYYY-MM-DD",
        "level": "会员等级，取值：普通、银牌、金牌、铂金",
        "age": "客户年龄，可能为空",
    },
    "products": {
        "product_id": "商品唯一标识，主键",
        "product_name": "商品名称",
        "category": "商品类目，取值：数码、家电、服饰、食品、家居",
        "brand": "商品品牌",
        "unit_price": "商品标准单价，单位：元",
        "stock_qty": "当前库存数量",
        "created_at": "商品上架日期",
    },
    "orders": {
        "order_id": "订单唯一标识，主键",
        "customer_id": "下单客户ID，关联 customers.customer_id",
        "order_date": "下单日期，格式 YYYY-MM-DD",
        "status": "订单状态，取值：待支付、已支付、已发货、已完成、已取消",
        "total_amount": "订单总金额，单位：元",
        "payment_method": "支付方式，取值：支付宝、微信、银行卡、余额",
        "channel": "下单渠道，取值：App、小程序、PC官网",
    },
    "order_items": {
        "item_id": "明细唯一标识，主键",
        "order_id": "所属订单ID，关联 orders.order_id",
        "product_id": "商品ID，关联 products.product_id",
        "quantity": "购买数量",
        "unit_price": "成交单价，单位：元，可能因促销低于商品标准单价",
        "discount": "该条目享受的折扣金额，单位：元",
    },
    "refunds": {
        "refund_id": "退款记录唯一标识，主键",
        "order_id": "退款所属订单ID，关联 orders.order_id",
        "refund_date": "退款日期",
        "refund_amount": "退款金额，单位：元",
        "refund_reason": "退款原因，取值：质量问题、七天无理由、发错货、物流延迟、其他",
    },
}


# 外键关系描述：告诉模型表之间怎么 JOIN。
# 这一段是解决多表关联错误最有效的材料，务必写全。
RELATIONSHIP_DESCRIPTIONS: list[str] = [
    "orders.customer_id -> customers.customer_id（多对一：多笔订单属于同一个客户）",
    "order_items.order_id -> orders.order_id（多对一：一个订单有多条商品明细）",
    "order_items.product_id -> products.product_id（多对一：多条明细可指向同一商品）",
    "refunds.order_id -> orders.order_id（多对一：一个订单可能有多条退款记录，也可能没有）",
]


# 业务口径说明：这些是"数据里看不出来、但业务上必须遵守"的规则。
# 缺少这部分，模型经常写出统计口径错误的 SQL，比如把已取消订单也算进销售额。
BUSINESS_RULES: list[str] = [
    "统计销售额、销量时，通常需要排除订单状态为「已取消」和「待支付」的订单",
    "订单实付金额可用 orders.total_amount；如需按商品细化，用 order_items 的 quantity * unit_price - discount",
    "「退款」相关信息必须查 refunds 表，不能从订单状态推断",
    "统计客户数、商品数时注意去重，多表 JOIN 会导致同一实体出现多行",
    "涉及「最近」「本月」「上个月」等相对时间，请基于 orders.order_date 计算，不要写死日期",
]


def get_table_names() -> list[str]:
    """
    返回本模块定义的所有表名。

    用途：建表、灌数据、建索引时遍历使用，避免在多个文件里重复维护表名列表。

    返回:
        表名列表，顺序与定义顺序一致
    """
    return ["customers", "products", "orders", "order_items", "refunds"]


def get_translated_table(table_name: str) -> Table:
    """
    按表名取出 SQLAlchemy 的 Table 对象。

    用途：灌测试数据时需要通过 Table 对象执行 insert。

    参数:
        table_name 表名

    返回:
        对应的 Table 对象

    异常:
        KeyError 当表名不存在时抛出，并明确列出可用表名
    """
    # metadata.tables 是一个类似字典的结构，键是表名
    if table_name not in metadata.tables:
        raise KeyError(f"表 {table_name!r} 不存在，可用表名：{list(metadata.tables)}")
    return metadata.tables[table_name]


def build_schema_text(table_names: list[str] | None = None) -> str:
    """
    把指定表的结构拼成一段适合放进 Prompt 的中文文本。

    这是 Text-to-SQL 最关键的一个函数。它把上面积累的中文描述
    组装成模型最容易理解的形式，既包含字段类型，也包含业务含义和可选值。

    参数:
        table_names 需要拼进文本的表名列表；传 None 表示包含全部表

    返回:
        多行字符串，形如：
            【customers】客户信息表。...
              字段：
                - customer_id (INTEGER)：客户唯一标识，主键
                ...
              关联关系：
                - orders.customer_id -> customers.customer_id (...)
    """
    # 不指定表名时默认取全部表
    target_tables = table_names if table_names is not None else get_table_names()

    sections: list[str] = []

    for table_name in target_tables:
        table = get_translated_table(table_name)
        lines: list[str] = [f"【{table_name}】{TABLE_DESCRIPTIONS.get(table_name, '')}"]

        # 逐字段输出：字段名 + 类型 + 中文含义
        lines.append("  字段：")
        for column in table.columns:
            # 取中文描述，没有就退回字段名本身，保证不会输出空白
            description = COLUMN_DESCRIPTIONS.get(table_name, {}).get(
                column.name, column.name
            )
            # str(column.type) 会得到 INTEGER / VARCHAR(50) / NUMERIC(10, 2) 这类类型名
            lines.append(f"    - {column.name} ({column.type})：{description}")

        sections.append("\n".join(lines))

    # 关联关系只在查询涉及多表时有意义，但一次给出成本很低，统一附在最后
    sections.append("表之间的关联关系：\n  " + "\n  ".join(RELATIONSHIP_DESCRIPTIONS))

    return "\n\n".join(sections)


# ---------------------------------------------------------------
# 自测入口：用法 python -m app.agent.database.schema_definition
# ---------------------------------------------------------------
if __name__ == "__main__":
    print("=" * 70)
    print("表结构定义自检")
    print("=" * 70)

    # 确认 5 张表都注册到了 metadata 里
    print(f"已定义表：{get_table_names()}")
    print(f"metadata 中注册的表：{sorted(metadata.tables.keys())}")
    print("-" * 70)

    # 打印完整 Schema 文本，这段内容就是后面要交给模型看的材料
    print("拼装出的 Schema 文本（实际会按检索结果只取相关表）：")
    print("-" * 70)
    print(build_schema_text())
    print("-" * 70)

    # 单独演示只取两张表的场景，这就是 Schema 检索命中后的效果
    print("只取 orders 和 customers 两张表时的文本：")
    print("-" * 70)
    print(build_schema_text(["orders", "customers"]))
    print("-" * 70)

    # 演示业务规则，这部分会单独拼进 Prompt
    print("业务口径说明：")
    for rule in BUSINESS_RULES:
        print(f"  - {rule}")
