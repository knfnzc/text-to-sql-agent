"""
Schema 向量化与检索。

解决的问题：
    数据库有 5 张表、几十个字段。把所有表结构都塞进 Prompt 有两个毛病：
        1. token 消耗大，每次都带着用不到的字段
        2. 干扰模型判断，表多了容易选错表去 JOIN

    解决思路：把每张表的描述变成向量存进向量库，
    用户提问时先检索"哪几张表和这个问题有关"，只把这部分 Schema 放进 Prompt。

为什么这个方案对 Text-to-SQL 特别有效（面试常问）：
    这是"检索增强生成（RAG）"在非文档场景的典型应用。
    很多人以为 RAG 只能用来查文档，其实凡是"信息量大、每次只用一小部分"的场景都适用。
    表结构就是典型：总量大、按问题只需要一小部分、且语义匹配就能找对。

关于直接用 chromadb 而不用 langchain-chroma：
    用框架封装好的 VectorStore 少写几行代码，但会掩盖"文档怎么切、向量怎么存、
    相似度怎么算"这些细节。这里用原生接口，这些过程在代码里看得见。
    想用封装版的话，装 langchain-chroma 后用 Chroma 类即可，逻辑完全一样。

运行方式：
    python -m app.agent.schema_rag.schema_indexer
"""

import json
from functools import lru_cache
from typing import Any

import chromadb
from chromadb.config import Settings as ChromaClientSettings

from app.core.config import ensure_data_dirs, settings
from app.core.llm import get_embeddings

from app.agent.database.schema_definition import (
    BUSINESS_RULES,
    RELATIONSHIP_DESCRIPTIONS,
    TABLE_DESCRIPTIONS,
    build_schema_text,
    get_table_names,
)

# ===============================================================
# 常量
# ===============================================================

# 集合名。Chroma 里可以有多个集合，类似关系库里的"表"。
# 用带版本号的命名，将来改了文档切分方式可以并存新旧两套索引
COLLECTION_NAME: str = "schema_descriptions_v1"

# 文档标识前缀，避免和将来可能加入的其他类型文档冲突
DOC_ID_PREFIX: str = "table::"


# ===============================================================
# 客户端
# ===============================================================


@lru_cache(maxsize=1)
def get_chroma_client() -> chromadb.ClientAPI:
    """
    获取 Chroma 持久化客户端（进程内单例）。

    返回:
        Chroma 客户端实例

    两个参数说明：
        path                     数据落盘目录。用持久化客户端而不是内存客户端，
                                 这样建一次索引可以反复使用，不用每次跑都重新向量化（那是要花钱的）
        anonymized_telemetry     Chroma 默认会匿名上报使用数据。关掉它，
                                 一方面避免内网环境下的网络超时，另一方面少一个外部请求
    """
    # 确保目录存在，Chroma 不会自动创建父目录
    ensure_data_dirs()

    return chromadb.PersistentClient(
        path=str(settings.chroma_path),
        settings=ChromaClientSettings(
            anonymized_telemetry=False,
            # 允许调用 reset()，调试时方便清空整个库
            allow_reset=True,
        ),
    )


def get_collection():
    """
    获取（或在不存在时创建）Schema 描述集合。

    返回:
        Chroma 集合对象
    """
    return get_chroma_client().get_or_create_collection(
        name=COLLECTION_NAME,
        # 指定用余弦相似度。文本向量检索一般都用余弦，
        # 因为它只看向量方向、不受长度影响，更适合语义相似度比较
        metadata={"hnsw:space": "cosine"},
    )


# ===============================================================
# 文档构造
# ===============================================================


def build_table_document(table_name: str) -> str:
    """
    把一张表的结构描述拼成一段自然语言文本，作为向量化的输入。

    为什么用自然语言而不是结构化 JSON：
        向量模型是在自然语言语料上训练的。
        "订单主表，记录下单时间、金额、状态" 这种描述，
        比 {"table": "orders", "columns": [...]} 更容易和
        "统计上个月的销售额"这种提问匹配上。

    参数:
        table_name 表名

    返回:
        描述该表的文本
    """
    # 复用 schema_definition 里已经写好的拼装逻辑，
    # 保证"检索用的文本"和"放进 Prompt 的文本"完全一致，不会出现描述不同步
    return build_schema_text([table_name])


