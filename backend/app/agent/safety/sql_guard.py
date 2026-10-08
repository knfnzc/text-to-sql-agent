"""
SQL 安全校验层：独立于模型做硬约束。

这是本项目最有技术含量的一个模块，也是面试时最值得展开讲的部分。

要解决的问题：
    让大模型直接生成 SQL 并执行，等于把一个"会犯错、可能被诱导"的程序
    直接接到生产数据库上。风险来自三个方向：
        1. 模型自己犯错：本该 SELECT 却写成 DELETE
        2. 用户恶意诱导：通过提示词注入让它执行 DROP TABLE
        3. 无意的性能事故：SELECT * 拉回整张表，把数据库拖垮

设计原则（重要，面试常问）：
    校验层必须是"独立于模型"的代码，不能靠提示词里写"你只能生成 SELECT"来约束。
    提示词是软约束，模型可能不遵守；代码层面的白名单是硬约束。
    这个"软约束 + 硬约束分层"的思路，是把 LLM 应用做上生产的关键。

八条校验规则（按执行顺序）：
    1. 空语句检查        拒绝空输入
    2. 单语句检查        拒绝分号拼接的多条语句（绕过检测的常见手法）
    3. 注释剥离与注释检查
                         剥离注释后再校验，防止用注释把危险关键字拆开；
                         同时单独扫描注释内容，拦截"把写操作藏进注释"的注入尝试
    4. 语句类型白名单    只允许 SELECT / WITH
    5. 危险关键字拦截    黑名单扫描（在屏蔽字符串字面量之后）
    6. 系统表拦截        禁止访问 sqlite_master、information_schema 等元数据表
    7. 表名白名单        只允许访问业务表、CTE 名和子查询别名
    8. 行数强制限制      自动追加或收紧 LIMIT，防止全表拉取

已知局限（诚实说明比假装完美更专业）：
    - 危险关键字扫描基于文本匹配，虽然做了字符串字面量屏蔽，
      但极端构造（如用 CHAR() 拼接关键字）仍可能绕过。
      生产环境应该叠加数据库账号权限控制——只给只读账号，这才是最后一道防线。
    - 表名提取基于 sqlparse 的解析结果，遇到复杂嵌套子查询时可能漏判或误判。
      这里用"子查询别名识别 + CTE 名提取"做了缓解，但并非完备。

运行方式：
    python -m app.agent.safety.sql_guard
"""

import re
from dataclasses import dataclass, field

import sqlparse
from sqlparse.sql import Identifier, IdentifierList, TokenList
from sqlparse.tokens import Keyword

from app.core.config import settings

from app.agent.database.schema_definition import get_table_names


# ===============================================================
# 规则配置
# ===============================================================

# 危险关键字黑名单。
# 选取原则：宁可严格，因为误报只是让合法查询被拒（用户体验问题），
# 漏报则可能导致数据被破坏（生产事故）。
# 注意：这里不包含 UNION、REPLACE、LOAD 等词，
# 因为它们在合法查询里也会出现（UNION 用于合并结果集），加进去会误伤。
DANGEROUS_KEYWORDS: tuple[str, ...] = (
    "INSERT",
    "UPDATE",
    "DELETE",
    "DROP",
    "TRUNCATE",
    "ALTER",
    "CREATE",
    "GRANT",
    "REVOKE",
    "ATTACH",
    "DETACH",
    "PRAGMA",
    "VACUUM",
    "REINDEX",
    "MERGE",
    "CALL",
    "EXEC",
    "EXECUTE",
    "COPY",
    "INTO",
    "OUTFILE",
    "DUMPFILE",
    "SHUTDOWN",
)

# 危险函数黑名单。
# 主要是两类：让数据库空转的（DoS）和读文件的。
DANGEROUS_FUNCTIONS: tuple[str, ...] = (
    "SLEEP",
    "BENCHMARK",
    "PG_SLEEP",
    "WAITFOR",
    "LOAD_FILE",
    "LOAD_EXTENSION",
    "RANDOMBLOB",
    "FTS3_TOKENIZER",
)

