"""
统一配置模块。

作用：把 .env 文件里的所有配置读进来，做类型转换和默认值填充，
     全项目通过 `from app.core.config import settings` 拿到配置对象。

为什么用 pydantic-settings 而不是直接 os.getenv：
    1. 自动类型转换。os.getenv 拿到的永远是字符串，"10" 和 10 混用迟早出 bug。
    2. 集中声明。所有可用配置项在这里一眼看全，不会出现"某个配置项不知道存不存在"。
    3. 缺省值明确。哪些配置可省略、省略后是什么行为，写在代码里而不是散在文档里。

使用方式：
    from app.core.config import settings
    print(settings.llm_model)
"""

from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


# 项目根目录（即本仓库的 backend/ 目录）。
# Path(__file__) 是当前文件路径 backend/app/core/config.py
# .resolve() 转成绝对路径，避免相对路径在不同工作目录下解析结果不同
# .parent 是 app/core/ 目录，再 .parent 是 app/，再 .parent 才是 backend/ 项目根目录
PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent.parent

# 数据目录，SQLite 数据库文件和 Chroma 索引都存在这里
DATA_DIR: Path = PROJECT_ROOT / "data"


class Settings(BaseSettings):
    """
    全局配置对象。

    字段名与 .env 中的变量名一一对应，且大小写不敏感：
        字段 llm_api_key  <->  环境变量 LLM_API_KEY 或 llm_api_key
    """

    # ---------------------------------------------------------------
    # Pydantic 配置：告诉它去哪里读配置、怎么读
    # ---------------------------------------------------------------
    model_config = SettingsConfigDict(
        # 从项目根目录下的 .env 文件读取（而不是当前工作目录，避免 cd 到别处就读不到）
        env_file=str(PROJECT_ROOT / ".env"),
        # 配置文件编码，中文注释必须用 utf-8，否则 Windows 下会乱码
        env_file_encoding="utf-8",
        # 环境变量名大小写不敏感，LLM_MODEL 和 llm_model 都能匹配
        case_sensitive=False,
        # 忽略 .env 里存在但本类没声明的变量。
        # 用 extra="ignore" 而不是 "forbid"，是为了让 .env 可以留一些备注性变量
        extra="ignore",
    )

    # ===============================================================
    # 大语言模型配置
    # ===============================================================

    llm_provider: str = Field(
        default="openai",
        description="模型服务商标识，仅用于日志区分，不影响调用逻辑",
    )

    llm_api_key: str = Field(
        default="",
        description="模型服务 API 密钥，必填；为空时在模型创建阶段会抛出明确错误",
    )

    llm_base_url: str = Field(
        default="https://api.openai.com/v1",
        description="模型接口地址，必须以 /v1 结尾；国内服务需改为对应兼容地址",
    )

    llm_model: str = Field(
        default="gpt-4o-mini",
        description="模型名称，换模型只需改这一项",
    )

    llm_temperature: float = Field(
        default=0.0,
        description="采样温度；Text-to-SQL 与结构化输出场景必须为 0 以保证可复现",
    )

    llm_max_tokens: int = Field(
        default=2000,
        description="单次回复最大 token 数，过小会截断长报告，过大增加成本",
    )

    llm_timeout: int = Field(
        default=60,
        description="单次请求超时秒数，超时由 SDK 抛异常，交由上层重试逻辑处理",
    )

    llm_max_retries: int = Field(
        default=2,
        description="SDK 层自动重试次数，主要应对网络抖动与 429 限流",
    )

    llm_mock: bool = Field(
        default=False,
        description=(
            "是否启用离线模拟模型。true 时全部模型调用走 app/core/mock_llm.py 的替身，"
            "不联网、零成本，用于在还没配 API Key 时跑通完整流程。"
            "注意：模拟模型的产出全部带 MOCK 标记，不具备任何事实参考价值。"
        ),
    )

    llm_disable_thinking: bool = Field(
        default=False,
        description=(
            "是否在请求里显式关闭模型的思考模式（thinking）。"
            "DeepSeek V4 默认开启思考模式，而思考模式下温度会被静默忽略"
            "（本项目大量依赖温度为 0 保证可复现）、思考过程本身占用 max_tokens、"
            "且不接受被强制指定的 tool_choice，会让结构化输出直接报 400"
            "（实测报错：Thinking mode does not support this tool_choice）。"
            "接 DeepSeek 时必须设为 true；OpenAI 等服务商不认识这个参数，保持 false。"
        ),
    )

    llm_structured_method: str = Field(
        default="function_calling",
        description=(
            "结构化输出（with_structured_output）的实现方式，"
            "取值 json_schema / function_calling / json_mode。"
            "json_schema 对输出的约束最强，但部分服务商不支持"
            "（实测 DeepSeek 返回 This response_format type is unavailable now）；"
            "function_calling 走工具调用，兼容性最好，因此作为默认值；"
            "json_mode 要求 Prompt 里出现 json 字样才可用。"
        ),
    )

    # ===============================================================
    # 向量化模型配置
    # ===============================================================

    embedding_provider: str = Field(
        default="openai",
        description="向量化模型提供方标识",
    )

    embedding_api_key: str = Field(
        default="",
        description="向量化模型密钥；留空则在运行时自动复用 llm_api_key",
    )

    embedding_base_url: str = Field(
        default="",
        description="向量化模型接口地址；留空则在运行时自动复用 llm_base_url",
    )

    embedding_model: str = Field(
        default="text-embedding-3-small",
        description="向量化模型名称",
    )

    embedding_dim: int = Field(
        default=1536,
        description="向量维度，必须与模型真实输出维度一致，否则 Chroma 建库报维度不匹配",
    )

    embedding_mock: bool = Field(
        default=False,
        description=(
            "向量化模型是否强制使用离线替身，与 LLM_MOCK 相互独立。"
            "为什么需要单独一个开关：DeepSeek 只提供对话模型、没有向量化接口，"
            "这种情况下对话可以走真实模型，但向量化只能留在本地替身，"
            "否则 Schema 检索环节会整条链路失败。"
            "注意：切换这个开关后 Chroma 里已建好的索引必须重建，"
            "因为真实模型与替身的向量维度不一定相同。"
        ),
    )

    # ===============================================================
    # 数据库配置
    # ===============================================================

    db_url: str = Field(
        default="sqlite:///./data/ecommerce.db",
        description="数据库连接串；SQLAlchemy 格式，切换数据库只改这一行",
    )

    sql_row_limit: int = Field(
        default=500,
        description="SQL 结果行数上限，校验层会强制追加到查询语句，防止全表拉取",
    )

    sql_timeout_seconds: int = Field(
        default=10,
        description="SQL 执行超时秒数，防止慢查询拖垮服务",
    )

    sql_max_retries: int = Field(
        default=2,
        description="SQL 生成失败后的最大尝试次数（含首次）；2 表示最多自我修正 1 次",
    )

    schema_top_k: int = Field(
        default=3,
        description="Schema 检索召回的表数量；过大干扰模型判断，过小可能漏表",
    )

    # ===============================================================
    # Web 接口层配置
    # ===============================================================

    cors_origins: str = Field(
        default="http://localhost:5173,http://127.0.0.1:5173",
        description=(
            "允许跨域访问的前端地址，多个用英文逗号分隔。"
            "前后端分离后前端由 Vite 提供（默认 5173 端口），"
            "浏览器会以 CORS 为由拦截未放行的来源，必须在这里显式声明"
        ),
    )

    # ===============================================================
    # 向量库与缓存配置
    # ===============================================================

    chroma_persist_dir: str = Field(
        default="./data/chroma",
        description="Chroma 持久化目录，索引建在此处，重启后无需重建",
    )

    redis_url: str = Field(
        default="redis://localhost:6379/0",
        description="Redis 连接串，多智能体研究报告项目的会话记忆使用",
    )

    force_reindex: bool = Field(
        default=False,
        description="是否每次启动强制重建向量索引；开启会产生真实向量化费用",
    )

    # ===============================================================
    # 搜索工具配置
    # ===============================================================

    tavily_api_key: str = Field(
        default="",
        description="Tavily 搜索密钥，多智能体研究报告项目的检索节点使用",
    )

    search_max_results: int = Field(
        default=5,
        description="单次检索返回的结果条数",
    )

    # ---------------------------------------------------------------
    # 校验器：在配置加载阶段就把明显错误拦下来
    # 这样问题会暴露在启动时，而不是跑到一半才崩
    # ---------------------------------------------------------------

    @field_validator("llm_base_url")
    @classmethod
    def _check_base_url(cls, value: str) -> str:
        """
        校验接口地址格式。

        为什么要检查：最常见的配置错误是漏了 https:// 或者多写了斜杠，
        导致 SDK 报一个含糊的 Connection error，排查半天。
        这里直接给出明确提示。

        参数 value: .env 里读到的原始字符串
        返回: 去掉末尾斜杠后的地址
        """
        # 去掉首尾空白，防止复制粘贴时带入空格
        normalized = value.strip().rstrip("/")

        # 必须是 http/https 开头，否则 SDK 无法识别
        if not normalized.startswith(("http://", "https://")):
            raise ValueError(
                f"LLM_BASE_URL 必须以 http:// 或 https:// 开头，当前值：{value!r}"
            )

        return normalized

    @field_validator("llm_temperature")
    @classmethod
    def _check_temperature(cls, value: float) -> float:
        """
        校验温度取值范围。

        参数 value: 配置的温度值
        返回: 校验通过的温度值
        """
        if not 0.0 <= value <= 2.0:
            raise ValueError(f"LLM_TEMPERATURE 必须在 0 到 2 之间，当前值：{value}")
        return value

    # ---------------------------------------------------------------
    # 派生属性：不来自 .env，而是由已有配置计算得出
    # 用 property 而不是字段，是为了让"派生逻辑"只有一份实现
    # ---------------------------------------------------------------

    @property
    def resolved_embedding_api_key(self) -> str:
        """
        向量化模型的最终密钥。

        逻辑：单独的 EMBEDDING_API_KEY 优先；没配就复用 LLM_API_KEY。
        大多数服务商两种模型共用一个密钥，这样可以少配一项。
        """
        return self.embedding_api_key or self.llm_api_key

    @property
    def resolved_embedding_base_url(self) -> str:
        """
        向量化模型的最终接口地址。

        逻辑：单独的 EMBEDDING_BASE_URL 优先；没配就复用 LLM_BASE_URL。
        """
        return self.embedding_base_url or self.llm_base_url

    @property
    def chroma_path(self) -> Path:
        """
        Chroma 持久化目录的绝对路径。

        为什么要转绝对路径：Chroma 内部对相对路径的处理依赖当前工作目录，
        如果从不同目录启动程序，可能读到两个不同的库，出现"明明建过索引却查不到"的怪问题。
        """
        # 配置里写的是相对路径（相对项目根目录），这里补全
        raw = Path(self.chroma_persist_dir)
        return raw if raw.is_absolute() else PROJECT_ROOT / raw

    @property
    def llm_config_summary(self) -> str:
        """
        生成一行模型配置摘要，用于启动时打印。

        安全性：只显示密钥的前 6 位，避免完整密钥出现在日志或截图里。
        """
        # 密钥太短时不显示内容，只标记已配置
        masked = f"{self.llm_api_key[:6]}***" if len(self.llm_api_key) > 6 else "(未配置)"
        return (
            f"provider={self.llm_provider} model={self.llm_model} "
            f"base_url={self.llm_base_url} key={masked} temperature={self.llm_temperature}"
        )

    @property
    def cors_origin_list(self) -> list[str]:
        """
        把逗号分隔的跨域白名单拆成列表。

        为什么单独做一层解析：
            环境变量只能存字符串，但 FastAPI 的 CORSMiddleware 要的是列表。
            解析逻辑集中在这里，.env 里就能写成一行"地址,地址"的可读形式，
            也避免了每个调用点各自 split 一遍导致行为不一致。
        """
        return [item.strip() for item in self.cors_origins.split(",") if item.strip()]