def build_all_documents() -> tuple[list[str], list[str], list[dict[str, Any]]]:
    """
    构造所有待索引的文档。

    返回:
        (文档ID列表, 文档文本列表, 元数据列表)，三个列表长度一致，下标一一对应

    为什么元数据要和文档分开存：
        元数据用于"检索后按条件过滤"和"取回表名"，
        文档文本用于向量化和展示。分开存比塞在一个大字符串里更灵活。
    """
    ids: list[str] = []
    documents: list[str] = []
    metadatas: list[dict[str, Any]] = []

    for table_name in get_table_names():
        ids.append(f"{DOC_ID_PREFIX}{table_name}")
        documents.append(build_table_document(table_name))
        metadatas.append(
            {
                # 表名：检索命中后靠它还原出"要用哪几张表"
                "table_name": table_name,
                # 表的中文描述，便于人工核对检索结果是否合理
                "description": TABLE_DESCRIPTIONS.get(table_name, ""),
                # 文档类型标记。将来如果要索引"业务规则""示例问答"等其他内容，
                # 靠这个字段区分，检索时可以只查某一类
                "doc_type": "table",
            }
        )

    return ids, documents, metadatas


# ===============================================================
# 建索引
# ===============================================================


def build_index(force: bool = False, verbose: bool = True) -> int:
    """
    构建（或复用）Schema 向量索引。

    参数:
        force   是否强制重建。
                False 且索引已存在且数量正确时直接跳过，
                这样反复跑主流程不会每次都产生向量化的费用
        verbose 是否打印过程信息

    返回:
        索引中的文档数量
    """
    client = get_chroma_client()
    expected_count = len(get_table_names())

    # 先判断能不能复用已有索引
    if not force and not settings.force_reindex:
        try:
            existing = client.get_collection(COLLECTION_NAME)
            if existing.count() == expected_count:
                if verbose:
                    print(f"[跳过] 索引已存在，包含 {existing.count()} 条表描述")
                    print("       如需重建，把 .env 里的 FORCE_REINDEX 改成 true")
                return existing.count()
        except Exception:  # noqa: BLE001 - 集合不存在时会抛异常，属于正常流程
            # 集合不存在，继续往下走正常建库流程
            pass

    # 重建：先删旧集合，避免新旧文档混在一起导致检索结果里出现已删除的表
    try:
        client.delete_collection(COLLECTION_NAME)
        if verbose:
            print("[清理] 已删除旧索引")
    except Exception:  # noqa: BLE001
        pass

    ids, documents, metadatas = build_all_documents()

    if verbose:
        print(f"[向量化] 正在把 {len(documents)} 张表的描述转成向量...")
        print(f"         使用模型：{settings.embedding_model}")

    # 调用向量化接口。embed_documents 适合批量，比循环调 embed_query 快得多
    embeddings = get_embeddings()
    vectors = embeddings.embed_documents(documents)

    if verbose:
        print(f"[写入] 向量维度 {len(vectors[0])}，正在写入 Chroma...")
        # 主动提示维度配置是否正确，这是最常见的配置错误
        if len(vectors[0]) != settings.embedding_dim:
            print(
                f"       [提醒] 实际维度 {len(vectors[0])} 与配置 EMBEDDING_DIM="
                f"{settings.embedding_dim} 不一致，建议同步修改 .env"
            )

    collection = client.get_or_create_collection(
        name=COLLECTION_NAME,
        metadata={"hnsw:space": "cosine"},
    )

    # 一次性写入
    collection.add(
        ids=ids,
        documents=documents,
        embeddings=vectors,
        metadatas=metadatas,
    )

    if verbose:
        print(f"[完成] 索引构建完成，共 {collection.count()} 条")

    return collection.count()


# ===============================================================
# 检索
# ===============================================================


def search_schema(
    question: str,
    top_k: int | None = None,
    verbose: bool = False,
) -> list[dict[str, Any]]:
    """
    根据用户提问检索最相关的表。

    参数:
        question 用户提问
        top_k    召回数量。None 表示用配置里的 SCHEMA_TOP_K
        verbose  是否打印检索详情

    返回:
        检索结果列表，每项包含：
            table_name  表名
            description 表描述
            distance    距离值（越小越相似，余弦距离范围 0-2）
            document    该表的结构描述文本
    """
    # 参数归一化：不传就用配置值
    k = top_k if top_k is not None else settings.schema_top_k

    collection = get_collection()

    # 索引为空说明还没建，给出明确指引而不是让后续步骤报错
    if collection.count() == 0:
        raise RuntimeError(
            "Schema 索引为空。请先运行：\n"
            "  python -m app.agent.schema_rag.schema_indexer"
        )

    # 把提问也转成向量。注意必须用同一个向量模型，
    # 用不同模型生成的向量空间不一致，检索结果会完全错乱
    query_vector = get_embeddings().embed_query(question)

    # n_results 不能超过集合里的文档总数，否则 Chroma 会报错或返回空
    n_results = min(k, collection.count())

    raw = collection.query(
        query_embeddings=[query_vector],
        n_results=n_results,
        # 只要这两项，不需要返回向量本身（返回向量会让结果体积暴涨）
        include=["documents", "metadatas", "distances"],
    )

    results: list[dict[str, Any]] = []

    # Chroma 返回的结构是"批量"形式：外层列表对应每个查询向量，内层列表对应每条结果。
    # 我们只查了一个问题，所以取下标 0
    documents = raw.get("documents") or [[]]
    metadatas = raw.get("metadatas") or [[]]
    distances = raw.get("distances") or [[]]

    for doc, meta, dist in zip(documents[0], metadatas[0], distances[0]):
        results.append(
            {
                "table_name": meta.get("table_name", ""),
                "description": meta.get("description", ""),
                "distance": round(float(dist), 4),
                "document": doc,
            }
        )

    if verbose:
        print(f"[检索] 提问：{question}")
        print(f"[检索] 召回 Top-{n_results}：")
        for item in results:
            print(f"   {item['table_name']:<12} 距离={item['distance']}  {item['description'][:30]}")

    return results


