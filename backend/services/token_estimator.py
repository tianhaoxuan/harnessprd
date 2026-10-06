r"""发送前的 Token 估算与双层 budget 检查（01 观测链路之上的"拦一道"）。

## 两层约束，取更严的那个

    有效 input 上限 = min(业务策略 cap, 模型物理顶)

- **物理顶**（`model_registry`）：`context_window - max_output_tokens - 余量` ——
  先给输出留位置，剩下的才轮得到 input。
- **业务顶**（`.env` 的 cap）：通常远小于窗口。例如澄清对话 `CHAT_HISTORY_BUDGET=60000`
  而模型窗口 1M —— 目的是**在接近业务上限时提前告警**，而不是等厂商 API 报错。

`budget_source` 说的就是"这次生效的是哪层"，排查时一眼能看出是该调 cap 还是该换模型。

## 三个常见误解（写在代码旁边，免得调参时踩）

1. 模型标 1M **不是**"整条 PRD 流程共享 1M"：澄清第 6 轮大约 1 万 token，对 1M 物理顶
   几乎永远不 warn。真正会先撞上的是**业务顶**。
2. **不能"按轮预分配 token"**：澄清轮数不确定，只能在每次发送前**现算**当前 messages 总长。
3. `ratio` 的分母是**input 有效上限**，不再从业务顶里扣 output —— output 只在算物理顶时
   从窗口里扣。两处都扣就会把上限算小一半，warn 来得莫名其妙。

## 估不准时宁可算大

优先 tiktoken（按注册表的 `encoding`）；没有 tiktoken 时退回 `len(text) * 0.6`，
并如实标明 `estimator="char_heuristic"` —— 观测数据里"这是估的"必须能看出来。
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from typing import Any, Sequence

from core.config import get_settings
from services.llm_metrics import LlmStep
from services.model_registry import hardware_input_cap, resolve, resolve_max_output

logger = logging.getLogger("harnessprd.llm")

WARN_RATIO = 0.6
EXCEED_RATIO = 0.95
"""阈值。**刻意不做成 .env 项**：现在只有两档语义，暴露四个旋钮会让调参变成玄学。"""

_CHAR_TO_TOKEN = 0.6
"""字符兜底的比例，偏保守（宁可算大）。"""

_POLICY_FIELD: dict[str, str] = {
    LlmStep.CHAT_START.value: "chat_history_budget",
    LlmStep.CHAT_CONTINUE.value: "chat_history_budget",
    LlmStep.PRD_WRITER.value: "single_call_input_budget",
    LlmStep.PRD_REVIEW.value: "single_call_input_budget",
    LlmStep.PRD_REWRITE.value: "single_call_input_budget",
    LlmStep.API_DOCS_GENERATE.value: "single_call_input_budget",
    LlmStep.API_DOCS_REVIEW.value: "single_call_input_budget",
    LlmStep.PROMPTS_GENERATE.value: "single_call_input_budget",
    LlmStep.PROMPTS_REVIEW.value: "single_call_input_budget",
    LlmStep.DOCUMENT_OPTIMIZE.value: "single_call_input_budget",
    LlmStep.SUMMARY_SYNC.value: "context_budget_tokens",
    LlmStep.JSON_REPAIR.value: "context_budget_tokens",
    LlmStep.JSON_REGENERATE.value: "context_budget_tokens",
}
"""step → 用哪条业务 cap。**固定规则**，写在模块级而不是散在各调用点。"""


@dataclass(frozen=True)
class TokenEstimate:
    """一次估算的结果。"""

    estimated_input_tokens: int
    budget_tokens: int
    max_output_tokens: int
    ratio: float
    estimator: str


@dataclass(frozen=True)
class BudgetCheckResult:
    """一次检查的结论。`to_context_usage()` 是给前端的那四个字段。"""

    allowed: bool
    mode: str
    level: str
    estimated_input_tokens: int
    budget_tokens: int
    ratio: float
    message: str
    step: str
    model: str
    policy_cap: int
    hardware_cap: int
    budget_source: str

    def to_context_usage(self) -> dict[str, Any]:
        """前端契约：**只有这四个字段**（语义别改）。

        内部字段（policy/hardware/model）只进日志：给用户看"物理顶 995904"没有意义，
        反而会被当成 bug。
        """
        return {
            "estimated_input_tokens": self.estimated_input_tokens,
            "budget_tokens": self.budget_tokens,
            "ratio": round(self.ratio, 4),
            "level": self.level,
        }

    def to_log(self) -> dict[str, Any]:
        return asdict(self)


class BudgetExceededError(RuntimeError):
    """超预算且当前是 `reject` 模式 —— 调用方据此返回 422 / 发 error 事件。"""

    def __init__(self, check: BudgetCheckResult) -> None:
        super().__init__(check.message)
        self.check = check


def _policy_cap(step: Any) -> int:
    settings = get_settings()
    field = _POLICY_FIELD.get(getattr(step, "value", str(step)))
    if field is None:
        # 新加了枚举值却忘了配 cap：**不要静默用默认值**，那样会悄悄按另一档限制
        logger.warning("step %s 没有配业务 cap，按 context_budget_tokens 处理", step)
        field = "context_budget_tokens"
    return max(int(getattr(settings, field, 0) or 0), 1)


def resolve_effective_budget(step: Any, model: str | None) -> tuple[int, str, int, int]:
    """返回 `(有效 input 上限, budget_source, policy_cap, hardware_cap)`。"""
    policy = _policy_cap(step)
    hardware = hardware_input_cap(model)
    if policy <= hardware:
        return policy, "policy", policy, hardware
    return hardware, "hardware", policy, hardware


def _text_of(message: Any) -> str:
    """把一条消息的正文抠成字符串（兼容 LangChain 的 content blocks 形态）。"""
    content = getattr(message, "content", message)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                text = block.get("text") or block.get("content")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(parts)
    return str(content or "")


def _encoder(encoding: str) -> Any:
    try:
        import tiktoken
    except ModuleNotFoundError:  # pragma: no cover - 环境问题
        return None
    try:
        return tiktoken.get_encoding(encoding)
    except Exception:  # noqa: BLE001 - 编码名不认识时退回默认编码
        logger.warning("tiktoken 不认识编码 %s，退回 cl100k_base", encoding)
        try:
            return tiktoken.get_encoding("cl100k_base")
        except Exception:  # noqa: BLE001
            return None


def estimate_tokens(
    messages: Sequence[Any] | str,
    *,
    step: Any,
    model: str | None,
) -> TokenEstimate:
    """估算待发 prompt 的 input token 数，并附上这次的有效上限。"""
    if isinstance(messages, str):
        texts = [messages]
    else:
        texts = [_text_of(item) for item in messages]
    joined = "\n".join(texts)

    settings = get_settings()
    caps = resolve(model)
    prefer = str(getattr(settings, "token_estimator", "tiktoken") or "tiktoken")
    encoder = _encoder(caps.encoding) if prefer == "tiktoken" else None

    if encoder is not None:
        estimated = sum(len(encoder.encode(text)) for text in texts if text)
        estimator = "tiktoken"
    else:
        # 兜底：字符数 × 0.6（中英混排下偏保守），并如实标注是估的
        estimated = int(len(joined) * _CHAR_TO_TOKEN)
        estimator = "char_heuristic"

    # 每条消息的角色/分隔开销（约 4 token）—— 不估这一项时，长对话会被系统性低估
    estimated += 4 * max(len(texts) - 1, 0)

    budget, _source, _policy, _hardware = resolve_effective_budget(step, model)
    ratio = estimated / budget if budget > 0 else 1.0
    return TokenEstimate(
        estimated_input_tokens=estimated,
        budget_tokens=budget,
        max_output_tokens=resolve_max_output(model),
        ratio=ratio,
        estimator=estimator,
    )


def check_budget(
    estimate: TokenEstimate, *, step: Any, model: str | None = None
) -> BudgetCheckResult:
    """与有效上限比较，得出 ok / warn / exceed 与是否放行。"""
    settings = get_settings()
    mode = str(getattr(settings, "token_budget_mode", "warn") or "warn")
    ratio = estimate.ratio
    if ratio < WARN_RATIO:
        level = "ok"
    elif ratio <= EXCEED_RATIO:
        level = "warn"
    else:
        level = "exceed"

    budget, source, policy, hardware = resolve_effective_budget(step, model)
    message = (
        f"[{getattr(step, 'value', step)}] 估算 input {estimate.estimated_input_tokens} / "
        f"上限 {estimate.budget_tokens}（{source}），占用 {ratio:.0%}"
    )
    allowed = not (mode == "reject" and level == "exceed")
    result = BudgetCheckResult(
        allowed=allowed,
        mode=mode,
        level=level,
        estimated_input_tokens=estimate.estimated_input_tokens,
        budget_tokens=estimate.budget_tokens,
        ratio=ratio,
        message=message,
        step=getattr(step, "value", str(step)),
        model=model or "",
        policy_cap=policy,
        hardware_cap=hardware,
        budget_source=source,
    )
    if level != "ok":
        logger.warning(
            "event=token_budget %s",
            json.dumps(
                {
                    "event": "token_budget",
                    "level": level,
                    "mode": mode,
                    "allowed": allowed,
                    **result.to_context_usage(),
                    "step": result.step,
                    "estimator": estimate.estimator,
                    "budget_source": source,
                    "policy_cap": policy,
                    "hardware_cap": hardware,
                },
                ensure_ascii=False,
            ),
        )
    return result


def estimate_model_step(step: Any) -> str | None:
    """给 `check_budget` 用的模型名占位。

    `check_budget` 是**纯比较**，不需要知道模型；物理顶相关字段在 `estimate_tokens`
    那一步已经算好并存在 `TokenEstimate` 里。这个函数只为让签名对称、便于以后扩展
    （例如把模型名带进日志）。返回 `None` 表示"用注册表的兜底值"。
    """
    return None
