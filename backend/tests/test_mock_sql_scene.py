"""MOCK 模式下 SQL 场景剧本的回归测试。

为什么这个测试很重要：
    离线模拟层是"无 API Key 也能演示"的唯一保障。
    如果它给不出可执行的 SQL，安全校验层就会固定拦截，
    整条链路永远是"重试 -> 放弃"，演示页面变成失败页。
    所以这里把"每条样例都必须是能通过安全校验的 SELECT"
    变成一条自动化断言，避免以后改样例时不小心引入非法 SQL。
"""

from __future__ import annotations

import pytest

from app.agent.safety.sql_guard import validate_sql
from app.core.mock_llm import (
    _MOCK_SQL_FALLBACK,
    _MOCK_SQL_SCENES,
    _build_sql_scene_overrides,
    _extract_question,
    _select_sql_scene,
)


def _all_scenes() -> list[dict]:
    """返回全部样例（含兜底），供参数化测试使用。"""
    return [*_MOCK_SQL_SCENES, _MOCK_SQL_FALLBACK]


def test_question_extraction_handles_both_prompt_shapes() -> None:
    """两种提示词写法（意图解析 / SQL 生成）都要能提取出提问。"""
    assert _extract_question("用户提问：一共有多少个客户？") == "一共有多少个客户？"
    assert (
        _extract_question("用户的原始提问：最近三个月销售额\n\n已解析的查询意图：")
        == "最近三个月销售额"
    )


def test_question_extraction_returns_empty_when_absent() -> None:
    """提示词里没有提问时返回空串，而不是抛异常或返回整段提示词。"""
    assert _extract_question("请审核这份报告") == ""
    assert _extract_question("") == ""


def test_specific_keyword_beats_generic_one() -> None:
    """更具体的关键词应当胜出，否则"列举类目"会被"按类目统计"抢走。"""
    scene = _select_sql_scene("有哪些商品类目？")
    assert "DISTINCT products.category" in scene["sql"]


def test_relative_time_scene_uses_max_date_not_current_date() -> None:
    """相对时间必须基于数据内的最大日期推算，不能用 CURRENT_DATE。"""
    scene = _select_sql_scene("最近三个月每个月的销售额是多少？")
    assert "MAX(order_date)" in scene["sql"]
    assert "CURRENT_DATE" not in scene["sql"]


def test_unmatched_question_falls_back() -> None:
    """完全没命中关键词时返回兜底样例，而不是 None。"""
    scene = _select_sql_scene("请帮我随便看看这堆数据")
    assert scene is _MOCK_SQL_FALLBACK


@pytest.mark.parametrize("scene", _all_scenes(), ids=lambda s: s["sql"].splitlines()[0][:40])
def test_every_scene_passes_sql_guard(scene: dict) -> None:
    """每条样例 SQL 都必须能通过安全校验层的全部规则。

    这条断言把"演示可用性"变成了可回归的质量门：
    只要有人往样例库里加了一条含危险关键字或非法表名的 SQL，
    测试立刻会红。
    """
    result = validate_sql(scene["sql"])
    assert result.passed, f"样例被安全校验拦截：{result.errors}"


@pytest.mark.parametrize("scene", _all_scenes(), ids=lambda s: s["sql"].splitlines()[0][:40])
def test_every_scene_uses_only_whitelisted_tables(scene: dict) -> None:
    """样例里声明的 tables 字段必须与 SQL 实际引用的表一致。

    tables 会被填进 SQLDraft.tables_used 展示在界面上，
    如果和实际 SQL 对不上，界面展示的"用到的表"就是错的。
    """
    result = validate_sql(scene["sql"])
    assert set(result.detected_tables) == set(scene["tables"])


def test_sql_field_override_is_wired_into_structured_output() -> None:
    """SQLDraft 的 sql 字段必须被替换成可执行 SQL，而不是占位文本。

    这里动态构造一个类名恰好是 SQLDraft 的替身类：
    mock 层只按类名识别场景，不依赖真实类，
    这样测试就不必拉起 text2sql_chain 的整条导入链，跑得更快也更稳。
    """
    fake_sql_draft = type("SQLDraft", (), {})

    overrides = _build_sql_scene_overrides(
        fake_sql_draft, "用户的原始提问：各商品类目的总销售额是多少？"
    )
    assert "SELECT" in overrides["sql"]
    assert "MOCK 占位内容" not in overrides["sql"]
    assert overrides["tables_used"] == ["orders", "order_items", "products"]


def test_query_intent_override_is_returned_for_intent_schema() -> None:
    """QueryIntent 也要被填充，否则界面上展示的意图解析结果是占位文本。"""
    fake_intent = type("QueryIntent", (), {})

    overrides = _build_sql_scene_overrides(
        fake_intent, "用户的原始提问：各商品类目的总销售额是多少？"
    )
    assert overrides["dimensions"] == ["商品类目"]
    assert overrides["metric"]


def test_non_sql_schema_is_not_affected() -> None:
    """非 SQL 链路的模型不能被 SQL 场景污染。"""

    class ResearchReport:
        """替身：多智能体研究报告项目的报告模型。"""

    assert _build_sql_scene_overrides(ResearchReport, "用户的原始提问：看看客户数") == {}