# 系统表/元数据表黑名单。
# 这些表能读到数据库结构甚至其他库的内容，属于不该暴露给业务查询的范畴。
SYSTEM_TABLE_PATTERNS: tuple[str, ...] = (
    "sqlite_master",
    "sqlite_schema",
    "sqlite_temp_master",
    "information_schema",
    "pg_catalog",
    "pg_tables",
    "pg_class",
    "performance_schema",
    "mysql.user",
    "sys.",
)

# 允许访问的业务表白名单。从表结构定义处取，避免两处维护
ALLOWED_TABLES: frozenset[str] = frozenset(name.lower() for name in get_table_names())


# ===============================================================
# 结果数据结构
# ===============================================================


@dataclass
class GuardResult:
    """
    SQL 校验结果。

    为什么不直接返回布尔值：
        调用方需要的不只是"过没过"，还需要知道"为什么没过"和"改成了什么"。
        错误信息要回传给模型做自我修正，改写后的 SQL 要拿去执行，
        命中的规则要在界面上展示。这些都需要结构化承载。
    """

    # 是否通过校验
    passed: bool = False

    # 通过校验后的可执行 SQL。
    # 注意它可能和输入的 SQL 不同——校验层会自动追加或收紧 LIMIT
    safe_sql: str = ""

    # 拦截原因列表。只要这个列表非空，passed 就是 False
    errors: list[str] = field(default_factory=list)

    # 通过的规则名列表，用于在界面上展示"做了什么检查"
    applied_rules: list[str] = field(default_factory=list)

    # 非致命的提醒。例如"SQL 里含有注释，已剥离"，不阻止执行但值得记录
    warnings: list[str] = field(default_factory=list)

    # 从 SQL 中解析出的表名，便于排查"为什么说这张表不在白名单里"
    detected_tables: list[str] = field(default_factory=list)

    def summary(self) -> str:
        """
        生成可读的校验摘要。

        返回: 多行字符串
        """
        lines = [f"校验结果：{'通过' if self.passed else '未通过'}"]

        if self.errors:
            lines.append("拦截原因：")
            for error in self.errors:
                lines.append(f"  - {error}")

        if self.warnings:
            lines.append("提醒：")
            for warning in self.warnings:
                lines.append(f"  - {warning}")

        if self.detected_tables:
            lines.append(f"识别到的表：{', '.join(self.detected_tables)}")

        if self.applied_rules:
            lines.append(f"已执行检查：{', '.join(self.applied_rules)}")

        if self.safe_sql:
            lines.append("可执行 SQL：")
            lines.append(f"  {self.safe_sql}")

        return "\n".join(lines)


# ===============================================================
# 辅助解析函数
# ===============================================================


def mask_string_literals(sql: str) -> str:
    """
    把 SQL 里的字符串字面量替换成空字符串占位，用于关键字扫描。

    为什么需要这一步：
        假设查询是 SELECT * FROM orders WHERE payment_method = 'DROP'
        关键字扫描会把 'DROP' 当成危险关键字而误报。
        把字面量屏蔽掉再扫描，就能避免这类误报。

    实现说明：
        单引号字符串内部，两个连续单引号表示一个转义的引号（SQL 标准做法），
        所以正则里要处理 '' 的情况，不能简单用 '[^']*'。

    参数:
        sql 原始 SQL

    返回:
        字面量被替换成 '' 的 SQL
    """
    # (?:[^']|'')* 表示：非引号字符，或一对连续单引号，重复任意次
    pattern = re.compile(r"'(?:[^']|'')*'")
    return pattern.sub("''", sql)


# 注释匹配：行注释 -- 直到行尾；块注释 /* ... */（用 DOTALL 让 . 能跨行）
_COMMENT_PATTERN = re.compile(r"--[^\n]*|/\*.*?\*/", re.DOTALL)


