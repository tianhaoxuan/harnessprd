r"""会话业务层：API 直接调它。四个操作 + 两条会话特有的规则。

## 四个操作

| 操作 | 行为 |
| --- | --- |
| `list_sessions()` | 按 `updated_at` 倒序；**只回摘要**，不带 `session_data` |
| `get_session(id)` | 回完整快照；若 `viewState` 是 `generating-*`，**先降级再返回** |
| `save_session(data, id?)` | 无 id → 新建并返回新 id；有 id → 更新；**写入前降级**；标题只在新记录时解析写入 |
| `delete_session(id)` | 不存在 → 抛 `SessionNotFound`（HTTP 层映射 404） |

## 规则一：`generating-*` 降级

与前端 `App.tsx` 的 `loadSession()` **同一张表**（那边叫 `IN_FLIGHT_VIEWS`）：

    generating-prd        → review-prd        （被打断的是 PRD）
    generating-api-docs   → review-api-docs   （接口文档）
    generating-prompts    → review-prompts    （提示词套件）

为什么必须降级：`generating-*` 是"**当时**正在生成"的意思，而生成过程**只活在内存里** ——
进程没了、请求断了，就没有任何东西在跑。把它原样返回，客户端会显示一个永远不会完成的
"生成中"，用户等到天荒地老（`会话持久化方案` §7.4 的孤儿生成态）。降级到"待审核"是诚实的表述：
**稿子可能已经写了一半或写完了，但没人知道** —— 所以调用方还需要知道"是哪一份被打断了"。

⚠️ 只做降级，**不**移植前端 `fallbackView()` 那套（"没有 viewState 时按产物有没有内容推断"）。
那是另一条规则（兼容缺少 `viewState` 的旧数据），本次没有要求，不做也不该顺手加。

## 规则二：标题

优先级：**PRD 正文的第一个 `#` 一级标题** → **`产品名称：xxx`** → **记录 id**。

- 只在**新建**时解析写入。更新已有记录**不动 title** —— 用户可能手工改过，
  而且更新时正文常常还没写完（拿半份正文去重命名是负优化）。
- 优先级 1 要求是**一级**标题：正则写 `^#\s+`，所以 `## 2. 功能需求` 不会被误当成标题。

## 分层

本层**不直接写 SQL**：存储、校验、时间戳都委派给 `PlanService`（同一张表）。
另起一套实现就等于两份真相，改一处忘一处不会有任何报错。本层只加会话特有的两条规则。
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from collections.abc import Mapping
from typing import Any

from core.config import Settings
from services.plan_models import (
    ENTRY_MODES,
    STAGES,
    TITLE_FALLBACK,  # noqa: F401 - 供调用方参考的兜底文案（本层兜底是记录 id）
    PlanCreate,
    PlanUpdate,
)
from services.plan_service import PlanService, TITLE_MAX_LENGTH, normalize_snapshot
from services.session_models import (
    SessionLoad,
    SessionSaveResult,
    SessionSummary,
)
from services.state import DocKind

logger = logging.getLogger(__name__)

DOWNGRADES: dict[str, tuple[str, DocKind]] = {
    "generating-prd": ("review-prd", "prd"),
    "generating-api-docs": ("review-api-docs", "api"),
    "generating-prompts": ("review-prompts", "prompts"),
}
"""`generating-*` → (对应的审核视图, 被打断的产物)。与前端 `IN_FLIGHT_VIEWS` 一致。"""

_H1_TITLE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)
"""**一级**标题：`# ` 后面直接跟内容。`## ` 不会命中（`#` 后面不是空白）。"""

_NAME_LINE = re.compile(r"产品名称\s*[：:]\s*(.+?)\s*$", re.MULTILINE)
"""`产品名称：xxx`（中英文冒号都认）。"""

_TITLE_KEYS = ("product_name", "productName")
"""快照里可能放产品名的两处键：20 题表单用 `product_name`，结构化表单用 `productName`。

