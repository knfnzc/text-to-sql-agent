"""
Streamlit 交互界面。

界面结构：
    左侧边栏  配置信息、索引状态、重建索引按钮、示例问题
    主区域    提问输入框、执行过程轨迹、最终 SQL、结果表格、图表、解读结论

一个必须知道的坑（否则你一定会卡住）：
    `streamlit run legacy/streamlit_app.py` 执行时，
    Python 只会把 streamlit_app.py 所在的目录（legacy/）加进模块搜索路径，
    项目根目录不在其中。所以 `from app.core.config import settings` 会直接报
    ModuleNotFoundError。
    解决办法就是在所有项目内导入之前，手动把项目根目录插进 sys.path。
    下面那段 sys.path 处理代码就是干这个的，顺序不能挪到导入语句之后。

运行方式：
    streamlit run legacy/streamlit_app.py
"""

import sys
from pathlib import Path

# ===============================================================
# 路径处理（必须放在所有项目内导入之前）
# ===============================================================
# Path(__file__) 是当前文件的路径
# .resolve() 转成绝对路径
# .parents[1] 往上数两级：
#     parents[0] -> legacy/
#     parents[1] -> backend/  这就是项目根目录
PROJECT_ROOT = Path(__file__).resolve().parents[1]

# 把项目根目录（backend/）插到搜索路径最前面，这样 app 包才能被导入。
# 先判断再插入，避免重复运行时路径列表里堆一长串同样的路径
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# ===============================================================
# 项目内导入（必须在上面那段路径处理之后）
# ===============================================================

import pandas as pd
import streamlit as st

from app.core.config import settings
from app.core.db import check_connection
from app.agent.schema_rag.schema_indexer import (
    build_index,
    get_index_stats,
)
from app.agent.safety.sql_graph import build_graph
from app.agent.output.renderer import build_figure
from app.agent.output.renderer import decide_chart_spec
from app.agent.output.renderer import interpret_result
from app.agent.state import create_initial_state


# ===============================================================
# 页面基础配置
# ===============================================================
# set_page_config 必须是第一个 Streamlit 调用，否则会报错
st.set_page_config(
    page_title="智能数据分析 Agent",
    page_icon="📊",
    layout="wide",  # 用宽屏布局，表格和图表能显示得更清楚
    initial_sidebar_state="expanded",
)


# ===============================================================
# 带缓存的资源加载
# ===============================================================
# 为什么必须用缓存：
#     Streamlit 的机制是"每次交互都重新执行整个脚本"。
#     如果不缓存，每点一次按钮就会重新编译一次状态图，
#     如果还有索引构建之类的高开销操作，界面会卡到没法用。
#     cache_resource 适合缓存"不可序列化的对象"（如编译后的图），
#     cache_data 适合缓存"数据结果"（如统计字典）。
#     这个区别要记清：把不可序列化对象放进 cache_data 会报错。


@st.cache_resource(show_spinner=False)
def get_compiled_graph():
    """
    获取编译好的状态图（进程内只编译一次）。

    返回: 已编译的 LangGraph 图对象
    """
    return build_graph()


@st.cache_data(show_spinner=False, ttl=60)
def get_cached_index_stats() -> dict:
    """
    获取索引统计信息，缓存 60 秒。

    参数 ttl: 缓存有效期。设 60 秒是为了重建索引后能较快刷新状态，
              又不至于每次交互都去查一次向量库

    返回: 统计信息字典
    """
    return get_index_stats()


# ===============================================================
# 侧边栏
# ===============================================================

