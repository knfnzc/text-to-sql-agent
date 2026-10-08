"""接口层的请求与响应模型。

为什么要在已有状态字典之外再定义一套 Pydantic 模型：
    1. 前端拿到的字段是"承诺"。状态字典是内部实现，字段可以随重构变化；
       这里的模型改了就是契约变了，前端必须同步——这个约束是有价值的。
    2. 自动生成 OpenAPI 文档。FastAPI 会用这些模型生成 /docs 里的交互式文档，
       前端可以直接照着文档联调，不需要口头对字段。
    3. 请求参数在这里做第一道校验（比如问题不能为空、长度上限），
       非法请求在进入 Agent 之前就被拦下，不浪费模型调用。

设计取舍：把 SQL、校验结果、执行结果、图表配置分别做成子模型，
而不是铺平成一个大字典。好处是前端能按块渲染，坏处是层级多一点。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


# ===============================================================
# 请求
# ===============================================================


class QueryRequest(BaseModel):
    """问答请求。"""

    question: str = Field(
        ...,
        min_length=1,
        max_length=500,
        description="用户的自然语言问题，例如「各商品类目的总销售额是多少？」",
        examples=["最近三个月每个月的销售额是多少？"],
    )

    with_answer: bool = Field(
        default=True,
        description=(
            "是否生成结论解读与图表配置。"
            "开启会额外消耗两次模型调用；"
            "只想看 SQL 和原始数据时可以关掉，用于快速调试"
        ),
    )


class RebuildIndexRequest(BaseModel):
    """重建 Schema 向量的请求。"""

    force: bool = Field(
        default=True,
        description="是否强制重建。false 时若索引已就绪则直接复用",
    )


class LLMConfigUpdateRequest(BaseModel):
    """更新运行时模型配置的请求。

    为什么所有字段都是可选的：
        前端只提交"用户真的改了的那几项"，其余保持当前值。
        如果要求全量提交，用户只想换密钥时就得把模型名、地址都原样回传一遍，
        任何一项在前端被截断或误改都会顺手把别的配置一起弄坏。
    """

    model: str | None = Field(
        default=None,
        description="模型名称。不提交表示不修改",
        examples=["deepseek-chat"],
    )
    base_url: str | None = Field(
        default=None,
        description="接口地址，必须以 http:// 或 https:// 开头。不提交表示不修改",
        examples=["https://api.deepseek.com/v1"],
    )
    api_key: str | None = Field(
        default=None,
        description="密钥。不提交表示不修改（密钥不会回传给前端，所以留空即不修改）",
    )
    structured_method: str | None = Field(
        default=None,
        description="结构化输出方式：json_schema / function_calling / json_mode",
    )
    disable_thinking: bool | None = Field(
        default=None,
        description=(
            "是否显式关闭思考模式。"
            "DeepSeek 等模型的思考模式会忽略温度、占用 max_tokens，"
            "并拒绝被强制指定的 tool_choice，导致结构化输出 400"
        ),
    )


# ===============================================================
# 响应（问答结果的组成部分）
# ===============================================================


class ExecutionPayload(BaseModel):
    """SQL 执行结果。

    rows 直接用二维数组而不是字典数组：
        同样的数据，字典数组要为每一行重复一遍列名，行数多时体积翻好几倍。
        列名只传一次放在 columns 里，前端按下标对齐即可。
    """

    success: bool = Field(default=False, description="是否执行成功")
    error: str = Field(default="", description="失败原因，成功时为空串")
    row_count: int = Field(default=0, description="返回行数")
    elapsed_ms: float = Field(default=0.0, description="执行耗时（毫秒）")
    columns: list[str] = Field(default_factory=list, description="列名，顺序与 rows 中每行一致")
    rows: list[list[Any]] = Field(default_factory=list, description="行数据，二维数组")


class ValidationPayload(BaseModel):
    """SQL 安全校验结果。

    把"检查了什么"也返回给前端，是为了让安全机制可见——
    这是本项目最有技术含量的部分，藏着不展示等于白做。
    """

    passed: bool = Field(default=False, description="是否通过全部校验规则")
    errors: list[str] = Field(default_factory=list, description="拦截原因")
    warnings: list[str] = Field(default_factory=list, description="非致命提醒")
    applied_rules: list[str] = Field(default_factory=list, description="已执行的检查项")
    detected_tables: list[str] = Field(default_factory=list, description="SQL 中识别到的表")


class ChartPayload(BaseModel):
    """图表配置。

    只传配置不传图形对象：
        Plotly 的 Figure 序列化成 JSON 又大又难被前端复用。
        这里给出"画什么类型、用哪两列"，前端用 ECharts 自行渲染，
        既减小传输体积，也让前端能自由调整视觉风格。
    """

    chart_type: str = Field(default="table", description="metric/bar/line/pie/scatter/table")
    x_field: str = Field(default="", description="分类轴或横轴列名")
    y_field: str = Field(default="", description="数值轴列名")
    title: str = Field(default="", description="图表标题")
    reason: str = Field(default="", description="选择该图表类型的理由")
    notes: list[str] = Field(default_factory=list, description="配置被自动修正的记录")


class QueryResponse(BaseModel):
    """一次问答的完整结果。"""

    question: str = Field(description="原始提问，原样回传便于前端对应")
    intent: dict[str, Any] = Field(default_factory=dict, description="解析出的查询意图")
    sql: str = Field(default="", description="模型最后生成的 SQL")
    safe_sql: str = Field(default="", description="经安全校验改写后真正执行的 SQL")
    sql_history: list[str] = Field(default_factory=list, description="每次尝试生成的 SQL")
    candidate_tables: list[str] = Field(default_factory=list, description="Schema 检索命中的表")
    validation: ValidationPayload = Field(default_factory=ValidationPayload)
    execution: ExecutionPayload = Field(default_factory=ExecutionPayload)
    chart: ChartPayload = Field(default_factory=ChartPayload)
    answer: str = Field(default="", description="结论解读，Markdown 文本")
    trace: list[dict[str, Any]] = Field(default_factory=list, description="执行轨迹时间线")
    token_usage: dict[str, int] = Field(default_factory=dict, description="token 用量")
    retry_count: int = Field(default=0, description="因失败重试的次数")
    succeeded: bool = Field(default=False, description="整条链路是否成功产出结果")
    mock: bool = Field(default=False, description="是否运行在离线模拟模式")


# ===============================================================
# 响应（元信息）
# ===============================================================


class HealthResponse(BaseModel):
    """健康检查结果。前端启动时用它决定是否显示"服务未就绪"。"""

    status: str = Field(description="ok / degraded")
    mock_mode: bool = Field(description="是否处于离线模拟模式")
    database_connected: bool = Field(description="数据库是否可连接")
    index_ready: bool = Field(description="Schema 向量索引是否就绪")
    index_document_count: int = Field(default=0, description="索引中的文档数")
    index_expected_count: int = Field(default=0, description="应有的文档数（表数量）")


class MetaResponse(BaseModel):
    """配置与限制信息，用于前端侧边栏展示。"""

    model_summary: str = Field(description="模型配置摘要，密钥已脱敏")
    mock_mode: bool = Field(description="是否处于离线模拟模式")
    limits: dict[str, Any] = Field(default_factory=dict, description="安全限制参数")
    example_questions: list[str] = Field(default_factory=list, description="示例问题")


class LLMConfigResponse(BaseModel):
    """当前生效的模型配置。

    安全说明：密钥只以脱敏形式返回（api_key_masked），
    原始密钥只进不出——前端提交后不会再收到它。
    """

    model: str = Field(description="当前生效的模型名称")
    base_url: str = Field(description="当前生效的接口地址")
    api_key_masked: str = Field(description="脱敏后的密钥，形如 sk-abc***")
    has_api_key: bool = Field(description="是否已配置密钥")
    structured_method: str = Field(description="当前生效的结构化输出方式")
    disable_thinking: bool = Field(description="是否显式关闭思考模式")
    mock_mode: bool = Field(description="是否处于离线模拟模式（由 .env 决定，界面不可改）")
    overridden: list[str] = Field(
        default_factory=list,
        description="被界面覆盖过的字段名，为空表示当前全部来自 .env",
    )
    summary: str = Field(default="", description="一行配置摘要，密钥已脱敏")
    structured_method_options: list[str] = Field(
        default_factory=list, description="结构化输出方式的可选值，供前端渲染下拉框"
    )
    message: str = Field(default="", description="操作结果说明，仅写操作会填充")


class TableInfo(BaseModel):
    """一张表的元信息。"""

    name: str = Field(description="表名")
    description: str = Field(default="", description="中文业务含义")
    columns: list[dict[str, Any]] = Field(default_factory=list, description="字段列表")


class IndexStatsResponse(BaseModel):
    """索引统计。"""

    collection_name: str = Field(default="")
    document_count: int = Field(default=0)
    expected_count: int = Field(default=0)
    embedding_model: str = Field(default="")
    persist_dir: str = Field(default="")
    ready: bool = Field(default=False, description="文档数与预期表数是否一致")


class RebuildIndexResponse(BaseModel):
    """重建索引结果。"""

    document_count: int = Field(description="重建后的文档数")
    message: str = Field(default="")
