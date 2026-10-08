/**
 * 离线演示数据。
 *
 * 存在的意义：
 *    这是一个前后端分离的项目，界面本身没有任何推理能力。
 *    但作品展示常常需要在没有后端的环境里也能打开看
 *    （比如 GitHub Pages 上的静态站点、或者交给别人时对方懒得装 Python 环境）。
 *    内置一份真实的样例响应，就能让界面独立跑起来。
 *
 * 数据来源说明（这点很重要，不能含糊）：
 *    下面的内容与后端在离线模拟模式下对同一问题的真实输出结构完全一致，
 *    数值是按其生成规律填的样例，不是编造出来的假接口。
 *    界面在演示模式下会显式标出"演示数据"，不会让人误以为是实时调用。
 */

import type {
  HealthResponse,
  IndexStatsResponse,
  LLMConfigResponse,
  MetaResponse,
  QueryResponse,
  TableInfo,
} from '@/types/api'

export const DEMO_QUESTION = '各商品类目的总销售额是多少？'

export const demoMeta: MetaResponse = {
  model_summary: 'Provider: openai-compatible | Model: gpt-4o-mini | Key: sk-abc…（脱敏）',
  mock_mode: true,
  limits: {
    sql_row_limit: 200,
    sql_timeout_seconds: 10,
    sql_max_retries: 3,
    schema_top_k: 3,
  },
  example_questions: [
    '一共有多少个客户？',
    '各商品类目的总销售额是多少？',
    '最近六个月每个月的销售额是多少？',
    '各会员等级的平均订单金额是多少？',
    '销量最高的 10 个商品是哪些？',
    '退款率最高的商品类目是哪个？',
    '哪些商品从来没有被卖出过？',
    '复购客户占了总客户数的多少比例？',
  ],
}

export const demoHealth: HealthResponse = {
  status: 'ok',
  mock_mode: true,
  database_connected: true,
  index_ready: true,
  index_document_count: 5,
  index_expected_count: 5,
}

/**
 * 演示模式下的模型配置。
 *
 * 同样是只读展示：演示模式没有后端，改配置无从生效，
 * 面板会把编辑入口一并禁用，而不是让人填完才发现保存不了。
 */
export const demoLLMConfig: LLMConfigResponse = {
  model: 'gpt-4o-mini',
  base_url: 'https://api.openai.com/v1',
  api_key_masked: 'sk-abc***',
  has_api_key: true,
  structured_method: 'function_calling',
  disable_thinking: false,
  mock_mode: true,
  overridden: [],
  summary: 'provider=openai-compatible model=gpt-4o-mini key=sk-abc***',
  structured_method_options: ['json_schema', 'function_calling', 'json_mode'],
  message: '',
}

export const demoIndexStats: IndexStatsResponse = {
  collection_name: 'schema_docs',
  document_count: 5,
  expected_count: 5,
  embedding_model: 'text-embedding-3-small',
  persist_dir: 'data/chroma',
  ready: true,
}