with st.sidebar:
    st.title("⚙️ 配置与状态")

    # ---------- 模型配置 ----------
    st.subheader("模型")
    # 密钥已在配置层脱敏，这里可以安全展示
    st.code(settings.llm_config_summary, language=None)

    # ---------- 数据库状态 ----------
    st.subheader("数据库")
    if check_connection():
        st.success("数据库连接正常")
    else:
        st.error("数据库连接失败")

    # ---------- 索引状态 ----------
    st.subheader("Schema 索引")
    stats = get_cached_index_stats()

    # 索引文档数与预期表数一致时才算就绪
    if stats["document_count"] == stats["expected_count"] and stats["document_count"] > 0:
        st.success(f"索引就绪，共 {stats['document_count']} 张表")
    else:
        st.warning(
            f"索引未就绪（当前 {stats['document_count']} 条，"
            f"预期 {stats['expected_count']} 条）"
        )

    with st.expander("索引详情"):
        for key, value in stats.items():
            st.text(f"{key}: {value}")

    # 重建索引按钮。
    # 向量化会产生真实费用，所以加了确认勾选，避免误点
    confirm_reindex = st.checkbox("我确认要重建索引（会产生向量化费用）")
    if st.button("重建 Schema 索引", disabled=not confirm_reindex):
        with st.spinner("正在向量化表结构描述..."):
            count = build_index(force=True, verbose=False)
            # 清掉缓存，让界面显示最新的索引状态
            get_cached_index_stats.clear()
        st.success(f"索引重建完成，共 {count} 条")
        st.rerun()

    # ---------- 安全限制展示 ----------
    # 把校验参数展示出来，让使用者知道系统做了什么保护
    st.subheader("安全限制")
    st.text(
        f"行数上限：{settings.sql_row_limit}\n"
        f"执行超时：{settings.sql_timeout_seconds} 秒\n"
        f"最大尝试：{settings.sql_max_retries} 次\n"
        f"Schema 召回：Top-{settings.schema_top_k}"
    )

    # ---------- 示例问题 ----------
    st.subheader("示例问题")
    st.caption("点一下即可填入输入框")

    example_questions = [
        "一共有多少个客户？",
        "各商品类目的总销售额是多少？",
        "最近六个月每个月的销售额是多少？",
        "各会员等级的平均订单金额是多少？",
        "销量最高的 10 个商品是哪些？",
        "退款率最高的商品类目是哪个？",
        "哪些商品从来没有被卖出过？",
        "复购客户占了总客户数的多少比例？",
    ]

    # 用 session_state 记录"点击了哪个示例"，
    # 因为 Streamlit 的按钮点击状态只在一轮执行内有效
    if "selected_example" not in st.session_state:
        st.session_state.selected_example = ""

    for index, example in enumerate(example_questions):
        if st.button(example, key=f"example_{index}", use_container_width=True):
            st.session_state.selected_example = example


# ===============================================================
# 主区域
# ===============================================================

st.title("📊 智能数据分析 Agent")
st.caption(
    "用中文提问，自动生成 SQL 查询、执行、解读结论并画出图表。"
    "底层是 LangGraph 状态图，带 SQL 安全校验与失败自动修正。"
)

# 提问输入区。
# 用 form 包起来的好处是按回车或点按钮提交时只触发一次执行，
# 不包的话输入框每次变化都会重跑整个脚本
with st.form("question_form"):
    question = st.text_input(
        "请输入你的问题",
        value=st.session_state.get("selected_example", ""),
        placeholder="例如：最近三个月各渠道的销售额占比是多少？",
    )
    submitted = st.form_submit_button("开始分析", type="primary", use_container_width=True)


# ===============================================================
# 执行流程
# ===============================================================

if submitted:
    # 输入为空时给提示而不是直接跑，省一次无意义的模型调用
    if not question.strip():
        st.warning("请先输入问题。")
        st.stop()

    # 把本次结果存进 session_state，
    # 这样界面上其他交互（比如展开折叠面板）不会把结果弄丢
    st.session_state.last_question = question

    graph = get_compiled_graph()

    # ---------- 执行，并实时展示进度 ----------
    with st.status("正在分析...", expanded=True) as status:
        st.write("解析意图 → 检索表结构 → 生成 SQL → 安全校验 → 执行查询")

        # invoke 是同步执行，这里包一层为了让用户看到进度提示。
        # 生产环境可以用 stream() 做真正的流式进度展示
        final_state = graph.invoke(create_initial_state(question))

        st.write("完成")

        # 根据最终结果决定状态图标的样式
        success = getattr(final_state.get("query_result"), "success", False)
        status.update(
            label="分析完成" if success else "分析未成功",
            state="complete" if success else "error",
        )

    st.session_state.last_state = final_state

# ===============================================================
# 结果展示区
# ===============================================================

# 从 session_state 取出结果。用 get 防止首次进入时 KeyError
state = st.session_state.get("last_state")

