"""LLM 客户端工厂：把 Settings 翻译成 LangChain ChatModel。

业务层只依赖 langchain-core 的 ``BaseChatModel``；各厂商的参数名、
鉴权方式差异全部在这一层吸收，换 provider / 换模型都不用动业务层。

DeepSeek 走 OpenAI 兼容协议（langchain-openai + base_url），
Anthropic 走 langchain-anthropic。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel

from core.config import Settings, get_settings


class LlmConfigError(RuntimeError):
    """LLM 配置缺失或不受支持——属于部署问题，不是用户输入问题。

    路由层据此返回 503（服务端未就绪），而不是 400。
    """


def build_chat_model(
    settings: Settings | None = None,
    *,
    temperature: float | None = None,
    max_tokens: int | None = None,
    streaming: bool = False,
) -> BaseChatModel:
    """按 ``settings.llm_provider`` 构造 ChatModel。

    Args:
        settings: 不传则用全局配置。
        temperature: 覆盖默认温度（例如澄清阶段要更保守）。
        max_tokens: 覆盖默认输出上限。
        streaming: 为后续 SSE 流式输出预留。

    Raises:
        LlmConfigError: 缺少当前 provider 的 API Key，或 provider 不受支持。
    """
    settings = settings or get_settings()
    provider = settings.llm_provider

    api_key = settings.active_llm_api_key
    if api_key is None:
        raise LlmConfigError(
            f"未配置 {provider} 的 API Key：请在 backend/.env 中设置 "
            f"{provider.upper()}_API_KEY（当前 DEFAULT_LLM_PROVIDER={provider}）"
        )
    secret = api_key.get_secret_value()

    resolved_temperature = settings.llm_temperature if temperature is None else temperature
    resolved_max_tokens = max_tokens or settings.llm_max_tokens

    if provider == "deepseek":
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(
            model=settings.active_llm_model,
            api_key=secret,
            base_url=settings.deepseek_base_url,
            temperature=resolved_temperature,
            timeout=settings.llm_timeout,
            streaming=streaming,
            # ⚠️ 输出上限**只能用 `extra_body` 传**，`max_tokens=` 和
            # `model_kwargs={"max_tokens": N}` 都不行 —— 实测**两者都会被 langchain-openai
            # 改名成 OpenAI 的新字段 `max_completion_tokens`**（`_get_request_payload()`
            # 里看到的始终是 `max_completion_tokens`），而 **DeepSeek 不认这个字段、
            # 静默忽略**：发 `max_completion_tokens=16` 会吐出 1500 个分片且
            # `finish_reason=stop`。`extra_body` 由 openai SDK 直接并进请求体，
            # 实测 `extra_body={"max_tokens": 16}` → 16 个分片、`finish_reason=length`。
            #
            # 代价：`LLM_MAX_TOKENS` 从此**真的**生效，超限会被厂商按 `length` 截断
            # ——所以调用方必须看 `finish_reason`（`finish_reason_of` / `is_truncated`），
            # 否则截断会静默发生（`HANDOFF.md` §4 坑 #18）。
            extra_body={"max_tokens": resolved_max_tokens},
        )

    if provider == "openai":
        from langchain_openai import ChatOpenAI

        extra: dict[str, Any] = {}
        if settings.openai_base_url:
            # 留空即用官方地址；填了可接中转站或本地 Ollama
            extra["base_url"] = settings.openai_base_url

        return ChatOpenAI(
            model=settings.active_llm_model,
            api_key=secret,
            temperature=resolved_temperature,
            max_tokens=resolved_max_tokens,
            timeout=settings.llm_timeout,
            streaming=streaming,
            **extra,
        )

    if provider == "anthropic":
        from langchain_anthropic import ChatAnthropic

        return ChatAnthropic(
            model=settings.active_llm_model,
            api_key=secret,
            temperature=resolved_temperature,
            max_tokens=resolved_max_tokens,
            timeout=settings.llm_timeout,
            streaming=streaming,
        )

    # Literal 已限定取值，正常不可达；显式兜底以免新增 provider 时静默返回 None
    raise LlmConfigError(f"不支持的 LLM_PROVIDER：{provider!r}")


# ---------------------------------------------------------------- 结束原因
#
# 为什么需要这一组函数：**"撞上输出上限"与"正常写完"在文本上无法区分** ——
# 截断点不在任何标记上，末尾就是半行表格 / 半句话。实测接口文档整份生成会撞上
# `LLM_MAX_TOKENS`，被截断后前端照样显示成"待审核"，看起来完全成功。
# 唯一可靠的判据是厂商给的 `finish_reason`（Anthropic 叫 `stop_reason`）。

# 各厂商表示"被输出上限截断"的取值。key 一律小写比较。
# OpenAI / DeepSeek：`length`；Anthropic：`max_tokens`。
# 正常结束是 `stop`（OpenAI 系）/ `end_turn`、`stop_sequence`（Anthropic）。
_TRUNCATED_REASONS: frozenset[str] = frozenset({"length", "max_tokens"})


def finish_reason_of(chunk: Any) -> str | None:
    """从分片（或完整消息）里取厂商的结束原因。

    取不到就返回 `None` —— **不猜**。宁可漏报一次截断，也不要凭"末尾看起来不完整"
    去猜（那会误报，而误报会让用户不再相信这个提示）。
    """
    metadata = getattr(chunk, "response_metadata", None) or {}
    if not isinstance(metadata, dict):
        return None
    reason = metadata.get("finish_reason") or metadata.get("stop_reason")
    return reason if isinstance(reason, str) and reason else None


def is_truncated(reason: str | None) -> bool:
    """该结束原因是否表示**输出被上限截断**（而不是正常写完）。"""
    return reason is not None and reason.lower() in _TRUNCATED_REASONS


@dataclass
class StreamOutcome:
    """一次流式生成的**收尾信息** —— 正文之外、调用方必须知道的那一点。

    放在 `llm.py` 而不是 `document_service.py`：**对话侧和生成侧都要用它**
    （两边都会撞输出上限），谁 import 谁就成了反向依赖。定义在这里，两边都只依赖它。

    为什么不做成"流里的一个特殊分片"：服务层对外承诺的是**裸文本片段的异步迭代器**，
    掺进非字符串会污染所有调用方（含前端与三个校验脚本）。所以改成
    "调用方传一个空盒子进来、服务层往里填"，由 `api` 层在 `done` 帧里带出去。
    **一个盒子只给一次调用用**，不要跨请求复用。

    要解决的问题是**静默截断**：撞上输出上限时，末尾就是半行表格 / 半句话 / 半截 JSON，
    与正常写完在文本上**无法区分**，前端照样显示成"待审核"，看起来完全成功。
    实测接口文档整份生成就会这样（见 `HANDOFF.md` §4 坑 #18）。
    """

    finish_reason: str | None = None
    """厂商原话（`stop` / `length` / `max_tokens` …），原样带出去便于排查。取不到是 `None`。"""

    @property
    def truncated(self) -> bool:
        """输出是否被上限截断。**`finish_reason` 取不到时是 `False`** —— 不猜。"""
        return is_truncated(self.finish_reason)
