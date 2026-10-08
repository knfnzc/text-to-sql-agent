/**
 * 后端接口的类型定义。
 *
 * 这份文件与 `backend/app/api/schemas.py` 一一对应，改后端契约时必须同步改这里。
 * 为什么手写而不从 OpenAPI 自动生成：
 *    自动生成的类型往往是 `{ [key: string]: any }` 这样的宽泛结构，
 *    IDE 补全给不出字段名提示，等于没有类型。
 *   手写一份能换来完整的字段提示和编译期检查，这点维护成本是值得的。
 *
 * 另外，后端刻意给响应字段都设了默认值，所以这里全部标成必填——
 * 只要接口返回 200，就一定有这么些字段，前端不必到处写 `?.`。
 */

// ===============================================================
// 请求
// ===============================================================

/** 提问请求体 */
export interface QueryRequest {
  /** 自然语言问题，1~500 字符 */
  question: string
  /** 是否生成结论解读与图表配置 */
  with_answer?: boolean
}

/** 重建 Schema 向量索引的请求体 */
export interface RebuildIndexRequest {
  /** 是否强制重建。false 时若索引已就绪则直接复用 */
  force?: boolean
}

/**
 * 更新运行时模型配置的请求体。
 *
 * 全部字段可选：只提交用户真的改过的那几项。
 * 若要求全量提交，用户只想换密钥时也得把模型名、地址原样回传，
 * 任何一项在前端被误改都会顺手把别的配置一起弄坏。
 */
export interface LLMConfigUpdateRequest {
  /** 模型名称 */
  model?: string
  /** 接口地址，必须以 http:// 或 https:// 开头 */
  base_url?: string
  /** 密钥。不提交表示沿用当前密钥 */
  api_key?: string
  /** 结构化输出方式：json_schema / function_calling / json_mode */
  structured_method?: string
  /** 是否显式关闭思考模式 */
  disable_thinking?: boolean
}

// ===============================================================
// 响应：问答结果的组成部分
// ===============================================================

/** 查询意图。字段由模型按 INTENT_PROMPT 的结构化输出填充 */
export interface IntentPayload {
  /** 核心指标，例如「总销售额」 */
  metric?: string
  /** 分析维度，例如 ["商品类目"] */
  dimensions?: string[]
  /** 筛选条件 */
  filters?: string[]
  /** 时间范围 */
  time_range?: string
  /** 排序与限制，例如「按销售额降序取前 10」 */
  sort_and_limit?: string
  [key: string]: unknown
}

/** SQL 执行结果 */
export interface ExecutionPayload {
  success: boolean
  /** 失败原因，成功时为空串 */
  error: string
  row_count: number
  elapsed_ms: number
  /** 列名，顺序与 rows 中每行的下标一致 */
  columns: string[]
  /**
   * 行数据，二维数组。
   *
   * 后端刻意不使用「字典数组」，因为那样每一行都要重复一遍列名，
   * 行数一多传输体积就要翻好几倍。前端展示时用 columns 的下标去对齐。
   * 元素类型放开成 unknown，因为 SQL 结果里可能出现数字、字符串、null。
   */
  rows: unknown[][]
}

/** SQL 安全校验结果 */
export interface ValidationPayload {
  passed: boolean
  /** 拦截原因 */
  errors: string[]
  /** 非致命提醒 */
  warnings: string[]
  /** 已执行的检查项，用于展示"安全层确实在工作" */
  applied_rules: string[]
  /** SQL 中识别到的表 */
  detected_tables: string[]
}

/** 图表配置。前端按 chart_type 自行渲染，后端不传图形对象 */
export interface ChartPayload {
  /** metric / bar / line / pie / scatter / table */
  chart_type: string
  /** 分类轴或横轴列名 */
  x_field: string
  /** 数值轴列名 */
  y_field: string
  title: string
  /** 选择该图表类型的理由 */
  reason: string
  /** 配置被自动修正的记录 */
  notes: string[]
}

/** 一条执行轨迹 */
export interface TraceItem {
  step?: string
  /** 产生这条轨迹的节点名 */
  node: string
  message: string
  /** info / warning / error */
  level: string
  elapsed_ms?: number
}

/** token 用量 */
export interface TokenUsage {
  prompt_tokens?: number
  completion_tokens?: number
  [key: string]: number | undefined
}

