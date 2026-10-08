"""
统一模型工厂模块。

作用：全项目的模型实例都从这里创建，业务代码不直接 new 模型对象。

为什么必须收口到一处：
    1. 换模型只改 .env，不用全局搜索替换
    2. 超时、重试、温度这些"防护性参数"统一设置，不会某个文件漏配
    3. 调用量统计、成本控制这些横切逻辑有统一的挂载点

使用方式：
    from app.core.llm import get_chat_model, get_embeddings

    llm = get_chat_model()                    # 用 .env 里的默认配置
    llm_json = get_chat_model(temperature=0)  # 单独覆盖某个参数
"""

from functools import lru_cache
from typing import Any

from langchain_openai import ChatOpenAI, OpenAIEmbeddings

from app.core.config import settings
from app.core.runtime_config import get_runtime_config


# ===============================================================
# 模型创建
# ===============================================================


@lru_cache(maxsize=16)
def _build_chat_model(
    model: str,
    base_url: str,
    api_key: str,
    temperature: float,
    max_tokens: int,
    timeout: int,
    max_retries: int,
    disable_thinking: bool,
) -> ChatOpenAI:
    """
    实际创建对话模型实例，带缓存。

    为什么参数是"一个个散开的"而不是传一个配置对象：
        lru_cache 要求参数可哈希，配置对象里含 dict/list 时无法缓存。
        把参数拆成基础类型，缓存才能生效。

    为什么 base_url 和 api_key 也必须进参数列表：
        它们既是"连接身份"，也是缓存键的一部分。如果只作为函数体内的全局变量读取，
        用户在界面上换了密钥之后，相同 model/temperature 会命中旧实例，
        实际请求仍然带着旧密钥——表现为"改了没生效"，而且极难排查。

    为什么要缓存：
        模型实例内部持有 HTTP 连接池，重复创建会浪费连接、拖慢响应。
        同一个进程内相同参数只建一次。

    参数:
        model            模型名称
        base_url         接口地址
        api_key          密钥
        temperature      采样温度
        max_tokens       单次回复最大 token 数
        timeout          请求超时秒数
        max_retries      SDK 层自动重试次数
        disable_thinking 是否显式关闭思考模式

    返回:
        配置完成的 ChatOpenAI 实例
    """
    # 密钥为空时立刻报错，并给出可操作的提示。
    # 这样错误发生在启动阶段，而不是跑到一半才抛一个含糊的认证失败
    if not api_key:
        raise ValueError(
            "未配置 LLM_API_KEY。请在侧栏「更换模型 / 密钥」里填写，"
            "或复制 .env.example 为 .env 并填写密钥。\n"
            "  PowerShell: Copy-Item .env.example .env"
        )

    return ChatOpenAI(
        # 模型名称，决定能力上限和价格
        model=model,
        # 接口地址，已由配置层校验过格式并去掉末尾斜杠
        base_url=base_url,
        # 密钥
        api_key=api_key,
        # 采样温度，0 表示尽量确定性输出
        temperature=temperature,
        # 回复长度上限
        max_tokens=max_tokens,
        # 单次请求超时
        timeout=timeout,
        # SDK 内部重试次数
        max_retries=max_retries,
        # 关闭思考模式。为什么需要这个开关：
        #   DeepSeek V4 默认开启思考模式，但思考模式下温度会被静默忽略
        #   （本项目依赖温度为 0 的可复现性），思考过程本身占用 max_tokens，
        #   更关键的是此时不接受被强制指定的 tool_choice，
        #   结构化输出会直接 400。
        #   实测把 thinking 放在 extra_body 里能生效，放在 model_kwargs 里不行
        #   （后者会被当成 chat.completions 的顶层关键字而报 TypeError）。
        extra_body=(
            {"thinking": {"type": "disabled"}} if disable_thinking else None
        ),
    )


