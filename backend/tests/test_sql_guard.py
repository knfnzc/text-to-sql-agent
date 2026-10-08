"""
SQL 安全校验层的单元测试。

这是整个项目里最值得写测试的模块，原因有两个：
    1. 它是纯函数逻辑，没有外部依赖，测试起来快且稳定
    2. 它承担安全职责，一旦有漏洞后果严重，
       "哪些必须拦住"这件事必须有明确的、可重复执行的验证

测试策略：分两组
    TestShouldPass   正常查询必须放行，防止校验过严导致功能不可用
    TestShouldBlock  危险操作必须拦截，这是安全底线

为什么用 pytest.mark.parametrize：
    把"输入 + 期望结果"写成表格形式，新增用例只需加一行数据，
    不用复制粘贴测试函数。这是单元测试里最实用的一个技巧。

运行方式：
    pytest tests/ -v
"""

import pytest

from app.core.config import settings
from app.agent.safety.sql_guard import (
    ALLOWED_TABLES,
    enforce_row_limit,
    extract_cte_names,
    extract_table_names,
    is_sql_safe,
    mask_string_literals,
    validate_sql,
)


# ===============================================================
# 必须放行的查询
# ===============================================================


class TestShouldPass:
    """
    正常查询必须通过校验。

    为什么这组测试和拦截组同样重要：
        一个只会"全都拦下"的校验层是没用的。
        校验过严会让正常功能不可用，属于严重的质量问题。
    """

    @pytest.mark.parametrize(
        "description,sql",
        [
            ("最简单的单表统计", "SELECT COUNT(*) AS 客户数 FROM customers"),
            ("带排序和限制", "SELECT product_name FROM products ORDER BY unit_price DESC LIMIT 5"),
            ("两表关联", "SELECT c.name FROM orders o JOIN customers c ON o.customer_id = c.customer_id"),
            ("LEFT JOIN 反连接", "SELECT p.product_name FROM products p LEFT JOIN order_items i ON p.product_id = i.product_id WHERE i.item_id IS NULL"),
            ("三表关联加聚合", "SELECT p.category, SUM(i.quantity) FROM order_items i JOIN products p ON i.product_id = p.product_id JOIN orders o ON i.order_id = o.order_id GROUP BY p.category"),
            ("子查询作为数据源", "SELECT t.province, t.cnt FROM (SELECT province, COUNT(*) AS cnt FROM customers GROUP BY province) t"),
            ("WITH 子句", "WITH m AS (SELECT substr(order_date,1,7) AS mon, SUM(total_amount) AS amt FROM orders GROUP BY mon) SELECT * FROM m"),
            ("使用窗口函数", "SELECT customer_id, RANK() OVER (ORDER BY SUM(total_amount) DESC) AS rk FROM orders GROUP BY customer_id"),
            ("字符串字面量含危险词", "SELECT * FROM orders WHERE payment_method = 'DROP'"),
            ("中文别名", "SELECT province AS 省份, COUNT(*) AS 人数 FROM customers GROUP BY province"),
        ],
    )
    def test_normal_query_passes(self, description: str, sql: str) -> None:
        """
        参数化测试：正常查询应当通过校验。

        参数:
            description 用例说明，测试失败时能立刻知道是哪条
            sql         待校验的 SQL
        """
        result = validate_sql(sql)

        # 断言通过，并把错误信息放进断言消息里，
        # 这样失败时输出中会直接显示被拦截的原因，不用再去调试
        assert result.passed, f"用例「{description}」被误拦截：{result.errors}"


# ===============================================================
# 必须拦截的操作
# ===============================================================


