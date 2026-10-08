"""
其他组件的单元测试。

覆盖三部分：
    TestSchemaDefinition   表结构与描述文本的完整性
    TestStateDefinition    状态定义的正确性（重点测 reducer 字段的初始化）
    TestChartLogic         图表选择的规则兜底与校验修正

这三部分都是纯逻辑，不需要调用模型，所以测试跑得很快，
适合在改代码后频繁运行。

运行方式：
    pytest tests/ -v
"""

import pytest

from app.core.db import QueryResult
from app.agent.database.schema_definition import (
    BUSINESS_RULES,
    COLUMN_DESCRIPTIONS,
    RELATIONSHIP_DESCRIPTIONS,
    TABLE_DESCRIPTIONS,
    build_schema_text,
    get_table_names,
)
from app.agent.state import create_initial_state, summarize_state


# ===============================================================
# 表结构定义
# ===============================================================


class TestSchemaDefinition:
    """表结构定义是整条链路的源头，它的完整性直接影响 SQL 生成质量。"""

    def test_expected_tables_defined(self) -> None:
        """五张核心表都要定义好。"""
        tables = get_table_names()

        assert len(tables) == 5
        for expected in ("customers", "products", "orders", "order_items", "refunds"):
            assert expected in tables

    @pytest.mark.parametrize("table_name", ["customers", "products", "orders", "order_items", "refunds"])
    def test_every_table_has_description(self, table_name: str) -> None:
        """
        每张表都必须有中文描述。

        这条测试看似琐碎，但它守住的是"Schema 检索效果"这条生命线：
        没有描述的表格，向量检索几乎不可能召回正确。
        """
        assert table_name in TABLE_DESCRIPTIONS
        assert len(TABLE_DESCRIPTIONS[table_name]) > 10

    @pytest.mark.parametrize("table_name", ["customers", "products", "orders", "order_items", "refunds"])
    def test_every_column_has_description(self, table_name: str) -> None:
        """每个字段都必须有中文描述，否则模型只能猜字段含义。"""
        assert table_name in COLUMN_DESCRIPTIONS

        # 从 Table 定义里取出真实字段名，逐一核对是否有描述
        from app.agent.database.schema_definition import get_translated_table

        table = get_translated_table(table_name)
        descriptions = COLUMN_DESCRIPTIONS[table_name]

        for column in table.columns:
            assert column.name in descriptions, f"{table_name}.{column.name} 缺少中文描述"

    def test_relationships_documented(self) -> None:
        """外键关系必须写全，这是多表关联正确率的关键。"""
        assert len(RELATIONSHIP_DESCRIPTIONS) == 4

        joined = " ".join(RELATIONSHIP_DESCRIPTIONS)
        # 四条关联关系逐一核对
        assert "orders.customer_id" in joined
        assert "order_items.order_id" in joined
        assert "order_items.product_id" in joined
        assert "refunds.order_id" in joined

    def test_business_rules_not_empty(self) -> None:
        """业务口径规则不能为空，它决定了统计口径是否正确。"""
        assert len(BUSINESS_RULES) >= 3

        joined = " ".join(BUSINESS_RULES)
        # "排除已取消订单"是最重要的一条口径，必须写进去
        assert "已取消" in joined

    def test_build_schema_text_contains_all_parts(self) -> None:
        """拼装出的 Schema 文本应包含指定表的表名、字段名，以及关联关系段落。"""
        text = build_schema_text(["orders", "customers"])

        assert "orders" in text
        assert "customers" in text
        assert "total_amount" in text
        assert "关联关系" in text

        # 未指定的表不能出现在"字段清单"里：
        # 只给出 2 张表的字段定义、却让模型看到第 3 张表的字段，等于白做 Schema 召回。
        # 所以这里断言的是"没有该表的定义块"，用 【表名】 这个段落标题来判定，
        # 以及用该表独有的字段名兜底。
        assert "【refunds】" not in text
        assert "【order_items】" not in text
        assert "refund_amount" not in text

        # 注意：关联关系段落是有意做成全局的。
        # build_schema_text 里写明"关联关系只在查询涉及多表时有意义，
        # 但一次给出成本很低，统一附在最后"，所以 refunds.order_id 这类
        # 描述其他表的关联行会照常出现，不能断言表名完全不出现。
        assert "refunds.order_id" in text

    def test_build_schema_text_relationship_section_is_global(self) -> None:
        """
        关联关系段落是全量的，与传入的表名无关。

        这条测试把上面那条注释里的"设计约定"固定下来：
        以后如果有人把关联关系改成按传入表名过滤，
        这里会失败，从而提醒他这是个有意的行为变更
        （并且需要重新建索引，因为索引里的表文档也是这个函数生成的）。
        """
        partial = build_schema_text(["customers"])

        for relationship in RELATIONSHIP_DESCRIPTIONS:
            assert relationship in partial

    def test_build_schema_text_default_all_tables(self) -> None:
        """不传表名时默认包含全部表。"""
        text = build_schema_text()

        for table_name in get_table_names():
            assert table_name in text