def get_chat_model(
    model: str | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    timeout: int | None = None,
    max_retries: int | None = None,
) -> ChatOpenAI:
    """
    获取对话模型实例（业务代码统一用这个函数）。

    所有参数都是"可选覆盖"：传 None 表示用 .env 里的配置，传值表示临时覆盖。
    这样既支持全局统一配置，也支持个别节点特殊化。

    离线模拟开关：
        当 .env 里 LLM_MOCK=true 时，本函数返回 app/core/mock_llm.py 里的
        模拟模型（接口一致，不联网、零成本、产出带 MOCK 标记）。
        好处是业务代码完全不用改，一行配置就能在"真实模型"和"离线替身"之间切换——
        这正是"依赖抽象而非实现"带来的灵活性。

    参数:
        model       模型名称，None 表示使用当前生效配置里的模型
        temperature 采样温度，None 表示使用 settings.llm_temperature
                   典型用法：生成 SQL 用 0，写报告这类创意任务用 0.3-0.7
        max_tokens  回复长度上限，None 表示使用 settings.llm_max_tokens
                   需要输出长文（如完整研究报告）时调大，否则会被截断
        timeout     超时秒数，None 表示使用 settings.llm_timeout
        max_retries 重试次数，None 表示使用 settings.llm_max_retries

    返回:
        真实模式下是 ChatOpenAI 实例；模拟模式下是 MockChatModel 实例。
        两者接口一致，业务代码无需区分。
    """
    # 取"当前生效"的配置：.env 打底，被界面覆盖过的字段以覆盖层为准
    runtime = get_runtime_config()

    # 解析出本次调用的最终参数，模拟模式也需要它们（用于日志区分）
    resolved_temperature = (
        temperature if temperature is not None else settings.llm_temperature
    )
    resolved_max_tokens = max_tokens if max_tokens is not None else settings.llm_max_tokens

    # 模拟模式：延迟导入，避免不用 Mock 时也把 mock 模块加载进来
    if runtime.mock:
        from app.core.mock_llm import get_mock_chat_model

        return get_mock_chat_model(
            temperature=resolved_temperature,
            max_tokens=resolved_max_tokens,
            label="mock-chat",
        )

    return _build_chat_model(
        # 逐个字段做"覆盖优先，否则取默认"的判断
        model if model is not None else runtime.model,
        runtime.base_url,
        runtime.api_key,
        resolved_temperature,
        resolved_max_tokens,
        timeout if timeout is not None else settings.llm_timeout,
        max_retries if max_retries is not None else settings.llm_max_retries,
        runtime.disable_thinking,
    )


def get_structured_runnable(
    schema: type[Any],
    *,
    temperature: float = 0.0,
    max_tokens: int | None = None,
    include_raw: bool = True,
):
    """
    获取"结构化输出"链，并按配置选择实现方式。

    为什么需要这一层封装：
        with_structured_output 有三种实现方式，选错了会在调用时报 400，
        而且错误信息完全不提"实现方式"这件事，很容易排查半天：
            json_schema      用 response_format 约束，约束最强，
                             但 DeepSeek 直接拒绝：This response_format type is unavailable now
            function_calling 走工具调用，兼容性最好（默认值）
            json_mode        走 response_format=json_object，
                             要求 Prompt 里出现 json 字样，否则报错
        把选择权收口到配置项 LLM_STRUCTURED_METHOD，换服务商只改 .env；
        也可以在界面上临时切换，见 runtime_config 模块。

    注意：这里不缓存。结构化链内部持有模型实例，
    缓存会导致界面换配置后仍复用旧模型（图本身有缓存，见 api/service.py）。

    参数:
        schema       目标 Pydantic 模型
        temperature  采样温度，结构化输出场景默认 0
        max_tokens   回复长度上限，None 表示用 .env 里的默认值
        include_raw  是否同时返回原始消息（要统计 token 用量必须为 True）

    返回:
        可直接 invoke 的 Runnable
    """
    runtime = get_runtime_config()
    return get_chat_model(temperature=temperature, max_tokens=max_tokens).with_structured_output(
        schema,
        include_raw=include_raw,
        method=runtime.structured_method,
    )


@lru_cache(maxsize=4)
def _build_embeddings(
    model: str,
    api_key: str,
    base_url: str,
) -> OpenAIEmbeddings:
    """
    实际创建向量化模型实例，带缓存。

    参数:
        model    向量化模型名称
        api_key  密钥（已解析出最终值）
        base_url 接口地址（已解析出最终值）

    返回:
        配置完成的 OpenAIEmbeddings 实例
    """
    return OpenAIEmbeddings(
        model=model,
        api_key=api_key,
        base_url=base_url,
        # 向量化接口单次请求可能较慢，给一个宽松的超时
        timeout=60,
        # 网络抖动时自动重试
        max_retries=3,
    )