class TestShouldBlock:
    """
    危险操作必须被拦截。这是安全底线，任何一条漏掉都是严重问题。
    """

    @pytest.mark.parametrize(
        "description,sql",
        [
            ("删除数据", "DELETE FROM orders"),
            ("删除全部数据", "DELETE FROM orders WHERE 1=1"),
            ("更新数据", "UPDATE orders SET status = '已完成'"),
            ("插入数据", "INSERT INTO customers (name) VALUES ('x')"),
            ("删除表", "DROP TABLE orders"),
            ("清空表", "TRUNCATE TABLE orders"),
            ("修改表结构", "ALTER TABLE orders ADD COLUMN note TEXT"),
            ("创建表", "CREATE TABLE backdoor (id INT)"),
            ("分号拼接多条语句", "SELECT * FROM orders; DROP TABLE orders"),
            ("注释绕过尝试", "SELECT * FROM orders WHERE 1=1 -- 后接 DROP TABLE orders"),
            ("访问系统表", "SELECT name FROM sqlite_master WHERE type='table'"),
            ("访问其他库的表", "SELECT * FROM secret_table"),
            ("SELECT INTO 建表", "SELECT * INTO backup FROM orders"),
            ("危险函数造成拒绝服务", "SELECT SLEEP(100)"),
            ("危险函数读文件", "SELECT LOAD_FILE('/etc/passwd')"),
            # 下面两条是历史漏洞的回归用例：表名提取器若不递归下钻子查询，
            # 把越权表藏进子查询就能绕过表名白名单，所以必须固定验证会被拦截
            ("子查询里的越权表", "SELECT * FROM (SELECT * FROM secret_table) t"),
            ("EXISTS 子查询里的越权表", "SELECT * FROM orders WHERE EXISTS (SELECT 1 FROM secret_table)"),
            ("空语句", ""),
            ("只有空白字符", "   \n  "),
        ],
    )
    def test_dangerous_sql_blocked(self, description: str, sql: str) -> None:
        """
        参数化测试：危险操作应当被拦截。

        参数:
            description 用例说明
            sql         待校验的 SQL
        """
        result = validate_sql(sql)

        assert not result.passed, f"用例「{description}」未被拦截，存在安全风险"
        # 拦截时必须给出原因，否则模型无法据此修正，用户也不知道为什么被拒
        assert result.errors, f"用例「{description}」被拦截但没有给出原因"


# ===============================================================
# 测试各个辅助函数
# ===============================================================


class TestHelperFunctions:
    """测试解析辅助函数的正确性。这些函数是校验逻辑的基础。"""

    def test_mask_string_literals(self) -> None:
        """字符串字面量应当被替换掉，避免误报危险关键字。"""
        sql = "SELECT * FROM orders WHERE name = 'DROP TABLE' AND city = '北京'"
        masked = mask_string_literals(sql)

        # 字面量内容应该消失
        assert "DROP TABLE" not in masked
        assert "北京" not in masked
        # 但 SQL 结构应该保留
        assert "SELECT" in masked
        assert "FROM" in masked

    def test_mask_handles_escaped_quote(self) -> None:
        """SQL 里用两个连续单引号表示转义，屏蔽逻辑要能正确处理。"""
        # 'It''s a test' 是一个完整的字符串字面量
        sql = "SELECT * FROM orders WHERE note = 'It''s a test' AND id = 1"
        masked = mask_string_literals(sql)

        assert "It" not in masked
        # 后面的条件不应被误吞
        assert "id = 1" in masked

    def test_extract_table_names_simple(self) -> None:
        """单表查询应能提取出表名。"""
        tables = extract_table_names("SELECT COUNT(*) FROM customers")
        assert "customers" in tables

    def test_extract_table_names_with_join(self) -> None:
        """JOIN 场景应能提取出所有涉及的表。"""
        sql = (
            "SELECT * FROM orders o "
            "JOIN customers c ON o.customer_id = c.customer_id "
            "LEFT JOIN refunds r ON o.order_id = r.order_id"
        )
        tables = extract_table_names(sql)

        assert "orders" in tables
        assert "customers" in tables
        assert "refunds" in tables

    def test_extract_table_names_inside_subquery(self) -> None:
        """子查询里的表名也必须被提取出来。

        这是安全相关的断言：如果子查询里的表名提不出来，
        表名白名单就会被"把越权表藏进子查询"这一手法绕过。
        """
        sql = (
            "SELECT ROUND(AVG(item_count), 2) FROM ("
            "  SELECT orders.order_id, COUNT(*) AS item_count"
            "  FROM orders JOIN order_items ON orders.order_id = order_items.order_id"
            "  GROUP BY orders.order_id"
            ")"
        )
        tables = extract_table_names(sql)

        assert set(tables) == {"orders", "order_items"}

    def test_extract_table_names_nested_subquery(self) -> None:
        """多层嵌套子查询要能一路下钻到底。"""
        sql = "SELECT * FROM (SELECT * FROM (SELECT * FROM customers) a) b"
        tables = extract_table_names(sql)

        assert "customers" in tables

    def test_extract_table_names_in_cte_body(self) -> None:
        """CTE 主体里的表名同样要提取出来，否则 WITH 写法可以绕过白名单。"""
        sql = (
            "WITH m AS (SELECT customer_id, SUM(total_amount) AS amt FROM orders GROUP BY customer_id) "
            "SELECT * FROM m"
        )
        tables = extract_table_names(sql)

        assert "orders" in tables
        assert "m" in tables

    def test_extract_cte_names(self) -> None:
        """应能提取出 WITH 子句定义的 CTE 名称。"""
        sql = (
            "WITH monthly AS (SELECT 1), quarterly AS (SELECT 2) "
            "SELECT * FROM monthly"
        )
        names = extract_cte_names(sql)

        assert "monthly" in names
        assert "quarterly" in names

    def test_extract_cte_names_ignores_non_cte(self) -> None:
        """不是以 WITH 开头的语句不应提取出 CTE 名称。"""
        # 这条 SQL 里也有 "AS (" 形式，但它不是 CTE 定义
        sql = "SELECT (SELECT 1) AS x FROM (SELECT 2 AS y) t"
        names = extract_cte_names(sql)

        assert names == set()


