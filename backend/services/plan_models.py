"""方案（工作台快照）的领域模型。

## 什么是"方案"

一条方案 = **一份完整的工作台 state 快照** + 几个"列表页要用的"摘要字段。
快照对应前端 `App.tsx` 的 `SessionData`（`sessionId / formVersion / form / messages /
roundIndex / documents / viewState / updatedAt`），以及工作台运行期的 `entryMode` 等。

## 为什么快照是**不解析的 JSON 原文**

快照的**形状归工作台（前端）所有**。后端再建一套同构模型就等于有了第二份真相：
前端加一个字段、后端那份不会跟着变，而且**不会有任何报错** —— 这类"悄悄过期"的镜像
在这个仓库里已经被反复证明是坑（`HANDOFF.md` §4 坑 #10 的同一类问题）。

所以这一层只做两件事：

1. 存**原文**：传字符串就原样存，往返逐字节一致（前端能拿回它自己写进去的东西）；
2. 只校验"它是个 JSON **对象**"—— 不是数组、不是标量、不是坏 JSON、不是空串。
   这足以挡住"存了半截/存了个空"这类真错误，又不越界去管里面有哪些字段。

列表页要用的 `title / entry_mode / current_stage / status` 单独成列：列表**不解析 JSON**，
否则一页 50 条就要解析 50 份快照（这是把它单独成列的唯一理由）。

## 枚举为什么直接照抄前端的取值

`PlanStage` 就是前端 `types/index.ts` 里 `ViewState` 的那 9 个取值，`PlanEntryMode` 就是
`EntryMode` 的那 3 个 —— **原样存，后端不解释**。刻意不做"粗粒度阶段"的映射：
多一层映射就多一处漂移点，而这一层并不需要理解阶段语义。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, Field

PlanEntryMode = Literal["structured", "prd-shortcut", "prompts-debug"]
"""三套入口，与前端 `EntryMode` 一一对应（见 `frontend/src/types/index.ts`）。"""

PlanStage = Literal[
    "form",
    "chatting",
    "generating-prd",
    "review-prd",
    "generating-api-docs",
    "review-api-docs",
    "generating-prompts",
    "review-prompts",
    "done",
]
"""当前阶段，与前端 `ViewState` 一一对应。**原样存，不做粗粒度归并。**"""

PlanStatus = Literal["draft", "active", "archived", "failed"]
"""方案自身的状态（与产物状态 `DocStatus` 无关，别混）：

| 值 | 含义 |
| --- | --- |
| `draft` | 还在填 / 还在生成，没定稿 |
| `active` | 当前正在用的那一条 |
| `archived` | 用户归档（列表默认仍显示，由调用方决定要不要过滤） |
| `failed` | 生成失败留下的一条（留痕，别静默丢掉） |
"""

ENTRY_MODES: tuple[PlanEntryMode, ...] = ("structured", "prd-shortcut", "prompts-debug")
STAGES: tuple[PlanStage, ...] = (
    "form",
    "chatting",
    "generating-prd",
    "review-prd",
    "generating-api-docs",
    "review-api-docs",
    "generating-prompts",
    "review-prompts",
    "done",
)
STATUSES: tuple[PlanStatus, ...] = ("draft", "active", "archived", "failed")

TITLE_FALLBACK = "未命名方案"
"""标题兜底。取不到产品名时用它，**不**把标题留空（列表里空标题最难查）。"""


class PlanSummary(BaseModel):
    """列表页要的那几列。**不含快照** —— 列表不该把整份 state 读出来。"""

    id: str
    title: str
    entry_mode: PlanEntryMode
    current_stage: PlanStage
    status: PlanStatus
    created_at: str
    """ISO8601（UTC，**毫秒**精度）。字符串存、字符串出，字典序即时间序。"""
    updated_at: str


class PlanRecord(PlanSummary):
    """一条完整方案：摘要 + 快照原文。"""

    snapshot: str
    """工作台 state 的 JSON **原文**。给进来什么样，出去就什么样。"""


class PlanCreate(BaseModel):
    """新建一条方案。"""

    id: str | None = None
    """记录 id。留空则服务层生成 uuid4。

    允许调用方指定，是因为**标题的兜底就是记录 id**（见 `session_service.parse_prd_title`）：
    要写出"标题 = id"这条规则，id 必须在写库**之前**就定下来，否则得先插一次再更新一次。
    """
    title: str = ""
    """留空则**尽力**从快照里取产品名（见 `plan_service.derive_title`），再兜底成 `未命名方案`。"""
    entry_mode: PlanEntryMode = "structured"
    current_stage: PlanStage = "form"
    status: PlanStatus = "draft"
    snapshot: str | Mapping[str, Any] = Field(default_factory=dict)
    """JSON 原文（字符串）或一个映射（会被序列化成 JSON 对象）。"""


class PlanUpdate(BaseModel):
    """局部更新。**只更新显式给出的字段**（`None` = 不动）。"""

    title: str | None = None
    entry_mode: PlanEntryMode | None = None
    current_stage: PlanStage | None = None
    status: PlanStatus | None = None
    snapshot: str | Mapping[str, Any] | None = None