与 `plan_service._TITLE_KEYS` 同源但**各自持有**：那是方案层的兜底，这里是会话层的
（多一档"表单产品名"）。合成一处反而会让两层互相拖住，改动理由也不同。"""
"""`产品名称：xxx`（中英文冒号都认）。"""


class SessionNotFound(LookupError):
    """记录不存在。

    继承 `LookupError` 而不是自定义 `Exception`：HTTP 层据此返 **404**，
    语义也对（"查不到"就是 `LookupError`）。本层**不 import fastapi** ——
    传输协议的细节归 `api/`。
    """


# ---------------------------------------------------------------- 纯函数（可单测，不碰库）


def downgrade_view_state(view: str) -> tuple[str, DocKind | None]:
    """把单个 `viewState` 降级。返回 `(降级后的视图, 被打断的产物)`。"""
    target = DOWNGRADES.get(view)
    return target if target else (view, None)


def downgrade_session_data(session_data: str) -> tuple[str, str | None, DocKind | None]:
    """把快照里的 `viewState` 降级。

    返回 `(快照文本, 原 viewState, 被打断的产物)`。

    ⚠️ **不需要降级时原样返回**（不改键序、不改空白）。这个函数在每次保存时都会被调用，
    如果它顺手重新序列化一遍，"存进去什么就拿回什么"这条性质就没了 ——
    而且库里每存一次都会产生一次无意义的文本变更。
    """
    try:
        parsed = json.loads(session_data)
    except json.JSONDecodeError:  # pragma: no cover - 调用前已过 normalize_snapshot
        return session_data, None, None
    if not isinstance(parsed, dict):
        return session_data, None, None
    view = parsed.get("viewState")
    if not isinstance(view, str):
        return session_data, None, None
    downgraded, kind = downgrade_view_state(view)
    if kind is None:
        return session_data, None, None
    parsed["viewState"] = downgraded
    # 只有真要改的时候才重新序列化（紧凑 + 不转义中文，与 plan_repository 的口径一致）
    return json.dumps(parsed, ensure_ascii=False, separators=(",", ":")), view, kind


def prd_body(session_data: str) -> str:
    """取 PRD 正文。

    来源是前端持久化的形状 `documents.prd.content`；取不到（还没生成过 PRD、
    或快照不是对象）返回空串 —— 调用方据此走下一优先级，而不是报错。
    """
    try:
        parsed = json.loads(session_data)
    except json.JSONDecodeError:
        return ""
    if not isinstance(parsed, dict):
        return ""
    documents = parsed.get("documents")
    if not isinstance(documents, Mapping):
        return ""
    prd = documents.get("prd")
    if not isinstance(prd, Mapping):
        return ""
    content = prd.get("content")
    return content if isinstance(content, str) else ""


def parse_prd_title(session_data: str, fallback: str) -> str:
    """标题解析：一级标题 → `产品名称：xxx` → 表单里的产品名 → `fallback`（记录 id）。

    第三档（表单产品名）是**实测补上的**：新建方案在"开始澄清对话"那一刻就落库，
    那时还没有 PRD 正文可读 —— 只按前两档会退成记录 id，列表里显示一串十六进制，
    而用户刚在表单里填过产品名（`session_data.form.product_name`）。
    """
    body = prd_body(session_data)
    if body:
        for pattern in (_H1_TITLE, _NAME_LINE):
            matched = pattern.search(body)
            if matched:
                title = matched.group(1).strip()
                if title:
                    return title[:TITLE_MAX_LENGTH]
    # 退一步：快照里的表单作答。20 题表单用 `product_name`，结构化表单用 `productName`。
    try:
        parsed = json.loads(session_data)
    except json.JSONDecodeError:
        parsed = None
    form = parsed.get("form") if isinstance(parsed, Mapping) else None
    if isinstance(form, Mapping):
        for key in _TITLE_KEYS:
            candidate = form.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()[:TITLE_MAX_LENGTH]
    return fallback


def derive_summary_fields(session_data: str) -> tuple[str | None, str | None]:
    """从快照推导摘要列：`(entry_mode, current_stage)`。

    推导而不是让调用方另传一份 —— 摘要列与快照**必须一致**，否则列表页显示"表单阶段"、
    点进去却停在"提示词"，而且没有任何东西会报错。推导不出来的返回 `None`
    （= "这次别动这一列"），由调用方决定新建时的默认值。
    """
    try:
        parsed = json.loads(session_data)
    except json.JSONDecodeError:
        return None, None
    if not isinstance(parsed, dict):
        return None, None
    entry = parsed.get("entryMode")
    view = parsed.get("viewState")
    return (
        entry if isinstance(entry, str) and entry in ENTRY_MODES else None,
        view if isinstance(view, str) and view in STAGES else None,
    )


# ---------------------------------------------------------------- Service


class SessionService:
    """会话业务入口。存储与校验委派给 `PlanService`（同一张表）。"""

    def __init__(
        self,
        settings: Settings | None = None,
        plans: PlanService | None = None,
    ) -> None:
        self._plans = plans or PlanService(settings)

    @property
    def plans(self) -> PlanService:
        return self._plans

    def ensure_ready(self) -> None:
        """建表（幂等）。应用启动时调用 —— 见 `main.py` 的 lifespan。"""
        self._plans.ensure_ready()

    # ------------------------------------------------------------ 读

    def list_sessions(self, *, limit: int = 50, offset: int = 0) -> list[SessionSummary]:
        """列表：按 `updated_at` 倒序，**只回摘要**（不读 `session_data` 那一列）。"""
        return [
            SessionSummary(**row.model_dump())
            for row in self._plans.list(limit=limit, offset=offset)
        ]

    def get_session(self, session_id: str) -> SessionLoad:
        """取完整快照。`generating-*` **先降级再返回**。

        降级**不回写**：读操作不该改数据。库里若因故留着 `generating-*`（例如别的写入方
        绕过本层），每次读都会稳定地降级成同一个结果 —— 这是可预期的，不是"时好时坏"。
        """
        record = self._plans.get(session_id)
        if record is None:
            raise SessionNotFound(f"会话不存在：{session_id}")
        text, original, kind = downgrade_session_data(record.snapshot)
        return SessionLoad(
            **record.model_dump(exclude={"snapshot"}),
            session_data=text,
            downgraded_from=original,
            interrupted_kind=kind,
        )

    # ------------------------------------------------------------ 写

    def save_session(
        self,
        session_data: str | Mapping[str, Any],
        session_id: str | None = None,
    ) -> SessionSaveResult:
        """新建（无 id）或更新（有 id）。**写入前降级**，标题只在新记录时解析。"""
        text = normalize_snapshot(session_data)
        # 写入前降级：库里**不该**出现 generating-*（它是"此刻在跑"的意思，
        # 而落库这个动作本身就说明那一刻已经过去了）。
        text, downgraded_from, _ = downgrade_session_data(text)
        if downgraded_from is not None:
            logger.info("保存会话时把 %s 降级后再写入（原状态只存在于内存）", downgraded_from)
        entry_mode, current_stage = derive_summary_fields(text)

        if session_id is None:
            new_id = uuid.uuid4().hex
            record = self._plans.create(
                PlanCreate(
                    id=new_id,
                    # 标题的兜底**就是记录 id**：解析不出来也要有个能认的名字，
                    # 空标题在列表里最难查（不知道是哪一条）。
                    title=parse_prd_title(text, new_id),
                    entry_mode=entry_mode or "structured",
                    current_stage=current_stage or "form",
                    snapshot=text,
                )
            )
            return SessionSaveResult(id=record.id, created=True, title=record.title)

        updated = self._plans.update(
            session_id,
            PlanUpdate(entry_mode=entry_mode, current_stage=current_stage, snapshot=text),
        )
        if updated is None:
            raise SessionNotFound(f"会话不存在：{session_id}")
        # ⚠️ 这里**不传 title** —— 更新不改标题（见模块 docstring 的规则二）。
        return SessionSaveResult(id=updated.id, created=False, title=updated.title)

    def delete_session(self, session_id: str) -> None:
        """删除。不存在 → `SessionNotFound`（HTTP 层 404）。"""
        if not self._plans.delete(session_id):
            raise SessionNotFound(f"会话不存在：{session_id}")
