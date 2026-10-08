"""
结果解读与自动可视化。

解决的问题：
    数据库返回的是一张表，业务人员不想看表，想看结论和一张图。
    这一步负责把"SELECT 出来的二维数据"翻译成"一句人话 + 一张图"。

两个模块：
    interpret_result   调用模型，把结果转成自然语言结论
    build_figure       根据结果结构自动选择合适的图表类型并画出图

关于"图表类型怎么选"的设计取舍（面试可以展开聊）：
    完全交给模型选：灵活，但模型可能选错（比如给 50 个类目画饼图，
    结果每一块都细得看不清），而且每次调用都要花钱。
    完全用规则选：稳定，但无法理解"趋势"这类语义，
    比如它不知道按月的数据应该用折线图。

    这里的做法是"规则兜底 + 模型判断 + 代码校验"：
        1. 先把结果的结构特征（行数、列数、列类型）算出来
        2. 让模型结合提问和结构特征选图表类型与字段
        3. 代码再校验一遍：模型选的字段必须真实存在，
           如果是饼图但类目太多就强制改成柱状图
    这样既利用了模型的语义理解，又保证了输出的可用性。

中文字体说明：
    Plotly 画中文如果不指定字体，可能显示成方框。
    这里统一指定字体列表，包含 Noto Sans CJK SC、WenQuanYi Micro Hei
    以及 Windows 常见的微软雅黑、黑体，按顺序回退，保证各平台都能正常显示。

运行方式：
    python -m app.agent.output.renderer
"""

from typing import Any, Literal

import pandas as pd
import plotly.graph_objects as go
from langchain_core.prompts import ChatPromptTemplate
from pydantic import BaseModel, Field

from app.core.config import ensure_data_dirs
from app.core.db import QueryResult
from app.core.llm import get_chat_model, get_structured_runnable

from app.agent.schema_rag.schema_indexer import build_index
from app.agent.safety.sql_graph import ask as ask_graph


# ===============================================================
# 常量配置
# ===============================================================

# 中文字体。按顺序回退：先试 Linux 常用中文字体，再试 Windows 的。
# 必须显式指定，否则 Plotly 默认字体不含中文字形，中文会显示成豆腐块
CHINESE_FONT = "Noto Sans CJK SC, WenQuanYi Micro Hei, Microsoft YaHei, SimHei, sans-serif"

# 配色方案。用一组饱和度适中的颜色，保证打印和投屏都看得清
COLOR_SEQUENCE = [
    "#4C78A8", "#F58518", "#54A24B", "#E45756", "#72B7B2",
    "#EECA3B", "#B279A2", "#FF9DA6", "#9D755D", "#BAB0AC",
]

# 超过这个类目数就不建议画饼图。
# 饼图在类目多时每一块都极窄，读者无法分辨，这是数据可视化的常识
MAX_PIE_CATEGORIES = 8

# 推荐用折线图的维度关键词。时间类维度看趋势，折线比柱子直观
TIME_DIMENSION_KEYWORDS = ("月", "日", "季", "年", "周", "日期", "时间", "date", "month", "day")


# ===============================================================
# 结构化输出模型
# ===============================================================


class ChartSpec(BaseModel):
    """
    图表配置。

    设计说明：这里只存"配置"不存"图形对象"。
    原因是状态需要可序列化（为了持久化和断点续跑），
    Plotly 的 Figure 对象无法直接序列化成 JSON。
    另一个好处是前端拿到配置后可以自行决定怎么渲染，
    想换成 ECharts 也不用改后端逻辑。
    """

    chart_type: Literal["metric", "bar", "line", "pie", "scatter", "table"] = Field(
        description=(
            "图表类型。选择规则："
            "结果只有一个数值 -> metric（大数字卡片）；"
            "按时间维度看变化趋势 -> line（折线图）；"
            "类目数不超过 8 个且看占比 -> pie（饼图）；"
            "类目间对比大小 -> bar（柱状图）；"
            "两个数值列看相关性 -> scatter（散点图）；"
            "结构复杂不适合画图 -> table（表格）"
        )
    )

    x_field: str = Field(
        default="",
        description=(
            "横轴（或分类轴）使用的列名，必须严格等于结果中的某个列名。"
            "图表类型为 metric 或 table 时返回空字符串"
        ),
    )

    y_field: str = Field(
        default="",
        description="纵轴（或数值轴）使用的列名，必须严格等于结果中的某个列名",
    )

    title: str = Field(
        description="图表标题，用中文，要能说明这张图在展示什么，不要超过 20 个字"
    )

    reason: str = Field(
        description="一句话说明为什么选这个图表类型，便于人工核对选择是否合理"
    )