def extract_comment_text(sql: str) -> str:
    """
    把 SQL 里的注释内容单独提取出来。

    为什么剥离注释之后还要再看一眼注释：
        规则 3 会把注释剥掉再做关键字扫描，这是对的（防止 DE/*x*/LETE 拆词绕过）。
        但剥离之后注释里写了什么就再也看不到了，于是

            SELECT * FROM orders WHERE 1=1 -- 后接 DROP TABLE orders

        这种"把写操作塞进注释"的注入尝试会被当成一条干净的 SELECT 放行。
        单次 execute 时数据库确实会忽略注释，但这段 SQL 一旦被写进日志、
        被复制进数据库客户端、或者走了 executescript，DROP 就会真的执行。
        校验层的定位是"宁可严格"，所以这里对注释内容再扫一遍。

    参数:
        sql 原始 SQL（注释尚未剥离）

    返回:
        所有注释内容拼接成的文本
    """
    # 先屏蔽字符串字面量，避免把 WHERE note = 'a--b' 里的值误当成注释
    masked = mask_string_literals(sql)
    return " ".join(match.group(0) for match in _COMMENT_PATTERN.finditer(masked))


def find_dangerous_keywords(text: str) -> list[str]:
    """
    扫描文本里出现的危险关键字。

    参数:
        text 待扫描文本。内部会先屏蔽字符串字面量，
             避免把 SELECT * FROM orders WHERE payment_method = 'DROP' 里的值误报

    返回:
        命中的关键字列表，顺序与 DANGEROUS_KEYWORDS 的定义顺序一致
    """
    masked_upper = mask_string_literals(text).upper()

    # \b 是词边界，保证匹配的是完整单词而不是单词片段。
    # 例如 created_at 里虽然有 CREATE，但因为有下划线连接，
    # \bCREATE\b 不会匹配到它
    return [
        keyword
        for keyword in DANGEROUS_KEYWORDS
        if re.search(rf"\b{keyword}\b", masked_upper)
    ]


def find_dangerous_functions(text: str) -> list[str]:
    """
    扫描文本里出现的危险函数调用。

    参数:
        text 待扫描文本

    返回:
        命中的函数名列表
    """
    masked_upper = mask_string_literals(text).upper()

    # 函数名后面必须跟左括号才算调用，避免把名字里含 SLEEP 的字段误判
    return [
        function
        for function in DANGEROUS_FUNCTIONS
        if re.search(rf"\b{function}\s*\(", masked_upper)
    ]


def extract_cte_names(sql: str) -> set[str]:
    """
    提取 WITH 子句里定义的 CTE（公共表表达式）名称。

    为什么需要：
        形如 WITH monthly AS (SELECT ...) SELECT * FROM monthly 的语句，
        表名提取器会把 monthly 当成一张真实表，从而在白名单检查时误报。
        CTE 名字是当前语句内自己定义的临时结果集，应当允许。

    参数:
        sql 已剥离注释的 SQL

    返回:
        CTE 名称集合（统一转小写）
    """
    names: set[str] = set()

    # 只在 WITH 开头时才做这个提取，避免把普通 SQL 里误匹配到 "xxx AS ("
    stripped = sql.lstrip()
    if not stripped.upper().startswith("WITH"):
        return names

    # 匹配形如 "名字 AS (" 的结构，两个易错点：
    #   1. 紧跟在 WITH 后面的第一个 CTE，前面是关键字 WITH 而不是逗号。
    #      早期版本只认"行首或逗号"，导致第一个 CTE 永远提取不到，
    #      WITH m AS (...) SELECT * FROM m 里的 m 会被当成未知表拦截。
    #      所以这里必须把 WITH 也作为合法的前导。
    #   2. WITH RECURSIVE x AS (...) 这种写法，要在 WITH 和名字之间跳过 RECURSIVE。
    # 另外用 \b 词边界，并允许反引号/双引号包裹的标识符。
    pattern = re.compile(
        r"(?:\bWITH\b(?:\s+RECURSIVE\b)?|,)\s*[`\"]?(\w+)[`\"]?\s+AS\s*\(",
        re.IGNORECASE,
    )

    for match in pattern.finditer(stripped):
        names.add(match.group(1).lower())

    return names


