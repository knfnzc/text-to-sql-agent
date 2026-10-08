"""
app.agent.schema_rag 子包：Schema 向量化检索与基础链路。

    schema_indexer.py     把表描述向量化存入 Chroma，并支持按提问检索
    text2sql_chain.py     线性版"提问 -> SQL -> 结果"链路（无校验、无重试）
"""