# ===============================================================
# 提示词
# ===============================================================

# 结果解读提示词。
# 注意约束"不要编造数据里没有的信息"，这是 LLM 分析类应用最常见的幻觉来源：
# 模型会倾向于补充"这是因为季节性因素导致的"这类听起来合理但数据里没有的结论。
INTERPRET_PROMPT = ChatPromptTemplate.from_messages(
    [
        (
            "system",
            """你是数据分析师，负责把查询结果解读成业务人员能看懂的结论。

解读要求：
1. 先用一句话直接回答用户的问题，给出最关键的数字
2. 再补充 1 到 2 句值得注意的发现，例如最大值出现在哪个类别、占比是否悬殊
3. 严格基于给定数据，不要编造数据中没有的信息，不要臆测原因
4. 如果结果为空，明确说明"没有查到符合条件的数据"，并提示可能的原因
5. 语言简洁，不要罗列所有数据行，也不要输出表格
6. 数字要带上单位（元、个、人、笔等），大数字可以用"约"字简化""",
        ),
        (
            "human",
            """用户的提问：{question}

执行的 SQL：
{sql}

查询结果（共 {row_count} 行）：
{result_text}

请给出解读结论。""",
        ),
    ]
)

# 图表选择提示词。
# 把"结果的结构特征"（列名、列类型、行数）和"样例数据"一起给模型，
# 让它有足够信息判断该怎么画
CHART_PROMPT = ChatPromptTemplate.from_messages(
    [
        (
            "system",
            """你是数据可视化专家，负责为查询结果选择最合适的图表类型。

选择原则：
1. 结果只有一个数值（1 行 1 列）-> metric
2. 有明确的时间维度（列名含月、日、季、年等）且看变化 -> line
3. 类目数量不超过 8 个且问题是问占比、分布 -> pie
4. 类目之间比大小多少 -> bar
5. 两个数值列看是否相关 -> scatter
6. 结构与上面都不匹配，或列太多 -> table

硬性要求：
- x_field 和 y_field 必须严格等于给定的列名，一个字都不能改
- 饼图和折线图只能有 1 个数值列，如果结果有多个数值列，选最能回答问题的那个
- 标题用中文，不超过 20 字，不要出现"图表"两个字""",
        ),
        (
            "human",
            """用户的提问：{question}

查询结果的列：{columns}
各列的数据类型：{dtypes}
结果行数：{row_count}

前几行数据样例：
{sample_text}

请选择最合适的图表类型和字段。""",
        ),
    ]
)


# ===============================================================
# 结果解读
# ===============================================================


def _format_result_for_prompt(result: QueryResult, max_rows: int = 20) -> str:
    """
    把查询结果格式化成适合放进提示词的文本。

    为什么要限制行数：
        结果可能有几百行，全塞进提示词既费钱又没必要。
        解读结论看前若干行足够，而且解读任务本身也不需要逐行数据。

    参数:
        result   查询结果
        max_rows 最多放入提示词的行数

    返回:
        格式化后的文本
    """
    if not result.success:
        return f"（查询执行失败：{result.error}）"

    if result.row_count == 0:
        return "（查询结果为空）"

    # 用制表符分隔，比逗号更安全（数据里可能本身含逗号）
    lines = ["\t".join(result.columns)]
    for row in result.rows[:max_rows]:
        lines.append("\t".join("" if v is None else str(v) for v in row))

    if result.row_count > max_rows:
        lines.append(f"...（共 {result.row_count} 行，此处只展示前 {max_rows} 行）")

    return "\n".join(lines)


