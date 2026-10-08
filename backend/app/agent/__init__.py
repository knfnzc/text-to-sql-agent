"""
app.agent 包：Text-to-SQL 智能数据分析 Agent 的业务实现。

按职责划分为四个子包：
    database/     建示例数据库、写测试问题集、提供表结构中文描述
    schema_rag/   Schema 向量化检索 + 基础 Text-to-SQL 链路
    safety/       SQL 安全校验层与带失败自动修正的 LangGraph 状态图
    output/       结果解读与自动可视化

另外 state.py 是跨模块复用的状态定义，整条状态图都基于它。
"""