def retrieve_schema_text(
    question: str,
    top_k: int | None = None,
) -> tuple[list[str], str]:
    """
    检索并用命中的表拼出可直接放进 Prompt 的 Schema 文本。

    这是给主流程调用的便捷函数，把"检索"和"拼文本"两步合并。

    参数:
        question 用户提问
        top_k    召回数量

    返回:
        (命中的表名列表, 拼好的 Schema 文本)
    """
    hits = search_schema(question, top_k=top_k)

    # 按检索结果的顺序取表名
    table_names = [item["table_name"] for item in hits]

    # 复用 schema_definition 的拼装函数，保证和索引时用的描述完全一致
    schema_text = build_schema_text(table_names)

    # 业务规则是全局性的（比如"统计销售额要排除已取消订单"），
    # 不适合做向量检索——它们和数据无关，应该无条件带上。
    # 这是一个重要的工程判断：不是所有知识都该走 RAG
    rules_text = "业务口径要求：\n  " + "\n  ".join(f"- {rule}" for rule in BUSINESS_RULES)

    return table_names, f"{schema_text}\n\n{rules_text}"


def get_index_stats() -> dict[str, Any]:
    """
    获取索引统计信息，用于界面展示和自检。

    返回:
        含文档数量、集合名、模型名等信息的字典
    """
    try:
        collection = get_collection()
        count = collection.count()
    except Exception:  # noqa: BLE001
        count = 0

    return {
        "collection_name": COLLECTION_NAME,
        "document_count": count,
        "expected_count": len(get_table_names()),
        "embedding_model": settings.embedding_model,
        "persist_dir": str(settings.chroma_path),
    }


def reset_index() -> None:
    """
    清空整个向量库。调试文档切分方式变化时使用。

    返回:
        无
    """
    # reset 会删除所有集合，不只是本项目的那个。这里单独项目使用，可以接受
    get_chroma_client().reset()
    print("向量库已清空")


# ---------------------------------------------------------------
# 自测入口
# ---------------------------------------------------------------
if __name__ == "__main__":
    print("=" * 78)
    print("Schema 向量化与检索自检")
    print("=" * 78)

    # 第一步：建索引（已存在会自动跳过）
    build_index(force=False)

    print("-" * 78)
    print("索引状态：")
    for key, value in get_index_stats().items():
        print(f"  {key:<18} {value}")

    print("-" * 78)
    print("检索效果验证（重点看召回的表是否合理）：")
    print("-" * 78)

    # 这几条问题刻意覆盖不同场景，用来验证检索质量：
    #   单表问题应该只召回 1 张表
    #   关联问题应该召回多张表
    #   如果某条召回了不相关的表，说明表描述写得不够区分度，需要回去改描述
    demo_questions = [
        "一共有多少客户？",                          # 应该命中 customers
        "各会员等级的平均订单金额",                   # 应该命中 orders + customers
        "销量最高的商品是哪些",                       # 应该命中 order_items + products
        "退款率最高的商品类目",                       # 应该命中 refunds + products + order_items
        "各渠道的销售额占比",                         # 应该命中 orders
    ]

    for question in demo_questions:
        print(f"\n提问：{question}")
        hits = search_schema(question, verbose=False)
        for item in hits:
            print(f"   -> {item['table_name']:<12} 距离={item['distance']}")

    print("\n" + "-" * 78)
    print("拼装后的完整 Prompt 片段示例（问题：各会员等级的平均订单金额）：")
    print("-" * 78)
    tables, text = retrieve_schema_text("各会员等级的平均订单金额")
    print(f"命中表：{tables}")
    print(text)

    print("\n" + "-" * 78)
    print("导出为 JSON 便于人工核对完整文档内容：")
    print("-" * 78)
    _, documents, metadatas = build_all_documents()
    export = [
        {"table": meta["table_name"], "text_length": len(doc), "preview": doc[:80] + "..."}
        for doc, meta in zip(documents, metadatas)
    ]
    print(json.dumps(export, ensure_ascii=False, indent=2))