def interpret_result(
    question: str,
    result: QueryResult,
    sql: str = "",
    verbose: bool = False,
) -> str:
    """
    把查询结果解读成自然语言结论。

    参数:
        question 用户原始提问
        result   查询结果
        sql      实际执行的 SQL，给模型做上下文
        verbose  是否打印过程

    返回:
        自然语言结论文本。即使结果为空或失败，也会返回一段有意义的说明
    """
    # 执行失败时不必浪费一次模型调用，直接给出说明
    if not result.success:
        return f"查询未能执行成功。原因：{result.error}"

    # 结果为空是常见情况（比如筛选条件太严），给出针对性提示更有帮助
    if result.row_count == 0:
        return "没有查到符合条件的数据。建议放宽筛选条件，或确认时间范围是否在数据覆盖区间内。"

    llm = get_chat_model(temperature=0, max_tokens=500)
    chain = INTERPRET_PROMPT | llm

    response = chain.invoke(
        {
            "question": question,
            "sql": sql or result.executed_sql,
            "row_count": result.row_count,
            "result_text": _format_result_for_prompt(result),
        }
    )

    answer = response.content if isinstance(response.content, str) else str(response.content)

    if verbose:
        print("  [结果解读]")
        print(f"    {answer}")

    return answer


# ===============================================================
# 图表类型选择
# ===============================================================


def _classify_column(series: pd.Series) -> str:
    """
    粗略判断一列是数值型还是文本型。

    参数:
        series 一列数据

    返回:
        "numeric" 或 "text"
    """
    # pandas 的 is_numeric_dtype 会正确处理 Decimal、int、float 等
    return "numeric" if pd.api.types.is_numeric_dtype(series) else "text"


def _has_time_dimension(columns: list[str]) -> bool:
    """
    判断列名里是否包含时间维度关键词。

    用途：规则兜底时判断该用折线图还是柱状图。

    参数:
        columns 列名列表

    返回:
        含时间关键词返回 True
    """
    joined = " ".join(columns).lower()
    return any(keyword in joined for keyword in TIME_DIMENSION_KEYWORDS)


def _fallback_chart_spec(result: QueryResult) -> ChartSpec:
    """
    规则兜底的图表配置。

    什么情况下会用到：
        模型调用失败、结构化输出解析失败、或模型选了不存在的字段。

    为什么一定要有兜底：
        LLM 应用里"模型这一步挂了"是必须考虑的情况。
        没有兜底的话，一次模型抖动就会让整个功能不可用。
        有了兜底，最差也能给用户一张柱状图。

    参数:
        result 查询结果

    返回:
        ChartSpec 实例
    """
    columns = result.columns

    # 只有一个值：大数字卡片
    if result.row_count == 1 and result.column_count == 1:
        return ChartSpec(
            chart_type="metric",
            x_field="",
            y_field=columns[0],
            title=f"{columns[0]}",
            reason="结果只有一个数值，用量化卡片展示最直观",
        )

    # 列太少或行太多：直接给表格
    if result.column_count < 2:
        return ChartSpec(
            chart_type="table",
            x_field="",
            y_field="",
            title="查询结果",
            reason="结果只有一列，不适合画图",
        )

    # 默认：第一列作为分类轴，第二列作为数值轴
    x_field = columns[0]
    y_field = columns[1]

    # 时间维度用折线图，其他用柱状图
    chart_type = "line" if _has_time_dimension([x_field]) else "bar"

    return ChartSpec(
        chart_type=chart_type,
        x_field=x_field,
        y_field=y_field,
        title=f"{y_field}按{x_field}分布",
        reason=f"规则兜底：{x_field} 作为维度、{y_field} 作为数值，按{'趋势' if chart_type == 'line' else '对比'}展示",
    )