export const demoTables: TableInfo[] = [
  {
    name: 'customers',
    description: '客户信息表。记录注册客户的基本属性，用于客户画像、地域分布、会员等级分析。',
    columns: [
      { name: 'customer_id', description: '客户唯一标识，主键' },
      { name: 'name', description: '客户姓名' },
      { name: 'email', description: '客户邮箱，唯一' },
      { name: 'city', description: '客户所在城市' },
      { name: 'province', description: '客户所在省份' },
      { name: 'register_date', description: '注册日期，格式 YYYY-MM-DD' },
      { name: 'level', description: '会员等级，取值：普通、银牌、金牌、铂金' },
      { name: 'age', description: '客户年龄，可能为空' },
    ],
  },
  {
    name: 'products',
    description: '商品信息表。记录在售商品的价格、类目、品牌与库存，用于商品维度分析。',
    columns: [
      { name: 'product_id', description: '商品唯一标识，主键' },
      { name: 'product_name', description: '商品名称' },
      { name: 'category', description: '商品类目，取值：数码、家电、服饰、食品、家居' },
      { name: 'brand', description: '商品品牌' },
      { name: 'unit_price', description: '商品标准单价，单位：元' },
      { name: 'stock_qty', description: '当前库存数量' },
      { name: 'created_at', description: '商品上架日期' },
    ],
  },
  {
    name: 'orders',
    description: '订单主表。每行是一笔订单，记录下单时间、金额、状态、支付方式与渠道。',
    columns: [
      { name: 'order_id', description: '订单唯一标识，主键' },
      { name: 'customer_id', description: '下单客户ID，关联 customers.customer_id' },
      { name: 'order_date', description: '下单日期，格式 YYYY-MM-DD' },
      { name: 'status', description: '订单状态，取值：待支付、已支付、已发货、已完成、已取消' },
      { name: 'total_amount', description: '订单总金额，单位：元' },
      { name: 'payment_method', description: '支付方式，取值：支付宝、微信、银行卡、余额' },
      { name: 'channel', description: '下单渠道，取值：App、小程序、PC官网' },
    ],
  },
  {
    name: 'order_items',
    description:
      '订单明细表。每行是订单中的一个商品条目，一个订单可对应多行。用于分析具体商品销量。',
    columns: [
      { name: 'item_id', description: '明细唯一标识，主键' },
      { name: 'order_id', description: '所属订单ID，关联 orders.order_id' },
      { name: 'product_id', description: '商品ID，关联 products.product_id' },
      { name: 'quantity', description: '购买数量' },
      { name: 'unit_price', description: '成交单价，单位：元，可能因促销低于商品标准单价' },
      { name: 'discount', description: '该条目享受的折扣金额，单位：元' },
    ],
  },
  {
    name: 'refunds',
    description: '退款记录表。记录发生过退款的订单及其金额与原因，并非所有订单都有退款记录。',
    columns: [
      { name: 'refund_id', description: '退款记录唯一标识，主键' },
      { name: 'order_id', description: '退款所属订单ID，关联 orders.order_id' },
      { name: 'refund_date', description: '退款日期' },
      { name: 'refund_amount', description: '退款金额，单位：元' },
      {
        name: 'refund_reason',
        description: '退款原因，取值：质量问题、七天无理由、发错货、物流延迟、其他',
      },
    ],
  },
]

/**
 * 一份完整的样例问答结果。
 *
 * 刻意包含了一次失败重试（sql_history 有两条），
 * 因为"自我修正"是这个项目最有说服力的机制，
 * 演示数据里把它藏起来反而看不出系统的价值。
 */
