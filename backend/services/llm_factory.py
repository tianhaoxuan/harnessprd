"""LLM 工厂的**命名入口**：`get_llm()`。

⚠️ **实现只有一份**：`services/llm.py` 的 `build_chat_model()`。
本模块只是它的稳定门面 —— 业务代码写 `get_llm()` 比记住
`build_chat_model(settings, temperature=..., max_tokens=..., streaming=...)` 省事，
而且将来换底层实现时只改这一处。

**支持三家，不是两家**：

| `DEFAULT_LLM_PROVIDER` | 实际类 | 备注 |
| --- | --- | --- |
| `deepseek` | `ChatOpenAI` | 走 OpenAI 兼容协议（`base_url`）。**当前唯一配了 Key 的** |
| `openai` | `ChatOpenAI` | |
| `anthropic` | `ChatAnthropic` | 构造已验证，**真实调用未验证**（无 Key） |

`deepseek` 不能省：`validation_out/` 那 7 份留档全是经它跑的，去掉就断了唯一可用的链路。

关于命名：LangChain 里 `BaseChatModel` 才是"聊天模型"，旧 API 的 `LLM` 是另一个东西。
这里沿用 `get_llm()` 这个名字只是为了好记，返回的是 `BaseChatModel`。
"""

from __future__ import annotations

from langchain_core.language_models.chat_models import BaseChatModel

from core.config import Settings, get_settings
from services.llm import build_chat_model

SUPPORTED_PROVIDERS: tuple[str, ...] = ("deepseek", "openai", "anthropic")
"""与 `core.config.LlmProvider` 保持一致（那边是 Literal，这里是可迭代形式）。"""


def get_llm(
    provider: str | None = None,
    *,
    temperature: float | None = None,
    max_tokens: int | None = None,
    streaming: bool = False,
    settings: Settings | None = None,
) -> BaseChatModel:
    """按配置返回一个 ChatModel 实例。

    Args:
        provider: 覆盖 `DEFAULT_LLM_PROVIDER`。不传就用配置里的值。
            主要用于测试与"同一进程内比较不同模型"的场景。
        temperature / max_tokens: 覆盖配置默认值。
        streaming: 为 SSE 流式输出预留（F3.14）。
        settings: 不传用全局配置。

    Raises:
        LlmConfigError: 当前 provider 没配 Key，或 provider 不受支持。
            路由层据此返回 **503**（服务端未就绪），而不是 500。
    """
    resolved = settings or get_settings()
    if provider is not None and provider != resolved.llm_provider:
        # model_copy 不重跑校验器；非法取值会落到 build_chat_model 的兜底分支报错，
        # 这里不重复一份 provider 白名单（两份白名单必然漂移）。
        resolved = resolved.model_copy(update={"llm_provider": provider})

    return build_chat_model(
        resolved,
        temperature=temperature,
        max_tokens=max_tokens,
        streaming=streaming,
    )