def _validate_chart_spec(spec: ChartSpec, result: QueryResult) -> tuple[ChartSpec, list[str]]:
    """
    校验模型给出的图表配置，不合法就修正。

    这一步是"模型判断 + 代码校验"的落地。
    模型可能犯的错：选了不存在的列名、给太多类目画饼图、饼图指了非数值列。

    参数:
        spec   模型给出的配置
        result 查询结果

    返回:
        (修正后的配置, 修正说明列表)
    """
    notes: list[str] = []
    columns = result.columns

    # 校验字段是否存在
    if spec.chart_type == "metric":
        # metric 只描述"一个数字"。结果不是单值时是类型选错了，
        # 整条配置换成规则兜底的结果，避免多行数据硬撑成一张大数字卡
        if result.row_count != 1 or result.column_count != 1:
            fallback = _fallback_chart_spec(result)
            notes.append(
                f"结果为 {result.row_count} 行 {result.column_count} 列，量化卡片不适用，"
                f"已改为 {fallback.chart_type}"
            )
            spec.chart_type = fallback.chart_type
            spec.x_field = fallback.x_field
            spec.y_field = fallback.y_field
        # metric 也得能定位到那个数字。y_field 为空或不是真实列名时，
        # 前端取不到值，卡片会退化成"没有可展示的数值"
        elif spec.y_field not in columns:
            notes.append(f"量化卡片缺少数值字段，已改为 {columns[0]!r}")
            spec.y_field = columns[0]
    elif spec.chart_type != "table":
        if spec.x_field not in columns:
            notes.append(f"模型指定的横轴字段 {spec.x_field!r} 不存在，已改为 {columns[0]!r}")
            spec.x_field = columns[0]

        if spec.y_field not in columns:
            # 找一个数值列作为纵轴，优先选不是横轴的那一列
            numeric_columns = [c for c in columns if c != spec.x_field]
            fallback_y = numeric_columns[0] if numeric_columns else columns[-1]
            notes.append(f"模型指定的纵轴字段 {spec.y_field!r} 不存在，已改为 {fallback_y!r}")
            spec.y_field = fallback_y

    # 饼图类目过多时改柱状图。
    # 这是数据可视化的硬约束：饼图超过 8 块就没法读了
    if spec.chart_type == "pie" and result.row_count > MAX_PIE_CATEGORIES:
        notes.append(
            f"结果有 {result.row_count} 个类目，饼图无法清晰展示，已改为柱状图"
        )
        spec.chart_type = "bar"

    # 折线图要求横轴有序，类目型数据画折线容易误导，这里只在时间维度上允许
    if spec.chart_type == "line" and not _has_time_dimension([spec.x_field]):
        notes.append(f"横轴 {spec.x_field!r} 不是时间维度，折线图易产生误导，已改为柱状图")
        spec.chart_type = "bar"

    # 散点图需要两个数值列
    if spec.chart_type == "scatter":
        if spec.x_field not in columns or spec.y_field not in columns:
            notes.append("散点图字段不合法，已改为表格")
            spec.chart_type = "table"

    return spec, notes


def decide_chart_spec(
    question: str,
    result: QueryResult,
    verbose: bool = False,
) -> tuple[ChartSpec, list[str]]:
    """
    决定图表配置。先让模型判断，失败则规则兜底。

    参数:
        question 用户提问
        result   查询结果
        verbose  是否打印过程

    返回:
        (图表配置, 修正说明列表)
    """
    # 结果不可用时不需要画图
    if not result.success or result.row_count == 0:
        return (
            ChartSpec(
                chart_type="table",
                title="无数据",
                reason="查询结果为空或执行失败，无法生成图表",
            ),
            [],
        )

    # 转成 DataFrame，后续判断列类型、取样例都用它更方便
    frame = pd.DataFrame(result.to_dicts())

    try:
        chain = CHART_PROMPT | get_structured_runnable(
            ChartSpec, temperature=0, max_tokens=400
        )

        spec = chain.invoke(
            {
                "question": question,
                "columns": result.columns,
                "dtypes": {col: _classify_column(frame[col]) for col in frame.columns},
                "row_count": result.row_count,
                # 样例数据给前 5 行就够模型判断结构了
                "sample_text": frame.head(5).to_string(index=False),
            }
        )
    except Exception as exc:  # noqa: BLE001 - 模型调用失败必须有兜底，不能影响主流程
        if verbose:
            print(f"  [图表选择] 模型调用失败（{type(exc).__name__}），使用规则兜底")
        return _fallback_chart_spec(result), [f"模型调用失败，已使用规则兜底：{exc}"]

    # 校验并修正模型的选择
    validated, notes = _validate_chart_spec(spec, result)

    if verbose:
        print("  [图表选择]")
        print(f"    类型：{validated.chart_type}")
        print(f"    字段：x={validated.x_field or '-'} y={validated.y_field or '-'}")
        print(f"    理由：{validated.reason}")
        for note in notes:
            print(f"    修正：{note}")

    return validated, notes


# ===============================================================
# 绘图
# ===============================================================


def _base_layout(title: str) -> dict[str, Any]:
    """
    生成统一的图表布局配置。

    抽成函数是为了保证所有图表风格一致，也避免在每个分支里重复写字体配置。

    参数:
        title 图表标题

    返回:
        Plotly layout 参数字典
    """
    return {
        "title": {"text": title, "x": 0.5, "xanchor": "center", "font": {"size": 18}},
        "font": {"family": CHINESE_FONT, "size": 13},
        "colorway": COLOR_SEQUENCE,
        "margin": {"l": 60, "r": 40, "t": 70, "b": 60},
        "hovermode": "closest",
        # 白底，避免某些主题下背景透明导致截图后看不清
        "plot_bgcolor": "white",
        "paper_bgcolor": "white",
    }