if state:
    st.divider()

    # ---------- 执行轨迹 ----------
    # 放在折叠面板里，需要排查时才展开，不干扰主信息
    with st.expander("执行轨迹", expanded=False):
        for item in state.get("trace", []):
            # 按级别映射成不同图标，错误一眼可见
            icon = {"info": "🔹", "warning": "⚠️", "error": "❌"}.get(
                item.get("level", "info"), "🔹"
            )
            st.markdown(f"{icon} **{item['node']}** — {item['message']}")

    # ---------- SQL 与校验信息 ----------
    left, right = st.columns([3, 2])

    with left:
        st.subheader("生成的 SQL")
        st.code(state.get("safe_sql") or state.get("sql") or "（无）", language="sql")

        # 如果生成过多次，说明发生过重试，把历史列出来
        history = state.get("sql_history") or []
        if len(history) > 1:
            with st.expander(f"查看全部 {len(history)} 次尝试"):
                for attempt, sql in enumerate(history, start=1):
                    st.markdown(f"**第 {attempt} 次**")
                    st.code(sql, language="sql")

    with right:
        st.subheader("安全校验")
        if state.get("validation_passed"):
            st.success("校验通过")
        else:
            st.error("校验未通过")
            for error in state.get("validation_errors") or []:
                st.write(f"- {error}")

        # 展示已执行的检查项和命中的表，体现校验层确实在工作
        rules = state.get("applied_rules") or []
        if rules:
            st.caption(f"已执行检查（{len(rules)} 项）")
            for rule in rules:
                st.write(f"- {rule}")

        tables = state.get("candidate_tables") or []
        if tables:
            st.caption(f"Schema 检索命中：{'、'.join(tables)}")

    st.divider()

    # ---------- 结果、图表、结论 ----------
    result = state.get("query_result")

    if result is None:
        st.info("没有查询结果。")
    elif not result.success:
        st.error(f"查询执行失败：{result.error}")
        st.caption("系统已尝试自动修正。如果仍失败，可以换一种问法再试。")
    else:
        # 结果为空也要正常提示，不要显示一个空表格让人困惑
        if result.row_count == 0:
            st.warning("查询成功，但没有符合条件的数据。建议放宽筛选条件。")
        else:
            # 图表和结论并排展示
            chart_col, answer_col = st.columns([3, 2])

            with chart_col:
                st.subheader("可视化")
                # 生成图表配置和图形。
                # 这里用 try 包住是因为画图依赖数据的具体形态，
                # 极端情况下（比如全是空值）可能失败，
                # 不能因为画不出图就让整个页面报错
                try:
                    spec, notes = decide_chart_spec(
                        state["question"], result, verbose=False
                    )
                    figure = build_figure(spec, result)
                    st.plotly_chart(figure, use_container_width=True)
                    st.caption(f"图表类型：{spec.chart_type}｜选择理由：{spec.reason}")
                    for note in notes:
                        st.caption(f"已修正：{note}")
                except Exception as exc:  # noqa: BLE001
                    st.warning(f"图表生成失败，已降级为表格展示：{exc}")
                    st.dataframe(
                        pd.DataFrame(result.to_dicts()), use_container_width=True
                    )

            with answer_col:
                st.subheader("分析结论")
                with st.spinner("正在生成结论..."):
                    answer = interpret_result(
                        state["question"],
                        result,
                        sql=state.get("safe_sql", ""),
                        verbose=False,
                    )
                # 用 markdown 渲染，结论里的换行能正常显示
                st.markdown(answer)

                # 把关键指标用 metric 组件突出展示，比埋在文字里更醒目
                st.divider()
                col_a, col_b, col_c = st.columns(3)
                col_a.metric("结果行数", result.row_count)
                col_b.metric("查询耗时", f"{result.elapsed_ms:.0f} ms")
                col_c.metric("尝试次数", len(state.get("sql_history") or []))

            # ---------- 原始数据 ----------
            with st.expander("查看原始数据"):
                st.dataframe(
                    pd.DataFrame(result.to_dicts()),
                    use_container_width=True,
                    height=300,
                )

            # ---------- 用量统计 ----------
            usage = state.get("token_usage") or {}
            total_tokens = usage.get("prompt_tokens", 0) + usage.get("completion_tokens", 0)
            st.caption(
                f"本次消耗 token：输入 {usage.get('prompt_tokens', 0)} + "
                f"输出 {usage.get('completion_tokens', 0)} = {total_tokens}"
            )

else:
    # 首次进入时的引导
    st.info(
        "👈 从左侧选一个示例问题，或在上方输入自己的问题后点击「开始分析」。\n\n"
        "建议先试「一共有多少个客户？」这类简单问题确认链路通畅，"
        "再试「退款率最高的商品类目是哪个？」这类需要多表关联的复杂问题。"
    )