export const demoQueryResponse: QueryResponse = {
  question: DEMO_QUESTION,
  intent: {
    metric: '总销售额',
    dimensions: ['商品类目'],
    filters: ['排除订单状态为「已取消」和「待支付」的订单'],
    time_range: '不限',
    sort_and_limit: '按销售额降序，不限制条数',
  },
  sql: [
    'SELECT',
    '    p.category AS 商品类目,',
    '    ROUND(SUM(oi.quantity * oi.unit_price - oi.discount), 2) AS 总销售额',
    'FROM orders o',
    'JOIN order_items oi ON oi.order_id = o.order_id',
    'JOIN products p ON p.product_id = oi.product_id',
    "WHERE o.status NOT IN ('已取消', '待支付')",
    'GROUP BY p.category',
    'ORDER BY 总销售额 DESC',
  ].join('\n'),
  safe_sql: [
    'SELECT',
    '    p.category AS 商品类目,',
    '    ROUND(SUM(oi.quantity * oi.unit_price - oi.discount), 2) AS 总销售额',
    'FROM orders o',
    'JOIN order_items oi ON oi.order_id = o.order_id',
    'JOIN products p ON p.product_id = oi.product_id',
    "WHERE o.status NOT IN ('已取消', '待支付')",
    'GROUP BY p.category',
    'ORDER BY 总销售额 DESC',
    'LIMIT 200',
  ].join('\n'),
  sql_history: [
    [
      'SELECT',
      '    p.category AS 商品类目,',
      '    ROUND(SUM(oi.quantity * o.unit_price) - SUM(oi.discount), 2) AS 总销售额',
      'FROM orders o',
      'JOIN order_items oi ON oi.order_id = o.order_id',
      'JOIN products p ON p.product_id = oi.product_id',
      "WHERE o.status NOT IN ('已取消', '待支付')",
      'GROUP BY p.category',
      'ORDER BY 总销售额 DESC',
    ].join('\n'),
    [
      'SELECT',
      '    p.category AS 商品类目,',
      '    ROUND(SUM(oi.quantity * oi.unit_price - oi.discount), 2) AS 总销售额',
      'FROM orders o',
      'JOIN order_items oi ON oi.order_id = o.order_id',
      'JOIN products p ON p.product_id = oi.product_id',
      "WHERE o.status NOT IN ('已取消', '待支付')",
      'GROUP BY p.category',
      'ORDER BY 总销售额 DESC',
    ].join('\n'),
  ],
  candidate_tables: ['orders', 'order_items', 'products'],
  validation: {
    passed: true,
    errors: [],
    warnings: [],
    applied_rules: [
      '必须是 SELECT 语句，禁止任何写操作',
      '禁止多语句（分号拼接）',
      '禁止访问系统表与 PRAGMA',
      '单次查询必须带行数上限',
      '只允许访问白名单内的业务表',
      '检测到未加 LIMIT，已自动补上 LIMIT 200',
    ],
    detected_tables: ['orders', 'order_items', 'products'],
  },
  execution: {
    success: true,
    error: '',
    row_count: 5,
    elapsed_ms: 42.7,
    columns: ['商品类目', '总销售额'],
    rows: [
      ['数码', 1286430.5],
      ['家电', 986215.0],
      ['服饰', 542380.75],
      ['家居', 318940.2],
      ['食品', 176523.4],
    ],
  },
  chart: {
    chart_type: 'bar',
    x_field: '商品类目',
    y_field: '总销售额',
    title: '各商品类目总销售额对比',
    reason: '一个分类维度加一个数值指标，柱状图最适合做类目间的大小对比。',
    notes: [],
  },
  answer: [
    '### 结论',
    '',
    '**数码类目以约 128.6 万元位居第一**，是第二名家电（约 98.6 万元）的 1.3 倍。',
    '五个类目的销售额呈现出明显的梯队：',
    '',
    '| 梯队 | 类目 | 销售额（元） | 占比 |',
    '| --- | --- | --- | --- |',
    '| 第一梯队 | 数码 | 1,286,430.50 | 38.9% |',
    '| 第二梯队 | 家电 | 986,215.00 | 29.8% |',
    '| 第三梯队 | 服饰 | 542,380.75 | 16.4% |',
    '| 第三梯队 | 家居 | 318,940.20 | 9.6% |',
    '| 第三梯队 | 食品 | 176,523.40 | 5.3% |',
    '',
    '合计约 331.0 万元。',
    '',
    '### 需要注意的口径',
    '',
    '- 已按业务规则**排除「已取消」和「待支付」的订单**，因此这里是实际成交额。',
    '- 单件金额用的是 `数量 × 成交单价 − 折扣`，而不是商品表里的标准单价，' +
      '促销商品的贡献已按真实成交价计算。',
  ].join('\n'),
  trace: [
    {
      step: 'parse_intent',
      node: 'parse_intent',
      message: '解析意图：指标=总销售额，维度=[商品类目]，时间范围=不限',
      level: 'info',
      elapsed_ms: 812,
    },
    {
      step: 'retrieve_schema',
      node: 'retrieve_schema',
      message: 'Schema 检索命中 3 张表：orders, order_items, products',
      level: 'info',
      elapsed_ms: 96,
    },
    {
      step: 'generate_sql',
      node: 'generate_sql',
      message: '首次生成 SQL：SELECT p.category AS 商品类目, ROUND(SUM(oi.quantity * o.unit_price...',
      level: 'info',
      elapsed_ms: 1435,
    },
    {
      step: 'validate',
      node: 'validate',
      message: '安全校验通过，命中规则 6 条',
      level: 'info',
      elapsed_ms: 3,
    },
    {
      step: 'execute',
      node: 'execute',
      message: '执行失败：no such column: o.unit_price',
      level: 'error',
      elapsed_ms: 8,
    },
    {
      step: 'generate_sql',
      node: 'generate_sql',
      message: '第 1 次重试生成 SQL：SELECT p.category AS 商品类目, ROUND(SUM(oi.quantity * oi.unit_price...',
      level: 'warning',
      elapsed_ms: 1602,
    },
    {
      step: 'validate',
      node: 'validate',
      message: '安全校验通过，命中规则 6 条',
      level: 'info',
      elapsed_ms: 3,
    },
    {
      step: 'execute',
      node: 'execute',
      message: '执行成功，返回 5 行，耗时 42.7 ms',
      level: 'info',
      elapsed_ms: 43,
    },
  ],
  token_usage: {
    prompt_tokens: 4820,
    completion_tokens: 1176,
  },
  retry_count: 1,
  succeeded: true,
  mock: true,
}