def extract_table_names(sql: str) -> list[str]:
    """
    从 SQL 中提取被引用的表名。

    实现方式：用 sqlparse 把 SQL 解析成语法树，递归遍历 token，
    遇到 FROM / JOIN 关键字后紧接着的标识符就是表名。

    为什么必须递归下钻：
        早期版本只遍历最外层的 token，于是

            SELECT * FROM (SELECT * FROM secret_salary_table) t

        里藏在子查询中的表名一个都提不出来。这不只是"漏判"，
        而是表名白名单可以被绕过——把越权表塞进子查询就能躲过检查。
        所以这里对括号子查询、以及"带子查询的标识符"都做递归，
        保证任何层级出现的表名都会被纳入白名单校验。

    参数:
        sql 已剥离注释的 SQL

    返回:
        表名列表（保持出现顺序，可能包含重复项）
    """
    tables: list[str] = []

    for statement in sqlparse.parse(sql):
        _collect_table_names(statement, tables)

    return tables


def _collect_table_names(token_list: TokenList, tables: list[str]) -> None:
    """
    递归遍历一个 token 列表，把 FROM / JOIN 后紧跟的标识符收集为表名。

    参数:
        token_list sqlparse 的 token 列表（语句、括号子查询、标识符都是这种结构）
        tables     收集结果，原地追加
    """
    # 标记"下一个标识符是表名"
    expect_table = False

    for token in token_list.tokens:
        # 跳过空白，否则空白 token 会把 expect_table 状态消耗掉
        if token.is_whitespace:
            continue

        if expect_table:
            if isinstance(token, IdentifierList):
                # 形如 FROM a, b, c 的情况
                for identifier in token.get_identifiers():
                    name = identifier.get_real_name()
                    if name:
                        tables.append(name.lower())
            elif isinstance(token, Identifier):
                name = token.get_real_name()
                if name:
                    tables.append(name.lower())
            # 子查询（Parenthesis 类型）没有 real_name，get_real_name() 返回 None，
            # 会被上面的判断跳过，这正是我们想要的行为
            expect_table = False

        # 判断当前 token 是否是 FROM 或 JOIN 类关键字。
        # 注意 sqlparse 会把 "LEFT JOIN" 当成一个完整的 Keyword token，
        # 所以用 endswith("JOIN") 来判断更稳妥
        if token.ttype is Keyword:
            value = token.value.upper()
            if value == "FROM" or value.endswith("JOIN"):
                expect_table = True

        # 递归下钻：括号子查询、以及把子查询包在里面的标识符
        # （形如 (SELECT ...) AS t 会被解析成 Identifier 节点）里都可能藏着真实表名。
        # 注意递归用的是独立的 expect_table 状态，不会与外层互相干扰
        if isinstance(token, TokenList):
            _collect_table_names(token, tables)


def is_subquery_alias(sql: str, name: str) -> bool:
    """
    判断一个名字是不是子查询的别名。

    为什么需要：
        SELECT * FROM (SELECT ...) AS t 里，t 是子查询别名而不是真实表。
        表名提取器在部分情况下可能把 t 提出来，白名单检查就会误报。
        这里用"名字是否紧跟在右括号之后"来判断。

    参数:
        sql  已剥离注释的 SQL
        name 待判断的名字

    返回:
        是子查询别名返回 True
    """
    pattern = re.compile(r"\)\s*(?:AS\s+)?" + re.escape(name) + r"\b", re.IGNORECASE)
    return bool(pattern.search(sql))