# ===============================================================
# 状态定义
# ===============================================================


class TestStateDefinition:
    """
    状态定义的正确性测试。

    重点是那些带 reducer 的列表字段：
    它们必须在初始状态里就被初始化成空列表，
    否则第一次追加时会报 "NoneType + list" 的错误。
    这个问题很隐蔽，只有真正跑起来才会暴露，所以值得写测试守住。
    """

    def test_initial_state_has_question(self) -> None:
        """初始状态必须带上问题。"""
        state = create_initial_state("测试问题")

        assert state["question"] == "测试问题"

    @pytest.mark.parametrize(
        "field_name",
        ["sql_history", "trace"],
    )
    def test_reducer_lists_initialized(self, field_name: str) -> None:
        """
        带 reducer 的字段必须初始化成空列表。

        参数:
            field_name 字段名
        """
        state = create_initial_state("测试问题")

        assert field_name in state
        assert isinstance(state[field_name], list)
        assert state[field_name] == []

    def test_initial_state_plain_lists_initialized(self) -> None:
        """普通列表字段也要初始化成空列表，避免节点里做 None 判断。"""
        state = create_initial_state("测试问题")

        for field_name in ("candidate_tables", "validation_errors", "applied_rules"):
            assert isinstance(state[field_name], list)

    def test_initial_state_counters(self) -> None:
        """计数器类字段要初始化为 0，布尔量要初始化为 False。"""
        state = create_initial_state("测试问题")

        assert state["retry_count"] == 0
        assert state["validation_passed"] is False
        assert state["is_finished"] is False

    def test_initial_state_token_usage_keys(self) -> None:
        """用量字段必须包含两个键，否则累加时会 KeyError。"""
        state = create_initial_state("测试问题")

        assert "prompt_tokens" in state["token_usage"]
        assert "completion_tokens" in state["token_usage"]

    def test_summarize_state_handles_empty_state(self) -> None:
        """
        摘要函数对"字段缺失的状态"要能容错。

        为什么测这个：摘要函数常用于调试和异常捕获路径，
        那时候状态可能是不完整的。如果它自己先崩了，就失去了调试价值。
        """
        # 只给一个字段，模拟不完整状态
        summary = summarize_state({"question": "x"})

        assert isinstance(summary, str)
        assert "重试次数" in summary


# ===============================================================
# 图表选择逻辑
# ===============================================================


def make_result(columns: list[str], rows: list[list]) -> QueryResult:
    """
    构造一个测试用的 QueryResult。

    抽出这个辅助函数是为了让测试用例更聚焦——
    每个用例只需要写"列是什么、行是什么"，不用重复构造对象。

    参数:
        columns 列名列表
        rows    行数据

    返回:
        QueryResult 实例
    """
    return QueryResult(columns=columns, rows=rows, success=True, executed_sql="SELECT 1")


