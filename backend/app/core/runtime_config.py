"""
运行时配置覆盖层。

作用：在 .env 的静态配置之上，加一层"可以被界面修改、立即生效"的覆盖层。
     取值的规则只有一条：

         当前生效值 = 覆盖层里有 ? 用户填的值 : .env 里的值

为什么不能直接改 settings：
    settings 是 pydantic 在启动时构建好的静态对象，全项目到处都在读它。
    直接改它的字段，一来别的模块可能早就把值读走存到自己那里了、改了不起作用，
    二来"谁手里那份才是真的"变得不可预测。
    所以这里另起一层：谁需要用配置，谁就来这里取"当前生效值"。

为什么用 RLock：
    FastAPI 的同步路由跑在线程池里，SSE 的研究流程跑在独立工作线程里，
    读写覆盖层可能来自不同线程。RLock 可重入，同一线程内嵌套调用不会自锁。

为什么覆盖层只存"被改过的字段"：
    恢复默认就等价于把字典清空，不必记住 .env 的原值，
    也就不存在"恢复成错的值"这种情况。

注意：覆盖层只存在于进程内存里。重启后端就回到 .env 的值。

使用方式：
    from app.core.runtime_config import get_runtime_config

    runtime = get_runtime_config()
    print(runtime.model, runtime.masked_api_key)
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any

from app.core.config import settings


# 允许被界面覆盖的字段。
# 刻意不含 llm_temperature：本项目是 Text-to-SQL，温度为 0 是可复现性的前提，
# 放开之后用户调高会引入"同样的问题两次结果不一样"，属于给自己挖坑。
OVERRIDABLE_FIELDS: tuple[str, ...] = (
    "model",
    "base_url",
    "api_key",
    "structured_method",
    "disable_thinking",
)

# 结构化输出的合法取值，与 config.py 里 llm_structured_method 的说明保持一致
STRUCTURED_METHODS: tuple[str, ...] = ("json_schema", "function_calling", "json_mode")

# 密钥脱敏时保留的前缀长度
_MASK_PREFIX = 6


def _mask(secret: str) -> str:
    """
    把密钥脱敏成可安全展示的形式。

    参数:
        secret 原始密钥

    返回:
        形如 sk-abc*** 的字符串；未配置时返回"(未配置)"
    """
    value = (secret or "").strip()
    if not value:
        return "(未配置)"
    # 太短的密钥不显示内容，否则"脱敏"本身就把完整密钥暴露了
    return f"{value[:_MASK_PREFIX]}***" if len(value) > _MASK_PREFIX else "(已配置)"


@dataclass(frozen=True)
class LLMRuntimeConfig:
    """
    某一时刻"当前生效"的模型配置快照。

    为什么做成 frozen 的不可变对象：
        调用方拿到之后可以放心用，不必担心用着用着被别的线程改掉。
        要改配置就走 update_runtime_config，它会返回一份新的快照。
    """

    model: str
    base_url: str
    api_key: str
    structured_method: str
    disable_thinking: bool
    mock: bool
    # 哪些字段是被界面覆盖过的，用于前端提示"当前不是 .env 的值"
    overridden: tuple[str, ...] = ()

    @property
    def has_api_key(self) -> bool:
        """是否配置了非空的密钥。"""
        return bool(self.api_key.strip())

    @property
    def masked_api_key(self) -> str:
        """脱敏后的密钥，可以安全返回给前端。"""
        return _mask(self.api_key)

    @property
    def summary(self) -> str:
        """一行配置摘要，用于启动日志与 /meta 展示。密钥已脱敏。"""
        return (
            f"provider={settings.llm_provider} model={self.model} "
            f"base_url={self.base_url} key={self.masked_api_key} "
            f"structured={self.structured_method} mock={self.mock}"
        )


class _RuntimeConfigStore:
    """覆盖层的存储与合并逻辑。进程内唯一实例，见文件末尾的 _store。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        # 只存被界面改过的字段，key 一定是 OVERRIDABLE_FIELDS 里的成员
        self._overrides: dict[str, Any] = {}

    def snapshot(self) -> LLMRuntimeConfig:
        """读取当前生效配置。"""
        with self._lock:
            return self._compose()

    def update(self, changes: dict[str, Any]) -> LLMRuntimeConfig:
        """
        写入一批配置并返回更新后的快照。

        参数:
            changes 字段名 -> 新值。字段名必须在 OVERRIDABLE_FIELDS 里

        返回:
            更新后的配置快照

        抛出:
            ValueError 存在不支持的字段，或某个值不合法
        """
        unknown = set(changes) - set(OVERRIDABLE_FIELDS)
        if unknown:
            raise ValueError(f"不支持的配置项：{'、'.join(sorted(unknown))}")

        # 先全部校验、再统一写入。
        # 这样"模型名合法但接口地址非法"时不会只改掉一半，留下一个自相矛盾的配置状态
        cleaned = {key: self._normalize(key, value) for key, value in changes.items()}

        with self._lock:
            self._overrides.update(cleaned)
            return self._compose()

    def reset(self) -> LLMRuntimeConfig:
        """清空覆盖层，回到 .env 的配置。"""
        with self._lock:
            self._overrides.clear()
            return self._compose()

    def _compose(self) -> LLMRuntimeConfig:
        """把 .env 的基准值与覆盖层合并成一份快照。"""
        values: dict[str, Any] = {
            "model": settings.llm_model,
            "base_url": settings.llm_base_url,
            "api_key": settings.llm_api_key,
            "structured_method": settings.llm_structured_method,
            "disable_thinking": bool(settings.llm_disable_thinking),
            # mock 不从覆盖层取：这个开关只由 .env 决定，界面只做只读展示
            "mock": bool(settings.llm_mock),
        }
        values.update(self._overrides)

        return LLMRuntimeConfig(
            **values,
            overridden=tuple(
                field for field in OVERRIDABLE_FIELDS if field in self._overrides
            ),
        )

    @staticmethod
    def _normalize(key: str, value: Any) -> Any:
        """
        规范化并校验单个字段。

        参数:
            key   字段名
            value 原始值（可能来自 JSON，类型不可信）

        返回:
            规范化后的值

        抛出:
            ValueError 值不合法
        """
        # 布尔开关直接转 bool，不接受"看起来像假值"的字符串这种模糊写法
        if key == "disable_thinking":
            return bool(value)

        text = str(value).strip()

        if key == "model" and not text:
            raise ValueError("模型名称不能为空")

        if key == "base_url":
            normalized = text.rstrip("/")
            # 校验规则与 config.py 的 _check_base_url 保持一致，
            # 免得"界面能存进去、重启后 .env 里的同类值却过不了校验"
            if not normalized.startswith(("http://", "https://")):
                raise ValueError(
                    f"接口地址必须以 http:// 或 https:// 开头，当前值：{text!r}"
                )
            return normalized

        if key == "structured_method":
            if text not in STRUCTURED_METHODS:
                raise ValueError(
                    f"结构化输出方式只能是 {'、'.join(STRUCTURED_METHODS)} 之一，"
                    f"当前值：{text!r}"
                )
            return text

        return text