def enforce_row_limit(sql: str, row_limit: int) -> tuple[str, str]:
    """
    强制给 SQL 加上行数限制。

    两种处理方式：
        已有 LIMIT 且小于等于上限 -> 保留不变
        已有 LIMIT 但大于上限     -> 收紧到上限
        没有 LIMIT               -> 自动追加

    为什么必须做这件事：
        缺少 LIMIT 的查询在大表上会拉回全部数据，
        轻则响应缓慢，重则把数据库内存打满。
        这是生产环境最常见的事故之一，而且模型生成 SQL 时经常忘写 LIMIT。

    参数:
        sql       已校验通过的 SQL
        row_limit 行数上限

    返回:
        (处理后的 SQL, 本次操作的说明文字)
    """
    # 找现有的 LIMIT 数字。
    # 用 group(1) 单独捕获数字，方便精确替换而不影响其他部分
    match = re.search(r"\bLIMIT\s+(\d+)", sql, flags=re.IGNORECASE)

    if match:
        current = int(match.group(1))
        if current > row_limit:
            # 只替换数字本身，保留语句其余部分
            new_sql = sql[: match.start(1)] + str(row_limit) + sql[match.end(1):]
            return new_sql, f"原有 LIMIT {current} 超过上限，已收紧为 {row_limit}"
        return sql, f"已有 LIMIT {current}，未超过上限"

    # 没有 LIMIT 就追加。
    # 直接拼接在末尾对 SELECT 语句是正确的，因为 LIMIT 在语法上就是最后的子句
    return f"{sql.rstrip()}\nLIMIT {row_limit}", f"缺少 LIMIT，已自动追加 LIMIT {row_limit}"


# ===============================================================
# 主校验函数
# ===============================================================