def build_figure(spec: ChartSpec, result: QueryResult) -> go.Figure:
    """
    根据配置生成 Plotly 图表对象。

    参数:
        spec   图表配置
        result 查询结果

    返回:
        Plotly Figure 对象
    """
    frame = pd.DataFrame(result.to_dicts())
    layout = _base_layout(spec.title)

    # ---------------------------------------------------------------
    # 类型一：大数字卡片
    # ---------------------------------------------------------------
    if spec.chart_type == "metric":
        # 取第一行第一列的值
        value = result.rows[0][0] if result.rows and result.rows[0] else "N/A"
        figure = go.Figure(
            go.Indicator(
                mode="number",
                value=float(value) if isinstance(value, (int, float)) else 0,
                # 非数值时用文本模式展示，避免强转失败
                number={"font": {"size": 64, "family": CHINESE_FONT}},
                title={"text": spec.title, "font": {"size": 20, "family": CHINESE_FONT}},
            )
        )
        # 指标卡不需要坐标轴，这里只保留字体配置
        figure.update_layout(
            font={"family": CHINESE_FONT},
            margin=layout["margin"],
            paper_bgcolor="white",
        )
        return figure

    # ---------------------------------------------------------------
    # 类型二：表格
    # ---------------------------------------------------------------
    if spec.chart_type == "table" or not spec.x_field or not spec.y_field:
        figure = go.Figure(
            go.Table(
                # 表头：浅色底 + 深色字
                header={
                    "values": [f"<b>{c}</b>" for c in result.columns],
                    "fill_color": "#E8EEF7",
                    "align": "left",
                    "font": {"size": 13, "family": CHINESE_FONT},
                },
                # 单元格内容
                cells={
                    "values": [
                        ["" if v is None else str(v) for v in column_values]
                        # zip(*rows) 是转置二维列表的惯用写法，
                        # 把"行的列表"变成"列的列表"，正好是 Table 需要的格式
                        for column_values in zip(*result.rows)
                    ],
                    "align": "left",
                    "font": {"size": 12, "family": CHINESE_FONT},
                },
            )
        )
        figure.update_layout(font={"family": CHINESE_FONT}, margin=layout["margin"])
        return figure

    # ---------------------------------------------------------------
    # 类型三：饼图
    # ---------------------------------------------------------------
    if spec.chart_type == "pie":
        figure = go.Figure(
            go.Pie(
                labels=frame[spec.x_field].astype(str),
                values=frame[spec.y_field],
                # 显示百分比和标签
                textinfo="label+percent",
                # 超过 6 块时把标签放到图外，避免互相遮挡
                textposition="outside" if result.row_count <= 6 else "inside",
                hole=0.35,  # 做成环形图，视觉上更清爽
                textfont={"family": CHINESE_FONT, "size": 12},
            )
        )
        figure.update_layout(**layout)
        return figure

    # ---------------------------------------------------------------
    # 类型四：折线图
    # ---------------------------------------------------------------
    if spec.chart_type == "line":
        figure = go.Figure(
            go.Scatter(
                x=frame[spec.x_field].astype(str),
                y=frame[spec.y_field],
                mode="lines+markers",
                name=spec.y_field,
                line={"width": 2.5},
                marker={"size": 7},
                # 悬停时显示完整数值
                hovertemplate=f"%{{x}}<br>{spec.y_field}: %{{y}}<extra></extra>",
            )
        )
        figure.update_xaxes(title_text=spec.x_field, showgrid=False)
        figure.update_yaxes(title_text=spec.y_field, gridcolor="#EEEEEE")
        figure.update_layout(**layout)
        return figure

    # ---------------------------------------------------------------
    # 类型五：散点图
    # ---------------------------------------------------------------
    if spec.chart_type == "scatter":
        figure = go.Figure(
            go.Scatter(
                x=frame[spec.x_field],
                y=frame[spec.y_field],
                mode="markers",
                marker={"size": 9, "opacity": 0.7},
                hovertemplate=f"{spec.x_field}: %{{x}}<br>{spec.y_field}: %{{y}}<extra></extra>",
            )
        )
        figure.update_xaxes(title_text=spec.x_field, showgrid=False)
        figure.update_yaxes(title_text=spec.y_field, gridcolor="#EEEEEE")
        figure.update_layout(**layout)
        return figure

    # ---------------------------------------------------------------
    # 默认：柱状图
    # ---------------------------------------------------------------
    figure = go.Figure(
        go.Bar(
            x=frame[spec.x_field].astype(str),
            y=frame[spec.y_field],
            marker_color=COLOR_SEQUENCE[0],
            # 柱顶显示数值，省得读者去对坐标轴
            text=frame[spec.y_field],
            textposition="outside",
            textfont={"family": CHINESE_FONT, "size": 11},
            hovertemplate=f"%{{x}}<br>{spec.y_field}: %{{y}}<extra></extra>",
        )
    )
    figure.update_xaxes(title_text=spec.x_field, showgrid=False)
    figure.update_yaxes(title_text=spec.y_field, gridcolor="#EEEEEE")
    figure.update_layout(**layout)
    return figure