# 全局唯一的覆盖层实例
_store = _RuntimeConfigStore()


def get_runtime_config() -> LLMRuntimeConfig:
    """读取"当前生效"的模型配置。业务代码统一通过它取配置，不再直接读 settings。"""
    return _store.snapshot()


def update_runtime_config(**changes: Any) -> LLMRuntimeConfig:
    """
    更新运行时配置。

    参数:
        **changes 字段名=新值，字段必须在 OVERRIDABLE_FIELDS 里

    返回:
        更新后的配置快照
    """
    return _store.update(changes)


def reset_runtime_config() -> LLMRuntimeConfig:
    """清空覆盖层，恢复为 .env 里的配置。"""
    return _store.reset()


# ---------------------------------------------------------------
# 自测入口：用法 python -m app.core.runtime_config
# ---------------------------------------------------------------
if __name__ == "__main__":
    print("=" * 60)
    print("运行时配置覆盖层自检")
    print("=" * 60)

    print(f"[初始]   {get_runtime_config().summary}")

    # 覆盖模型名与结构化输出方式，验证"只覆盖指定字段、其余保持 .env 值"
    after_update = update_runtime_config(
        model="deepseek-chat",
        structured_method="function_calling",
    )
    print(f"[覆盖后] {after_update.summary}")
    print(f"[覆盖项] {after_update.overridden}")

    print(f"[重置后] {reset_runtime_config().summary}")

    # 非法值应当被拦下，且不影响已有配置
    for bad_case in (
        {"base_url": "api.deepseek.com/v1"},
        {"structured_method": "json_schema_v2"},
        {"model": ""},
        {"temperature": 0.7},
    ):
        try:
            update_runtime_config(**bad_case)
            print(f"[异常] 非法配置竟然通过了：{bad_case}")
        except ValueError as exc:
            print(f"[拦截] {bad_case} -> {exc}")

    print(f"[最终]   {get_runtime_config().summary}")