/** 一次问答的完整结果 */
export interface QueryResponse {
  question: string
  intent: IntentPayload
  /** 模型最后生成的 SQL */
  sql: string
  /** 经安全校验改写后真正执行的 SQL（带强制 LIMIT） */
  safe_sql: string
  /** 每次尝试生成的 SQL，长度大于 1 说明发生过重试 */
  sql_history: string[]
  /** Schema 检索命中的表 */
  candidate_tables: string[]
  validation: ValidationPayload
  execution: ExecutionPayload
  chart: ChartPayload
  /** 结论解读，Markdown 文本 */
  answer: string
  trace: TraceItem[]
  token_usage: TokenUsage
  /** 因失败重试的次数 */
  retry_count: number
  /** 整条链路是否成功产出结果 */
  succeeded: boolean
  /** 是否运行在离线模拟模式 */
  mock: boolean
}

// ===============================================================
// 响应：元信息
// ===============================================================

/** 健康检查结果 */
export interface HealthResponse {
  /** ok / degraded */
  status: string
  mock_mode: boolean
  database_connected: boolean
  index_ready: boolean
  index_document_count: number
  index_expected_count: number
}

/** 配置与限制信息 */
export interface MetaResponse {
  /** 模型配置摘要，密钥已脱敏 */
  model_summary: string
  mock_mode: boolean
  limits: {
    sql_row_limit?: number
    sql_timeout_seconds?: number
    sql_max_retries?: number
    schema_top_k?: number
    [key: string]: unknown
  }
  example_questions: string[]
}

/** 一张表的元信息 */
export interface TableInfo {
  name: string
  /** 中文业务含义 */
  description: string
  columns: TableColumnInfo[]
}

/** 表字段的业务说明 */
export interface TableColumnInfo {
  name: string
  description: string
  [key: string]: unknown
}

/** 向量索引统计 */
export interface IndexStatsResponse {
  collection_name: string
  document_count: number
  expected_count: number
  embedding_model: string
  persist_dir: string
  /** 文档数与预期表数是否一致 */
  ready: boolean
}

/** 重建索引结果 */
export interface RebuildIndexResponse {
  document_count: number
  message: string
}

/**
 * 当前生效的模型配置。
 *
 * 密钥只以脱敏形式下发（api_key_masked），原始密钥只进不出——
 * 前端不需要它，返回明文等于凭空多一个泄露面。
 */
export interface LLMConfigResponse {
  /** 当前生效的模型名称 */
  model: string
  /** 当前生效的接口地址 */
  base_url: string
  /** 脱敏后的密钥，形如 sk-abc*** */
  api_key_masked: string
  /** 是否已配置密钥 */
  has_api_key: boolean
  /** 当前生效的结构化输出方式 */
  structured_method: string
  /** 是否显式关闭思考模式 */
  disable_thinking: boolean
  /** 是否处于离线模拟模式。由 .env 决定，界面不可改 */
  mock_mode: boolean
  /** 被界面覆盖过的字段名，为空表示当前全部来自 .env */
  overridden: string[]
  /** 一行配置摘要，密钥已脱敏 */
  summary: string
  /** 结构化输出方式的可选值，供下拉框渲染 */
  structured_method_options: string[]
  /** 操作结果说明，仅写操作会填充 */
  message: string
}

// ===============================================================
// SSE 事件
// ===============================================================

/** 阶段的运行状态 */
export type StageStatus = 'running' | 'done'

/** stage 事件的负载 */
export interface StageEvent {
  /** 节点名，render 是接口层追加的"生成结论与图表"阶段 */
  node: string
  /** 给用户看的中文说明 */
  label: string
  status: StageStatus
}

/** error 事件的负载 */
export interface ErrorEvent {
  message: string
}

/** 流式回调。所有回调都可选，前端只关心其中几个 */
export interface StreamHandlers {
  onStage?: (event: StageEvent) => void
  onTrace?: (item: TraceItem) => void
  onResult?: (result: QueryResponse) => void
  onError?: (event: ErrorEvent) => void
  onDone?: () => void
  /** 连接层面的错误（后端未启动、网络中断），与业务 error 事件区分开 */
  onTransportError?: (error: Error) => void
}
