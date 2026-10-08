"""
text-to-sql-agent 后端应用包。

分层约定：
    app/core/    基础设施（配置、数据库访问、模型工厂）
    app/agent/   业务链路（状态定义、建库、Schema 检索、安全校验、结果解读）
"""
