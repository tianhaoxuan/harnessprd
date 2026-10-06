"""交付链路的**业务前置守卫**（05 篇）。

## 为什么单独一个模块

`api/jobs.py` 里已经有四道检查（会话存在 404 / 缺 Key 503 / 重复任务 409 /
payload 必需键 422）。它们都是"请求本身合法吗"。而**需求基线是否确认**是另一类：
请求完全合法，只是**顺序不对** —— 用户还没通读并确认 PRD，就要生成下游产物。

顺序不对的后果不是报错，是**产物基于一份还没被确认的需求**：接口文档与提示词都从 PRD
推导，PRD 还在被改的时候生成它们，用户拿到的是"看起来完整、其实对着旧需求"的东西。
所以这里返回 400（Bad Request：请求在当前状态下不该被处理），而不是 409/422。

## 为什么是函数而不是中间件

规则只有三行，但**必须能被单独断言**（`smoke_check` 直接喂 session_data 验四种组合），
也要能被将来别的入口复用（比如导出的"必须三份都确认基线"）。中间件会把这条业务规则
藏进请求生命周期里，测试只能靠起服务去撞。

## 与前端那套 `deliveryStage.ts` 的关系

⛔ **本仓库没有 `deliveryStage.ts`**（04 篇之前的工单里出现过这个名字，但它从未存在）。
前端的可用性判据在 `App.tsx` 里（按钮 `disabled` + 一句说明）。
这里**不复制前端的判据**：前端拦是为了让用户看得见原因，后端拦是因为不能只信前端。
两边判据必须一致 —— 不一致时以**后端**为准，前端负责把它翻译成人话。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

__all__ = [
    "BASELINE_GATED_ARTIFACTS",
    "BaselineNotConfirmed",
    "assert_can_create_job",
    "baseline_confirmed",
]


class BaselineNotConfirmed(ValueError):
    """需求基线未确认 —— 路由层翻译成 **400**。"""


#: 需要"需求基线已确认"才能开的**生成**产物。
#:
#: ⚠️ 只列生成，不列优化（`optimize-api-docs` / `optimize-prompts`）：优化是**改一份已有的
#: 文档**，PRD 变了顶多让被改的文档过时（那是"过期提示"要管的事，见 `derived_from`），
#: 不该连"修一个错别字"都拦住。生成则是在**制造**一份基于 PRD 的新产物。
BASELINE_GATED_ARTIFACTS = frozenset({"api-docs", "prompts"})

#: Session 快照里的字段名（`V2SessionData`，前端 `types/session.ts` 同名字段）。
BASELINE_FLAG_KEY = "prdBaselineConfirmed"


def baseline_confirmed(session_data: Mapping[str, Any] | None) -> bool:
    """Session 快照里"需求基线已确认"了吗。

    ⚠️ **只认严格 `True`**：老快照没有这个键（`None`）、前端传了字符串 `"true"`、
    或者存了个 `1`，都算**未确认**。这里刻意不做宽松转换 —— 一个"大概算确认了"的
    守卫等于没有守卫，而失败方向（要求用户去点一下确认）是安全的那个方向。
    """
    return bool(session_data) and session_data.get(BASELINE_FLAG_KEY) is True


def assert_can_create_job(*, artifact: str, session_data: Mapping[str, Any] | None) -> None:
    """开生成任务前的**业务前置**。不满足就抛 `BaselineNotConfirmed`。

    Args:
        artifact: `POST /api/jobs` 的 `artifact`（生成用 `api-docs` / `prompts`）。
        session_data: 该会话的快照（`json.loads(session.session_data)`）。

    Raises:
        BaselineNotConfirmed: 受管产物 + 基线未确认。
    """
    if artifact not in BASELINE_GATED_ARTIFACTS:
        return
    if baseline_confirmed(session_data):
        return
    raise BaselineNotConfirmed(
        f"{artifact} 必须基于已确认的需求基线 —— 请先到 PRD 审核页通读全文并点「确认需求基线」。"
    )