def _load_settings() -> Settings:
    """
    创建并返回全局唯一的配置实例。

    返回: Settings 实例

    说明：这里刻意不在导入时就做副作用操作（比如创建目录、打印日志），
         保持"导入模块"这个动作是干净的，方便测试时替换配置。
    """
    return Settings()


# 全局配置单例。
# 其他模块统一 `from app.core.config import settings` 使用，
# 保证全项目读到的配置完全一致，不会出现某处改了环境变量导致行为不一致。
settings: Settings = _load_settings()


def ensure_data_dirs() -> None:
    """
    确保运行所需的数据目录存在。

    为什么需要：SQLite 在数据库文件所在目录不存在时不会自动创建目录，
    只会报一个含义不清的 "unable to open database file"。
    在建库和建索引之前调用一次，把目录准备好。

    返回: 无
    """
    # 数据根目录
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    # Chroma 持久化目录
    settings.chroma_path.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------
# 自测入口：直接运行本文件可以检查配置是否读取正确
# 用法：python -m app.core.config
# ---------------------------------------------------------------
if __name__ == "__main__":
    print("=" * 60)
    print("配置自检")
    print("=" * 60)

    # 项目路径检查
    print(f"项目根目录 : {PROJECT_ROOT}")
    print(f"数据目录   : {DATA_DIR}")
    print(f"Chroma 目录: {settings.chroma_path}")

    # 模型配置检查（密钥已脱敏）
    print(f"模型配置   : {settings.llm_config_summary}")
    print(f"向量模型   : {settings.embedding_model} (维度 {settings.embedding_dim})")
    print(f"向量密钥来源: {'独立配置' if settings.embedding_api_key else '复用 LLM_API_KEY'}")

    # 数据库与业务参数检查
    print(f"数据库     : {settings.db_url}")
    print(f"SQL 限制   : 行数上限 {settings.sql_row_limit}，超时 {settings.sql_timeout_seconds}s，"
          f"重试 {settings.sql_max_retries} 次")
    print(f"Schema 召回: Top-{settings.schema_top_k}")

    # 必填项检查：提前告诉用户还差什么，而不是等到调用模型时才报错
    print("-" * 60)
    print(f"运行模式   : {'离线模拟（LLM_MOCK=true）' if settings.llm_mock else '真实模型调用'}")

    missing: list[str] = []
    # 离线模拟模式下不需要任何密钥，因此跳过必填项检查
    if not settings.llm_mock:
        if not settings.llm_api_key or settings.llm_api_key.startswith("sk-replace"):
            missing.append("LLM_API_KEY")
        if not settings.tavily_api_key or settings.tavily_api_key.startswith("tvly-replace"):
            missing.append("TAVILY_API_KEY（不填则检索走离线样例数据）")

    if missing:
        print("待填写配置：")
        for item in missing:
            print(f"  - {item}")
        print("请编辑项目根目录下的 .env 文件后重新运行。")
    else:
        print("配置检查通过，可以开始。")

    # 尝试创建数据目录，验证写权限
    ensure_data_dirs()
    print(f"数据目录已就绪：{DATA_DIR}")
