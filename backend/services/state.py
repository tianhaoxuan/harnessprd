"""会话状态的**内部**数据契约。

⚠️ 这是内部状态（`docs/状态数据设计.md` §2）；**对外视图** `SessionSnapshot` 在
`api/schemas.py`。设计原则②要求两者分离：

- 内部 `SessionState` 可以自由增删字段
- 对外 `SessionSnapshot` **只增不减**，否则前端会被内部重构反复打断

方向上 `api → services → core` 单向，所以枚举定义在这里、由 `api/schemas.py`
import 复用 —— 两边各写一份必然漂移。

**本模块目前只放两边都要用的枚举。** `SessionState` / `DialogueState` / `DocumentState`
等完整模型在实现 `SessionStore` 时补上（§2.1–2.3 已经给全）；
现在提前定义它们没有收益，反而会成为尚未验证的第二份契约。
"""

from __future__ import annotations

from typing import Literal

# 出处：docs/状态数据设计.md §2.1
SessionPhase = Literal[
    "created",
    "form_editing",
    "dialogue",
    "dialogue_summary",
    "prd_generating",
    "prd_review",
    "api_generating",
    "api_review",
    "prompts_generating",
    "prompts_review",
    "completed",
    "generation_failed",
    "abandoned",
]

DocKind = Literal["prd", "api", "prompts"]

DocStatus = Literal[
    "not_started",
    "generating",
    "pending_review",
    "approved",
    "stale",
    "failed",
]

# 出处：docs/对话阶段设计.md §4
DialogueStage = Literal["S0", "S1", "S2", "S3", "S4", "S5"]