def get_embeddings() -> OpenAIEmbeddings:
    """
    获取向量化模型实例。

    这里用的是配置层解析后的"最终值"：
    单独的 EMBEDDING_API_KEY / EMBEDDING_BASE_URL 优先，
    没配则自动复用 LLM 的配置，避免重复填写。

    离线模拟开关：
        当 .env 里 LLM_MOCK=true 时返回 MockEmbeddings。
        它的实现是"字符二元组 + 哈希落桶 + L2 归一化"，
        相似文本的向量确实会更接近，所以离线模式下语义去重
        是**真的在起作用**，不只是走个过场。

        另外还有独立的 EMBEDDING_MOCK 开关，作用是把"向量化"单独留在替身上。
        为什么需要它：DeepSeek 这类服务只提供对话模型、没有向量化接口，
        此时对话可以走真实模型，向量化却没有真实实现可用，
        不单独留一个开关的话，Schema 检索会整条链路失败。

    返回:
        真实模式下是 OpenAIEmbeddings 实例；模拟模式下是 MockEmbeddings 实例。
    """
    if settings.llm_mock or settings.embedding_mock:
        from app.core.mock_llm import get_mock_embeddings

        return get_mock_embeddings()

    return _build_embeddings(
        settings.embedding_model,
        settings.resolved_embedding_api_key,
        settings.resolved_embedding_base_url,
    )


# ===============================================================
# 调用量统计与成本控制
# ===============================================================


class TokenUsageTracker:
    """
    Token 用量追踪器。

    用途：多智能体研究报告项目的"总 token 预算"功能依赖它。
    每轮 Agent 调用后把用量累加进来，超过预算就中断循环，
    避免出现"审核一直不通过 -> 无限迭代 -> 账单失控"的情况。

    为什么需要它：LLM 应用的成本是隐性的。一个设计不好的反馈循环，
    单次任务可能跑出十几万 token，如果不设上限，跑一晚上代价很可观。

    使用方式：
        tracker = TokenUsageTracker(budget=100000)
        tracker.add(prompt_tokens=1200, completion_tokens=800)
        if tracker.is_exhausted:
            break
        print(tracker.summary())
    """

    def __init__(
        self,
        budget: int,
        prompt_price_per_1k: float = 0.0,
        completion_price_per_1k: float = 0.0,
    ) -> None:
        """
        初始化追踪器。

        参数:
            budget                  总 token 预算上限，超过即视为耗尽
            prompt_price_per_1k     输入 token 每千个的价格（用于估算成本，可填 0 不统计）
            completion_price_per_1k 输出 token 每千个的价格（同上）
        """
        # 预算上限，来自配置
        self.budget: int = budget
        # 累计输入 token
        self.prompt_tokens: int = 0
        # 累计输出 token
        self.completion_tokens: int = 0
        # 单价，仅用于成本估算展示
        self.prompt_price_per_1k: float = prompt_price_per_1k
        self.completion_price_per_1k: float = completion_price_per_1k
        # 记录每次调用的明细，便于事后分析哪个环节最费钱
        self.records: list[dict[str, Any]] = []

    @property
    def total_tokens(self) -> int:
        """累计消耗的总 token 数（输入 + 输出）。"""
        return self.prompt_tokens + self.completion_tokens

    @property
    def is_exhausted(self) -> bool:
        """
        预算是否已耗尽。

        返回: 累计 token 数 >= 预算上限时返回 True
        """
        return self.total_tokens >= self.budget

    @property
    def estimated_cost(self) -> float:
        """
        估算成本（单位与单价配置一致，通常是美元）。

        返回: 估算金额；未配置单价时返回 0.0
        """
        return (
            self.prompt_tokens / 1000 * self.prompt_price_per_1k
            + self.completion_tokens / 1000 * self.completion_price_per_1k
        )

    def add(
        self,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        label: str = "",
    ) -> None:
        """
        累加一次调用的用量。

        参数:
            prompt_tokens     本次调用的输入 token 数
            completion_tokens 本次调用的输出 token 数
            label             本次调用的来源标识（如节点名），用于事后分析

        返回: 无
        """
        # 累加总量
        self.prompt_tokens += prompt_tokens
        self.completion_tokens += completion_tokens
        # 记明细，方便回答"钱花在哪个节点上了"这个问题
        self.records.append(
            {
                "label": label,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
            }
        )

    def add_from_message(self, message: Any, label: str = "") -> None:
        """
        从 LangChain 返回的消息对象里提取用量并累加。

        为什么单独写一个方法：不同服务商返回的用量字段位置不完全一样，
        统一在这里做兼容处理，业务代码就不用关心这些差异。

        参数:
            message 模型返回的消息对象（AIMessage 等）
            label   来源标识

        返回: 无
        """
        # usage_metadata 是 LangChain 标准的用量字段，形如：
        # {"input_tokens": 100, "output_tokens": 50, "total_tokens": 150}
        usage = getattr(message, "usage_metadata", None)
        if isinstance(usage, dict):
            self.add(
                # 兼容不同命名：有的叫 input_tokens，有的叫 prompt_tokens
                prompt_tokens=int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0),
                completion_tokens=int(
                    usage.get("output_tokens") or usage.get("completion_tokens") or 0
                ),
                label=label,
            )

    def summary(self) -> str:
        """
        生成可读的用量摘要，适合打印到日志或界面。

        返回: 一行摘要字符串
        """
        cost_part = f"，估算成本 {self.estimated_cost:.4f}" if self.estimated_cost > 0 else ""
        return (
            f"token 用量：输入 {self.prompt_tokens} + 输出 {self.completion_tokens} "
            f"= {self.total_tokens} / 预算 {self.budget}"
            f"（{self.total_tokens / self.budget * 100:.1f}%）{cost_part}"
        )

    def reset(self) -> None:
        """清空所有统计，用于开始一个新任务。"""
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.records.clear()