class TestChartLogic:
    """图表选择的规则兜底与校验修正逻辑。"""

    def test_single_value_becomes_metric(self) -> None:
        """只有一个数值时应选大数字卡片。"""
        from app.agent.output.renderer import _fallback_chart_spec

        spec = _fallback_chart_spec(make_result(["客户数"], [[200]]))

        assert spec.chart_type == "metric"

    def test_time_dimension_becomes_line(self) -> None:
        """时间维度应用折线图。"""
        from app.agent.output.renderer import _fallback_chart_spec

        result = make_result(["月份", "销售额"], [["2026-01", 100], ["2026-02", 200]])
        spec = _fallback_chart_spec(result)

        assert spec.chart_type == "line"
        assert spec.x_field == "月份"
        assert spec.y_field == "销售额"

    def test_category_dimension_becomes_bar(self) -> None:
        """非时间维度应默认用柱状图。"""
        from app.agent.output.renderer import _fallback_chart_spec

        result = make_result(["类目", "销售额"], [["数码", 100], ["家电", 200]])
        spec = _fallback_chart_spec(result)

        assert spec.chart_type == "bar"

    def test_invalid_field_names_corrected(self) -> None:
        """
        模型给的字段名不存在时，校验函数必须修正它。

        这条测试守的是"模型幻觉"这个高频故障：
        模型经常会写一个看起来很像但实际不存在的列名。
        如果不修正，Plotly 会因为拿不到列而抛异常，整个页面报错。
        """
        from app.agent.output.renderer import (
            ChartSpec,
            _validate_chart_spec,
        )

        result = make_result(["类目", "销售额"], [["数码", 100]])
        # 故意给一个不存在的字段名
        bad_spec = ChartSpec(
            chart_type="bar",
            x_field="不存在的列",
            y_field="也存在的列",
            title="测试",
            reason="测试用例",
        )

        fixed, notes = _validate_chart_spec(bad_spec, result)

        # 字段应被修正为真实存在的列
        assert fixed.x_field in result.columns
        assert fixed.y_field in result.columns
        # 必须留下修正记录，便于排查
        assert len(notes) == 2

    def test_too_many_categories_pie_downgraded_to_bar(self) -> None:
        """
        类目过多的饼图应降级为柱状图。

        这是数据可视化的硬约束：饼图超过 8 块就没法读了。
        用代码强制守住这条规则，比在提示词里叮嘱模型可靠得多。
        """
        from app.agent.output.renderer import (
            MAX_PIE_CATEGORIES,
            ChartSpec,
            _validate_chart_spec,
        )

        # 构造超过阈值的数据行
        rows = [[f"类目{i}", i * 10] for i in range(MAX_PIE_CATEGORIES + 5)]
        result = make_result(["类目", "销售额"], rows)

        pie_spec = ChartSpec(
            chart_type="pie",
            x_field="类目",
            y_field="销售额",
            title="占比",
            reason="测试用例",
        )

        fixed, notes = _validate_chart_spec(pie_spec, result)

        assert fixed.chart_type == "bar"
        assert any("饼图" in note for note in notes)

    def test_line_chart_on_non_time_axis_downgraded(self) -> None:
        """
        非时间维度上的折线图应降级为柱状图。

        原因：类目之间没有顺序关系，用折线连接会暗示一种不存在的趋势，
        属于典型的图表误导。
        """
        from app.agent.output.renderer import (
            ChartSpec,
            _validate_chart_spec,
        )

        result = make_result(["类目", "销售额"], [["数码", 100], ["家电", 200]])
        line_spec = ChartSpec(
            chart_type="line",
            x_field="类目",
            y_field="销售额",
            title="趋势",
            reason="测试用例",
        )

        fixed, notes = _validate_chart_spec(line_spec, result)

        assert fixed.chart_type == "bar"
        assert any("折线图" in note for note in notes)
