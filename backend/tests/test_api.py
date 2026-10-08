"""HTTP 接口层的集成测试。

为什么用集成测试而不是给每个路由写单元测试：
    路由函数里几乎没有逻辑，"测它"等于测 FastAPI 本身，没有价值。
    真正需要验证的是"前端依赖的那份契约"——路径对不对、字段名对不对、
    SSE 事件顺序对不对。这些只有把应用真正跑起来才测得出来。

测试环境说明：
    用 FastAPI 自带的 TestClient，它不需要真的起一个网络端口，
    直接在进程内构造请求。所以这套测试在任何环境都能跑，
    不依赖开放端口，也不会因为防火墙或代理拦截而失败。
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.api.service import get_agent_graph


@pytest.fixture(scope="module")
def client() -> TestClient:
    """
    整个模块共用一个客户端。

    用 with 语句是为了触发 lifespan（启动时建目录、建索引）。
    不用 with 的话启动逻辑不会执行，测出来的行为和线上不一致。
    作用域设为 module 是因为索引构建不便宜，没必要每个用例重来一遍。
    """
    with TestClient(app) as test_client:
        yield test_client


# ===============================================================
# 元信息接口
# ===============================================================


class TestMetaEndpoints:
    """健康检查与元信息接口。"""

    def test_health_reports_dependencies(self, client: TestClient) -> None:
        """健康检查要能给出数据库与索引的状态。"""
        response = client.get("/api/health")

        assert response.status_code == 200
        payload = response.json()
        assert payload["status"] in ("ok", "degraded")
        assert payload["database_connected"] is True
        assert payload["index_expected_count"] == 5

    def test_meta_exposes_limits_and_examples(self, client: TestClient) -> None:
        """元信息要带安全限制参数和示例问题，前端侧边栏依赖它们。"""
        response = client.get("/api/meta")

        assert response.status_code == 200
        payload = response.json()
        assert payload["example_questions"]
        assert payload["limits"]["sql_row_limit"] > 0

    def test_meta_never_leaks_full_api_key(self, client: TestClient) -> None:
        """模型摘要里绝不能出现完整密钥，只允许前 6 位加掩码。"""
        summary = client.get("/api/meta").json()["model_summary"]

        assert "sk-replace-me" not in summary

    def test_tables_covers_all_business_tables(self, client: TestClient) -> None:
        """数据字典要覆盖全部 5 张业务表，且每张表都有中文说明。"""
        payload = client.get("/api/tables").json()

        assert {table["name"] for table in payload} == {
            "customers",
            "products",
            "orders",
            "order_items",
            "refunds",
        }
        # 每张表都要有描述和字段，否则前端渲染出来是空的
        for table in payload:
            assert table["description"]
            assert table["columns"]

    def test_index_stats_has_ready_flag(self, client: TestClient) -> None:
        """索引统计要带上就绪标记，前端据此决定是否提示重建。"""
        payload = client.get("/api/index/stats").json()

        assert payload["document_count"] == payload["expected_count"]

    def test_questions_endpoint_is_removed(self, client: TestClient) -> None:
        """评测问题集接口已下线，不应再暴露。"""
        assert client.get("/api/questions").status_code == 404


# ===============================================================
# 运行时模型配置
# ===============================================================


@pytest.fixture
def restored_llm_config(client: TestClient):
    """
    每个用例结束后把运行时配置恢复成 .env 的值。

    为什么必须有这个夹具：覆盖层是进程级单例，
    用例改了模型名却不还原，后面的用例就会在一个"被前一个用例改过"的环境里跑，
    失败信息还会指向无辜的用例。
    """
    yield

    client.post("/api/llm/config/reset")


class TestLLMConfigEndpoint:
    """GET/POST /api/llm/config 的契约。"""

    def test_get_exposes_effective_config(self, client: TestClient) -> None:
        """读取接口要给出模型名、接口地址和结构化输出方式，前端表单依赖它们。"""
        response = client.get("/api/llm/config")

        assert response.status_code == 200
        payload = response.json()
        assert payload["model"]
        assert payload["base_url"].startswith("http")
        assert payload["structured_method"] in payload["structured_method_options"]
        # 首次读取时应该还没有任何覆盖项
        assert payload["overridden"] == []

    def test_get_never_returns_raw_api_key(self, client: TestClient) -> None:
        """密钥只能以脱敏形式返回，明文一律不出后端。"""
        payload = client.get("/api/llm/config").json()

        assert "sk-replace-me" not in json.dumps(payload)
        assert "api_key" not in payload
        assert payload["api_key_masked"].endswith("***") or not payload["has_api_key"]

    def test_update_changes_model_and_marks_override(
        self, client: TestClient, restored_llm_config: None
    ) -> None:
        """只提交模型名时，其余字段保持不变，且该字段被标记为已覆盖。"""
        before = client.get("/api/llm/config").json()

        response = client.post("/api/llm/config", json={"model": "deepseek-chat"})

        assert response.status_code == 200
        payload = response.json()
        assert payload["model"] == "deepseek-chat"
        assert payload["overridden"] == ["model"]
        # 没提交的字段不能被清空，否则"只改一项"就会顺手改坏别的配置
        assert payload["base_url"] == before["base_url"]
        assert payload["structured_method"] == before["structured_method"]

    def test_update_is_visible_from_meta(self, client: TestClient, restored_llm_config: None) -> None:
        """换完模型后侧边栏的摘要要跟着变，否则界面显示和实际用的对不上。"""
        client.post("/api/llm/config", json={"model": "qwen-max"})

        assert "qwen-max" in client.get("/api/meta").json()["model_summary"]

    def test_update_rejects_bad_base_url(self, client: TestClient, restored_llm_config: None) -> None:
        """接口地址缺协议头要在写入前拦下，并说明原因。"""
        response = client.post("/api/llm/config", json={"base_url": "api.deepseek.com/v1"})

        assert response.status_code == 422
        assert "http" in response.json()["detail"]

    def test_update_rejects_unknown_structured_method(
        self, client: TestClient, restored_llm_config: None
    ) -> None:
        """结构化输出方式只能是枚举里的值。"""
        response = client.post("/api/llm/config", json={"structured_method": "json_schema_v2"})

        assert response.status_code == 422

    def test_update_rejects_unoverridable_field(
        self, client: TestClient, restored_llm_config: None
    ) -> None:
        """温度不在可覆盖范围内：放开它会破坏 Text-to-SQL 的可复现性。"""
        response = client.post("/api/llm/config", json={"temperature": 0.7})

        # Pydantic 默认忽略未声明的字段，所以这里断言"没有真的生效"
        assert response.status_code == 200
        assert response.json()["overridden"] == []

    def test_reset_restores_env_values(self, client: TestClient) -> None:
        """重置后覆盖项清空，配置回到 .env 的值。"""
        original = client.get("/api/llm/config").json()["model"]
        client.post("/api/llm/config", json={"model": "deepseek-chat"})

        response = client.post("/api/llm/config/reset")

        assert response.status_code == 200
        payload = response.json()
        assert payload["overridden"] == []
        assert payload["model"] == original

    def test_update_rebuilds_compiled_graph(
        self, client: TestClient, restored_llm_config: None
    ) -> None:
        """换配置后必须丢掉已编译的图，否则图里还绑着旧模型实例。"""
        before = get_agent_graph()

        client.post("/api/llm/config", json={"model": "deepseek-chat"})

        assert get_agent_graph() is not before


# ===============================================================
# 同步问答
# ===============================================================


class TestSyncQuery:
    """POST /api/query 的契约。"""

    def test_simple_question_returns_full_payload(self, client: TestClient) -> None:
        """一次完整问答要返回 SQL、数据、图表配置和结论。"""
        response = client.post("/api/query", json={"question": "一共有多少个客户？"})

        assert response.status_code == 200
        payload = response.json()

        assert payload["succeeded"] is True
        assert payload["execution"]["row_count"] >= 1
        assert payload["execution"]["columns"]
        assert payload["validation"]["passed"] is True
        assert "customers" in payload["candidate_tables"]
        assert payload["chart"]["chart_type"] == "metric"

    def test_sql_and_execution_are_consistent(self, client: TestClient) -> None:
        """真正执行的 SQL 必须等于校验层改写后的 safe_sql。"""
        payload = client.post("/api/query", json={"question": "各商品类目的总销售额是多少？"}).json()

        assert payload["safe_sql"]
        # 校验层会强制追加 LIMIT，所以 safe_sql 必须带行数上限
        assert "LIMIT" in payload["safe_sql"].upper()
        assert payload["execution"]["success"] is True

    def test_trace_records_every_node(self, client: TestClient) -> None:
        """执行轨迹要覆盖链路里的关键节点，前端时间线依赖它。"""
        payload = client.post("/api/query", json={"question": "一共有多少个客户？"}).json()

        nodes = {item["node"] for item in payload["trace"]}
        assert {"parse_intent", "retrieve_schema", "generate_sql", "validate", "execute"} <= nodes

    def test_with_answer_false_skips_interpretation(self, client: TestClient) -> None:
        """关闭结论生成时不返回解读文本与图表，省掉两次模型调用。"""
        payload = client.post(
            "/api/query", json={"question": "一共有多少个客户？", "with_answer": False}
        ).json()

        assert payload["answer"] == ""
        assert payload["chart"]["chart_type"] == "table"
        # 数据本身仍然要返回，否则这个参数就失去意义了
        assert payload["execution"]["row_count"] >= 1

    def test_blank_question_is_rejected(self, client: TestClient) -> None:
        """纯空格的问题要在入口被拒绝，不浪费模型调用。"""
        response = client.post("/api/query", json={"question": "   "})

        assert response.status_code == 422

    def test_missing_question_is_rejected(self, client: TestClient) -> None:
        """缺少 question 字段时由 Pydantic 返回 422 而不是 500。"""
        assert client.post("/api/query", json={}).status_code == 422


# ===============================================================
# 流式问答
# ===============================================================


def _read_sse(client: TestClient, url: str) -> list[tuple[str, dict]]:
    """
    读完整条 SSE 流，解析成 (事件名, 数据) 列表。

    参数:
        client 测试客户端
        url    带查询参数的完整路径

    返回:
        [(event, data), ...]
    """
    events: list[tuple[str, dict]] = []

    with client.stream("GET", url) as response:
        assert response.status_code == 200

        event_name = ""
        for line in response.iter_lines():
            if not line:
                continue
            if line.startswith("event:"):
                event_name = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                raw = line.split(":", 1)[1].strip()
                events.append((event_name, json.loads(raw)))

    return events


class TestStreamQuery:
    """GET /api/query/stream 的事件契约。"""

    def test_stream_emits_stages_result_and_done(self, client: TestClient) -> None:
        """流里必须出现 stage、result、done 三类事件。"""
        events = _read_sse(client, "/api/query/stream?question=一共有多少个客户？")

        names = [name for name, _ in events]
        assert "stage" in names
        assert "result" in names
        assert names[-1] == "done"
        assert "error" not in names

    def test_stream_result_matches_sync_contract(self, client: TestClient) -> None:
        """流式最终结果与同步接口的结构必须一致，前端才能共用类型定义。"""
        events = _read_sse(client, "/api/query/stream?question=一共有多少个客户？")

        result = next(data for name, data in events if name == "result")

        assert result["question"] == "一共有多少个客户？"
        assert result["succeeded"] is True
        assert result["execution"]["columns"]

    def test_stream_trace_events_arrive_with_stage(self, client: TestClient) -> None:
        """轨迹事件要跟在 stage 事件之后，说明是边执行边推送的。"""
        events = _read_sse(client, "/api/query/stream?question=一共有多少个客户？")

        names = [name for name, _ in events]
        assert "trace" in names
        # trace 必须排在 result 之前，否则就不是"实时推送"而是"结束后一次性发出"
        assert names.index("trace") < names.index("result")

    def test_stream_reports_render_stage(self, client: TestClient) -> None:
        """开启结论生成时要推送 render 阶段，让用户知道在等什么。"""
        events = _read_sse(
            client, "/api/query/stream?question=一共有多少个客户？&with_answer=true"
        )

        render_stages = [
            data for name, data in events if name == "stage" and data.get("node") == "render"
        ]
        assert render_stages
        assert {item["status"] for item in render_stages} == {"running", "done"}