def validate_sql(raw_sql: str, row_limit: int | None = None) -> GuardResult:
    """
    对 SQL 执行完整的安全校验。

    参数:
        raw_sql   模型生成的原始 SQL
        row_limit 行数上限，None 表示使用配置里的 SQL_ROW_LIMIT

    返回:
        GuardResult 实例。注意：不抛异常，所有问题都封在返回值里，
        这样调用方可以把错误信息回传给模型做自我修正
    """
    limit = row_limit if row_limit is not None else settings.sql_row_limit
    result = GuardResult()

    # ---------------------------------------------------------------
    # 规则 1：空语句检查
    # ---------------------------------------------------------------
    sql = (raw_sql or "").strip()
    if not sql:
        result.errors.append("SQL 为空")
        return result

    # 去掉结尾的分号。分号本身无害，但留着会影响后面追加 LIMIT
    sql = sql.rstrip().rstrip(";").strip()

    # ---------------------------------------------------------------
    # 规则 2：单语句检查
    # ---------------------------------------------------------------
    # 多条语句是绕过检测的经典手法：
    #   SELECT * FROM orders; DROP TABLE orders;
    # 只看第一个 SELECT 会以为安全，实际第二条会执行
    statements = [s for s in sqlparse.split(sql) if s.strip()]
    if len(statements) > 1:
        result.errors.append(
            f"检测到 {len(statements)} 条语句，只允许执行单条查询语句"
        )
        return result
    result.applied_rules.append("单语句检查")

    # ---------------------------------------------------------------
    # 规则 3：注释剥离
    # ---------------------------------------------------------------
    # 注释可能被用来切断关键字（如 DE/*x*/LETE）从而绕过扫描。
    # 这里的策略是：先把注释剥掉，后续所有检查都基于剥离后的内容。
    cleaned = sqlparse.format(sql, strip_comments=True).strip()
    if cleaned != sql:
        result.warnings.append("SQL 中包含注释，已剥离后再做安全校验")

        # 剥离之后注释里写了什么就看不见了，所以这里必须对注释内容单独扫一遍。
        # 典型场景：SELECT * FROM orders WHERE 1=1 -- 后接 DROP TABLE orders
        # 剥离注释后只剩一条干净的 SELECT，后面所有规则都会放行。
        comment_text = extract_comment_text(sql)
        comment_hits = find_dangerous_keywords(comment_text) + find_dangerous_functions(
            comment_text
        )
        if comment_hits:
            result.errors.append(
                f"注释中检测到危险关键字或函数：{', '.join(comment_hits)}。"
                "注释内容不允许包含写操作或危险函数调用"
            )
            return result
    result.applied_rules.append("注释剥离")

    # 注释剥离后可能变成空串，需要再检查一次
    if not cleaned:
        result.errors.append("剥离注释后 SQL 为空")
        return result

    # ---------------------------------------------------------------
    # 规则 4：语句类型白名单
    # ---------------------------------------------------------------
    # get_type() 会返回 SELECT / INSERT / UPDATE / DELETE / UNKNOWN
    # 注意 CTE 语句（WITH ... SELECT）的类型可能是 UNKNOWN，
    # 所以 UNKNOWN 时要额外检查是否以 WITH 开头
    parsed_statements = sqlparse.parse(cleaned)
    statement_type = parsed_statements[0].get_type() if parsed_statements else "UNKNOWN"
    first_keyword = cleaned.split()[0].upper() if cleaned.split() else ""

    allowed_type = statement_type == "SELECT" or first_keyword in ("SELECT", "WITH")

    if not allowed_type:
        result.errors.append(
            f"只允许执行 SELECT 查询，检测到语句类型为「{statement_type}」"
        )
        return result
    result.applied_rules.append("语句类型白名单（仅 SELECT/WITH）")

    # ---------------------------------------------------------------
    # 规则 5：危险关键字拦截
    # ---------------------------------------------------------------
    # 扫描前会先屏蔽字符串字面量，避免把 'DROP' 这种值当成关键字误报，
    # 具体判定逻辑见 find_dangerous_keywords
    hit_keywords = find_dangerous_keywords(cleaned)

    if hit_keywords:
        result.errors.append(
            f"检测到危险关键字：{', '.join(hit_keywords)}"
        )
        return result
    result.applied_rules.append("危险关键字拦截")

    # 危险函数单独检查（判定条件：函数名后紧跟左括号），见 find_dangerous_functions
    hit_functions = find_dangerous_functions(cleaned)

    if hit_functions:
        result.errors.append(f"检测到危险函数：{', '.join(hit_functions)}")
        return result
    result.applied_rules.append("危险函数拦截")

    # ---------------------------------------------------------------
    # 规则 6：系统表拦截
    # ---------------------------------------------------------------
    hit_system_tables = [
        pattern for pattern in SYSTEM_TABLE_PATTERNS if pattern.lower() in cleaned.lower()
    ]
    if hit_system_tables:
        result.errors.append(
            f"禁止访问系统表或元数据表：{', '.join(hit_system_tables)}"
        )
        return result
    result.applied_rules.append("系统表拦截")

    # ---------------------------------------------------------------
    # 规则 7：表名白名单
    # ---------------------------------------------------------------
    detected = extract_table_names(cleaned)
    result.detected_tables = detected

    # CTE 名和子查询别名都是语句内自己定义的，不属于越权访问
    cte_names = extract_cte_names(cleaned)

    unknown_tables: list[str] = []
    for table in set(detected):
        if table in ALLOWED_TABLES:
            continue
        if table in cte_names:
            continue
        if is_subquery_alias(cleaned, table):
            continue
        unknown_tables.append(table)

    if unknown_tables:
        result.errors.append(
            f"引用了未授权的表：{', '.join(sorted(unknown_tables))}。"
            f"允许的表：{', '.join(sorted(ALLOWED_TABLES))}"
        )
        return result
    result.applied_rules.append("表名白名单")

    # ---------------------------------------------------------------
    # 规则 8：行数强制限制
    # ---------------------------------------------------------------
    safe_sql, limit_note = enforce_row_limit(cleaned, limit)
    result.warnings.append(limit_note)
    result.applied_rules.append(f"行数限制（上限 {limit}）")

    # 所有检查通过
    result.passed = True
    result.safe_sql = safe_sql

    return result


def is_sql_safe(sql: str) -> bool:
    """
    便捷函数：只想知道能不能执行。

    参数:
        sql 待校验的 SQL

    返回:
        通过校验返回 True
    """
    return validate_sql(sql).passed