# ===============================================================
# 连通性自检
# ===============================================================


def check_llm_connection() -> bool:
    """
    检查模型是否可正常调用。

    用途：环境验证。花 10 秒钟确认配置正确，
    比写了两小时代码才发现密钥填错要好得多。

    返回: 调用成功返回 True，失败返回 False（异常已捕获并打印原因）
    """
    # 模拟模式下不存在"连通性"问题，直接通过并提示当前处于离线模式。
    # 这样自检入口在两种模式下行为一致，不会因为没配 Key 就报红。
    if get_runtime_config().mock:
        print("[跳过] 当前为离线模拟模式（LLM_MOCK=true），无需真实调用")
        return True

    try:
        llm = get_chat_model(max_tokens=32)
        # 发一个最短的请求，只要模型能回复就说明链路通了
        response = llm.invoke("回复两个字：正常")
        print(f"[成功] 模型响应：{response.content}")
        return True
    except Exception as exc:  # noqa: BLE001 - 这里是自检入口，需要捕获所有异常并说明原因
        print(f"[失败] 模型调用出错：{type(exc).__name__}: {exc}")
        print("排查顺序：1) LLM_API_KEY 是否填写 2) LLM_BASE_URL 是否正确 3) 网络是否可通")
        return False


def check_embedding_connection() -> bool:
    """
    检查向量化接口是否可用，并校验实际维度与配置是否一致。

    为什么要校验维度：如果 EMBEDDING_DIM 配错，Chroma 会在写入时报维度不匹配，
    错误信息不直观。这里主动比对，直接告诉你配错在哪。

    返回: 成功且维度一致返回 True，否则 False
    """
    # 模拟模式：直接验证 MockEmbeddings 的维度是否正确。
    # 这一步仍有价值——它能确认 EMBEDDING_DIM 的值是可用的。
    if get_runtime_config().mock:
        from app.core.mock_llm import get_mock_embeddings

        vector = get_mock_embeddings().embed_query("连通性测试")
        print(f"[跳过] 离线模拟模式，模拟向量维度 {len(vector)}（与 EMBEDDING_DIM 一致）")
        return True

    try:
        embeddings = get_embeddings()
        vector = embeddings.embed_query("连通性测试")
        actual_dim = len(vector)

        if actual_dim != settings.embedding_dim:
            print(
                f"[警告] 维度不一致：模型实际输出 {actual_dim} 维，"
                f"但配置 EMBEDDING_DIM={settings.embedding_dim}。"
                f"请把 .env 里的 EMBEDDING_DIM 改为 {actual_dim}"
            )
            return False

        print(f"[成功] 向量化正常，维度 {actual_dim}")
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"[失败] 向量化调用出错：{type(exc).__name__}: {exc}")
        return False


# ---------------------------------------------------------------
# 自测入口：用法 python -m app.core.llm
# ---------------------------------------------------------------
if __name__ == "__main__":
    print("=" * 60)
    print("模型连通性自检")
    print("=" * 60)
    # 先明确告知当前跑的是真实模型还是离线替身，避免误判自检结果
    print(f"运行模式：{'离线模拟（LLM_MOCK=true）' if settings.llm_mock else '真实模型调用'}")
    print(f"当前配置：{settings.llm_config_summary}")
    print("-" * 60)

    # 分别检查对话模型和向量化模型，便于定位问题出在哪一环
    llm_ok = check_llm_connection()
    emb_ok = check_embedding_connection()

    print("-" * 60)
    print(f"对话模型：{'正常' if llm_ok else '异常'}")
    print(f"向量模型：{'正常' if emb_ok else '异常'}")

    # 顺手演示一下用量追踪器的用法，多智能体研究报告项目会用到
    tracker = TokenUsageTracker(budget=10000)
    tracker.add(prompt_tokens=1200, completion_tokens=340, label="演示")
    print(f"用量演示：{tracker.summary()}")
