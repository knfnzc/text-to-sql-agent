"""
app.agent.safety 子包：SQL 安全与流程编排。

    sql_guard.py   独立于模型的安全校验层（八条规则，硬约束）
    sql_graph.py   LangGraph 状态图版链路，带安全校验与失败自动修正
"""
