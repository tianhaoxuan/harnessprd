"""会话（Session）的对外模型 —— 供 API 层直接使用。

## 与「方案」（`plans` 表）的关系

**一份会话 = 一条方案记录**：`plans.snapshot` 这一列装的就是会话快照 JSON。
存储层沿用已经建好、带 CHECK 与索引的 `plan_repository`，本模块只提供**面向会话的字段名**：
对外叫 `session_data`（而不是 `snapshot`）—— 它就是整份工作台 state。

## 快照形状

对齐前端 `App.tsx` 的 `SessionData`：
`sessionId / formVersion / form / messages / roundIndex / documents / viewState / updatedAt`，
外加工作台运行期的 `entryMode` 等。

⚠️ **形状归前端所有**：这里不建同构模型、不逐字段校验（理由见 `plan_models` 的说明）——
后端再抄一份字段清单，前端加字段时不会有任何报错，只会悄悄过期。
本层只依赖其中两个字段：`viewState`（降级）与 `documents.prd.content`（取标题）。
"""

from __future__ import annotations

from pydantic import BaseModel

from services.plan_models import PlanEntryMode, PlanStage, PlanStatus
from services.state import DocKind


class SessionSummary(BaseModel):
    """列表项。**不含 `session_data`** —— 列表页不该把整份 state 读出来。"""

    id: str
    title: str
    entry_mode: PlanEntryMode
    current_stage: PlanStage
    status: PlanStatus
    created_at: str
    updated_at: str


class SessionRecord(SessionSummary):
    """完整会话：摘要 + 快照原文。"""

    session_data: str
    """工作台 state 的 JSON 原文。**已做过 `generating-*` 降级。**"""


class SessionLoad(SessionRecord):
    """`get_session()` 的结果，比记录多两条"这次读取发生了什么"。"""

    downgraded_from: PlanStage | None = None
    """原本的 `viewState`（`generating-*`）。`None` = 这次没降级。"""

    interrupted_kind: DocKind | None = None
    """被打断的是哪份产物。前端据此提示「上次生成被刷新打断了」（与工作台一致）。"""


class SessionSaveResult(BaseModel):
    """`save_session()` 的结果。"""

    id: str
    created: bool
    """`True` = 这次是新建；`False` = 更新了已有记录。"""
    title: str
    """落库后的标题。**更新已有记录时它是原值**（标题不随更新改写）。"""