class TestRowLimit:
    """测试行数强制限制。这是防止全表拉取的关键机制。"""

    def test_append_limit_when_missing(self) -> None:
        """没有 LIMIT 时应自动追加。"""
        sql, note = enforce_row_limit("SELECT * FROM orders", 500)

        assert "LIMIT 500" in sql
        assert "追加" in note

    def test_tighten_limit_when_too_large(self) -> None:
        """LIMIT 超过上限时应收紧。"""
        sql, note = enforce_row_limit("SELECT * FROM orders LIMIT 100000", 500)

        assert "LIMIT 500" in sql
        assert "100000" not in sql
        assert "收紧" in note

    def test_keep_limit_when_within_bounds(self) -> None:
        """LIMIT 在上限之内时应保持原样，不要多此一举地改动用户意图。"""
        sql, note = enforce_row_limit("SELECT * FROM orders LIMIT 10", 500)

        assert "LIMIT 10" in sql
        assert "未超过" in note


class TestGuardResultStructure:
    """测试校验结果的结构是否完整。"""

    def test_passed_result_has_safe_sql(self) -> None:
        """通过校验时，必须给出可执行的 SQL。"""
        result = validate_sql("SELECT * FROM customers")

        assert result.passed
        # safe_sql 可能和输入不同（被追加了 LIMIT），但绝不能为空
        assert result.safe_sql
        assert "LIMIT" in result.safe_sql

    def test_passed_result_records_applied_rules(self) -> None:
        """通过校验时要记录执行了哪些检查，便于界面上展示。"""
        result = validate_sql("SELECT * FROM customers")

        assert len(result.applied_rules) >= 5
        # 关键规则必须出现在列表里
        assert any("白名单" in rule for rule in result.applied_rules)

    def test_blocked_result_has_errors_and_no_safe_sql(self) -> None:
        """拦截时必须给出原因，且不应提供可执行 SQL。"""
        result = validate_sql("DROP TABLE orders")

        assert not result.passed
        assert result.errors
        # 被拦截时不能有 safe_sql，否则调用方可能误执行
        assert result.safe_sql == ""

    def test_detected_tables_recorded(self) -> None:
        """通过校验时应记录识别到的表，便于排查问题。"""
        result = validate_sql("SELECT * FROM orders JOIN customers ON 1=1")

        assert "orders" in result.detected_tables
        assert "customers" in result.detected_tables


class TestConfigurationConsistency:
    """测试代码里的常量与配置是否一致。这类测试能防住"改了配置忘了改代码"。"""

    def test_allowed_tables_matches_schema(self) -> None:
        """白名单必须和表结构定义保持一致。"""
        from app.agent.database.schema_definition import get_table_names

        assert ALLOWED_TABLES == set(get_table_names())

    def test_row_limit_from_config(self) -> None:
        """校验层使用的行数上限应来自配置，而不是硬编码。"""
        result = validate_sql("SELECT * FROM orders")

        assert str(settings.sql_row_limit) in result.safe_sql


class TestConvenienceFunction:
    """测试便捷函数的返回值语义。"""

    def test_is_sql_safe_true(self) -> None:
        assert is_sql_safe("SELECT * FROM customers") is True

    def test_is_sql_safe_false(self) -> None:
        assert is_sql_safe("DROP TABLE customers") is False


# ---------------------------------------------------------------
# 直接运行本文件时的快速自检
# ---------------------------------------------------------------
if __name__ == "__main__":
    print("请用 pytest 运行完整测试：")
    print("  pytest tests/ -v")
    print()

    # 顺手演示几条关键用例，没装 pytest 时也能看结果
    cases = [
        ("SELECT COUNT(*) FROM customers", True),
        ("DELETE FROM orders", False),
        ("SELECT * FROM orders; DROP TABLE orders", False),
        ("SELECT * FROM orders WHERE payment_method = 'DROP'", True),
    ]
    for sql, expected in cases:
        result = validate_sql(sql)
        ok = result.passed == expected
        print(f"[{'OK  ' if ok else 'FAIL'}] 期望{'通过' if expected else '拦截'} | {sql[:60]}")