# ===============================================================
# 整合调用
# ===============================================================


def present(
    question: str,
    result: QueryResult,
    sql: str = "",
    verbose: bool = True,
) -> dict[str, Any]:
    """
    对外接口：给定提问和结果，产出"结论 + 图表"。

    参数:
        question 用户提问
        result   查询结果
        sql      执行的 SQL
        verbose  是否打印过程

    返回:
        含 answer、chart_spec、figure、notes 的字典
    """
    answer = interpret_result(question, result, sql=sql, verbose=verbose)
    spec, notes = decide_chart_spec(question, result, verbose=verbose)
    figure = build_figure(spec, result)

    return {
        "answer": answer,
        "chart_spec": spec,
        "figure": figure,
        "notes": notes,
    }


# ---------------------------------------------------------------
# 自测入口
# ---------------------------------------------------------------
if __name__ == "__main__":
    from pathlib import Path

    # 确保索引存在
    build_index(force=False, verbose=False)

    # 把生成的图表存成 HTML，方便直接双击打开查看。
    # 存到 data/preview_charts 下，不污染项目代码目录
    from app.core.config import PROJECT_ROOT

    output_dir = PROJECT_ROOT / "data" / "preview_charts"
    output_dir.mkdir(parents=True, exist_ok=True)

    print("=" * 78)
    print("结果解读与自动可视化演示")
    print("=" * 78)

    # 这几条问题刻意覆盖不同的图表类型，验证选择逻辑是否正确：
    #   单值      -> metric 大数字卡
    #   时间趋势  -> line 折线图
    #   类目占比  -> pie 饼图
    #   类目对比  -> bar 柱状图
    demo_questions = [
        "一共有多少个客户？",
        "最近六个月每个月的销售额是多少？",
        "各商品类目的总销售额是多少？",
        "各会员等级的客户数量是多少？",
        "销售额最高的 8 个订单分别是多少？",
    ]

    for question in demo_questions:
        # 先走完整链路拿到查询结果
        state = ask_graph(question, verbose=False)
        result = state.get("query_result")

        print()
        print("=" * 78)
        print(f"提问：{question}")
        print("=" * 78)

        if result is None or not result.success:
            print("查询未成功，跳过可视化。")
            continue

        # 结果预览
        print(result.preview(max_rows=6))
        print("-" * 78)

        # 生成结论和图表
        output = present(question, result, sql=state.get("safe_sql", ""), verbose=True)

        print()
        print(f"结论：{output['answer']}")
        print(f"图表类型：{output['chart_spec'].chart_type}")

        # 保存成 HTML。Plotly 的 write_html 会把图表和 JS 一起打进单文件，
        # 不依赖网络，双击就能看
        filename = f"chart_{demo_questions.index(question) + 1}_{output['chart_spec'].chart_type}.html"
        file_path = output_dir / filename
        output["figure"].write_html(str(file_path), include_plotlyjs="cdn")
        print(f"已保存：{file_path}")

    print()
    print("-" * 78)
    print(f"所有图表已保存到：{output_dir}")
    print("双击 HTML 文件即可在浏览器中查看交互式图表。")
    print()
    print("本模块到此完成。下一步做 Streamlit 界面：")
    print("  streamlit run legacy/streamlit_app.py")
