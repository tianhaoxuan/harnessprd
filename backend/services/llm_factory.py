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

import time
from collections.abc import AsyncIterator
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel

from core.config import Settings, get_settings
from services.llm import build_chat_model
from services.llm_metrics import LlmStep, RunMetricsCollector, record_step
from services.model_registry import resolve_max_output
from services.token_estimator import BudgetExceededError, check_budget, estimate_tokens

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

    # 输出上限只来自注册表：写死 4096 会截断 PRD；从 .env 取则换模型不跟着变。
    # API 的 max_tokens 与 budget 的物理顶用同一个数，日志与行为不会自相矛盾。
    resolved_max_tokens = (
        max_tokens if max_tokens is not None else resolve_max_output(resolved.active_llm_model)
    )
    return build_chat_model(
        resolved,
        temperature=temperature,
        max_tokens=resolved_max_tokens,
        streaming=streaming,
    )


class TrackedChatModel:
    """业务透明的观测包装：业务照旧 `ainvoke` / `astream`，只是多记一笔账。

    为什么用包装而不是让业务自己计时：业务里散着十几个调用点，每个都手写 try/finally
    计时 + 取 usage + 打日志，既啰嗦又必然漏 —— 漏掉的那次调用就是排查时的黑洞。

    ⚠️ **异常必须原样抛回**。观测层吞掉异常会直接改变业务行为（该失败的变成成功），
    那是比少一份账严重得多的问题 —— 所以这里 `except` 里记完账就 `raise`。
    """

    def __init__(self, llm: BaseChatModel, step: LlmStep) -> None:
        self._llm = llm
        self._step = step

    @property
    def model_name(self) -> str:
        name = getattr(self._llm, "model_name", None) or getattr(self._llm, "model", None)
        return str(name) if name else "unknown"

    def _guard_budget(self, messages: Any) -> None:
        """调模型前的预算检查；拒绝时抛 `BudgetExceededError` 且**不记 llm_step**
        （那一步没调模型，记了会让「这趟调了几次」虚高）。"""
        estimate = estimate_tokens(messages, step=self._step, model=self.model_name)
        check = check_budget(estimate, step=self._step, model=self.model_name)
        run = RunMetricsCollector.current()
        if run is not None:
            run.set_budget(check)
        if not check.allowed:
            raise BudgetExceededError(check)

    async def ainvoke(self, messages: Any, **kwargs: Any) -> Any:
        self._guard_budget(messages)
        started = time.perf_counter()
        try:
            response = await self._llm.ainvoke(messages, **kwargs)
        except Exception as exc:  # noqa: BLE001 - 记完账原样抛回
            record_step(
                step=self._step,
                model=self.model_name,
                usage=None,
                duration_ms=int((time.perf_counter() - started) * 1000),
                status="error",
                error=f"{type(exc).__name__}: {exc}"[:200],
            )
            raise
        record_step(
            step=self._step,
            model=self.model_name,
            usage=response,
            duration_ms=int((time.perf_counter() - started) * 1000),
        )
        return response

    async def astream(self, messages: Any, **kwargs: Any) -> AsyncIterator[Any]:
        """流式：**等流结束再记账**（usage 通常只在最后一个分片上）。"""
        self._guard_budget(messages)
        started = time.perf_counter()
        last: Any = None
        try:
            async for chunk in self._llm.astream(messages, **kwargs):
                if chunk is not None:
                    last = chunk
                yield chunk
        except Exception as exc:  # noqa: BLE001 - 记完账原样抛回
            record_step(
                step=self._step,
                model=self.model_name,
                usage=last,
                duration_ms=int((time.perf_counter() - started) * 1000),
                status="error",
                error=f"{type(exc).__name__}: {exc}"[:200],
            )
            raise
        record_step(
            step=self._step,
            model=self.model_name,
            usage=last,
            duration_ms=int((time.perf_counter() - started) * 1000),
        )

    def __getattr__(self, name: str) -> Any:
        """其余属性/方法一律转发 —— 业务不需要知道这层包装存在。"""
        return getattr(self._llm, name)


def track(llm: BaseChatModel, step: LlmStep) -> TrackedChatModel:
    """把已有的模型实例包成带观测的（测试注入的假模型同样适用）。"""
    if isinstance(llm, TrackedChatModel) and llm._step == step:
        return llm
    return TrackedChatModel(llm, step)


def get_tracked_llm(step: LlmStep, **kwargs: Any) -> TrackedChatModel:
    """`get_llm()` 的观测版：参数透传，额外带上步骤标签。"""
    return TrackedChatModel(get_llm(**kwargs), step)