# ---------------------------------------------------------------
# 自测入口：用一批"该拦截"和"该放过"的 SQL 验证规则是否生效
# ---------------------------------------------------------------
if __name__ == "__main__":
    print("=" * 78)
    print("SQL 安全校验层自测")
    print("=" * 78)
    print(f"行数上限：{settings.sql_row_limit}")
    print(f"业务表白名单：{sorted(ALLOWED_TABLES)}")
    print()

    # 每项是 (说明, SQL, 期望结果)
    test_cases: list[tuple[str, str, bool]] = [
        # ---------- 应该放过 ----------
        ("正常的单表查询", "SELECT COUNT(*) AS 客户数 FROM customers", True),
        ("正常的关联查询", "SELECT c.name, SUM(o.total_amount) FROM orders o JOIN customers c ON o.customer_id = c.customer_id GROUP BY c.name", True),
        ("带子查询别名", "SELECT t.省份, t.人数 FROM (SELECT province AS 省份, COUNT(*) AS 人数 FROM customers GROUP BY province) t", True),
        ("CTE 写法", "WITH monthly AS (SELECT substr(order_date, 1, 7) AS m, SUM(total_amount) AS amt FROM orders GROUP BY m) SELECT * FROM monthly ORDER BY m", True),
        ("字面量里含危险词（不应误报）", "SELECT * FROM orders WHERE payment_method = 'DROP'", True),
        ("已有合理的 LIMIT", "SELECT * FROM products ORDER BY unit_price DESC LIMIT 5", True),
        ("LEFT JOIN 场景", "SELECT p.product_name FROM products p LEFT JOIN order_items i ON p.product_id = i.product_id WHERE i.item_id IS NULL", True),

        # ---------- 应该拦截 ----------
        ("写操作 DELETE", "DELETE FROM orders WHERE status = '已取消'", False),
        ("写操作 UPDATE", "UPDATE orders SET status = '已完成'", False),
        ("写操作 INSERT", "INSERT INTO customers (name) VALUES ('test')", False),
        ("删表 DROP", "DROP TABLE orders", False),
        ("清表 TRUNCATE", "TRUNCATE TABLE orders", False),
        ("多语句拼接", "SELECT * FROM orders; DROP TABLE orders;", False),
        ("注释绕过尝试", "SELECT * FROM orders WHERE 1=1 -- ; DROP TABLE orders", False),
        ("访问系统表", "SELECT name FROM sqlite_master WHERE type = 'table'", False),
        ("未授权的表", "SELECT * FROM secret_salary_table", False),
        ("危险函数", "SELECT SLEEP(100)", False),
        ("SELECT INTO 建表", "SELECT * INTO backup_orders FROM orders", False),
        ("空语句", "   ", False),
    ]

    passed_cases = 0
    for description, sql, expected in test_cases:
        result = validate_sql(sql)
        ok = result.passed == expected
        passed_cases += 1 if ok else 0

        # 用符号直观标记是否符合预期
        flag = "OK  " if ok else "FAIL"
        expectation = "应通过" if expected else "应拦截"
        outcome = "通过" if result.passed else "拦截"

        print(f"[{flag}] {description}")
        print(f"       期望：{expectation}　实际：{outcome}")
        if not result.passed:
            print(f"       原因：{result.errors[0]}")
        if result.passed and result.warnings:
            print(f"       提醒：{result.warnings[-1]}")
        print()

    print("=" * 78)
    print(f"用例通过：{passed_cases} / {len(test_cases)}")
    print("=" * 78)

    # 单独展示一次完整的校验过程，看清每一步做了什么
    print()
    print("完整校验过程示例：")
    print("-" * 78)
    demo_sql = "SELECT c.name, SUM(o.total_amount) AS 消费额 FROM orders o JOIN customers c ON o.customer_id = c.customer_id GROUP BY c.name ORDER BY 消费额 DESC LIMIT 2000"
    print(f"输入 SQL：{demo_sql}")
    print()
    print(validate_sql(demo_sql).summary())
