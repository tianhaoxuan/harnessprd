"""方案的 **Service 层**：校验、兜底与时间戳都在这里，SQL 一行没有。

分层：`api`（将来加路由）→ **本文件** → `plan_repository`（只懂 SQL）。
与仓库其它 service 一致：**不 import fastapi**，也不 import 任何 LLM 模块。

它负责三件"仓储不该管"的事：

1. **快照必须是 JSON 对象**这件事的校验（仓储只管把字符串写进去）；
2. **标题兜底**：留空时尽力从快照里取产品名，取不到用 `未命名方案`；
3. **时间戳**：`created_at` / `updated_at` 由这里生成 —— 仓储只接受"已经算好的时间"。

这样切的理由：把"什么算合法输入"和"怎么写进库"分开之后，将来加 HTTP 路由时，
路由只需要把请求体转成 `PlanCreate`，不必再关心这些规则；规则也只有这一份。
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.config import Settings, get_settings
from services.plan_models import (
    ENTRY_MODES,
    STAGES,
    STATUSES,
    TITLE_FALLBACK,
    PlanCreate,
    PlanRecord,
    PlanSummary,
    PlanUpdate,
)
from services.plan_repository import PlanRepository, dumps_snapshot

logger = logging.getLogger(__name__)

TITLE_MAX_LENGTH = 120
"""标题截断长度。列表页要显示它，而快照里的产品名可能是一整句话。"""

_TITLE_KEYS = ("product_name", "productName")
"""快照里可能放产品名的两处键：20 题表单用 `product_name`，结构化表单用 `productName`。

⚠️ 这是**尽力而为**的兜底，不是契约：读不到就用 `未命名方案`，**绝不**因此报错，
也**绝不**把推导出来的标题写回快照（那会污染前端自己拥有的那份 state）。
"""


class PlanValidationError(ValueError):
    """调用方给的东西不合法（快照不是 JSON 对象、枚举取值不在允许集合里……）。

    继承 `ValueError`：将来 HTTP 层可以直接把它映射成 **422**，不用再建一套异常类型。
    """


def now_iso() -> str:
    """当前时间（UTC、**毫秒**精度、ISO8601）。同一格式下**字典序 = 时间序**。

    ⚠️ 用毫秒而不是秒：列表按 `updated_at` 排序，而"同一秒内建两条"在自动化与测试里
    太常见了 —— 秒精度下它们会完全并列，顺序只能靠 id 兜底（一个随机值），
    于是列表顺序看起来是随机的（实测踩到）。毫秒把这个窗口缩到几乎不会撞上；
    真撞上了也还有 `rowid` 兜底，见 `plan_repository.list_summaries`。
    """
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def normalize_snapshot(value: str | Mapping[str, Any]) -> str:
    """校验并返回**可入库的快照文本**。

    传字符串就**原样返回**（不重新序列化）—— 这是"往返逐字节一致"的关键：
    前端存进去什么，拿回来就是什么。重新序列化虽然通常等价，但会改变键顺序与
    空白，让 `git diff` 之外的任何比对（哈希、字符串相等）失效。
    """
    text = dumps_snapshot(value)
    if not text.strip():
        raise PlanValidationError("快照是空的 —— 至少也要是个 `{}` 这样的 JSON 对象")
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise PlanValidationError(f"快照不是合法 JSON：{exc.msg}（第 {exc.pos} 个字符）") from exc
    if not isinstance(parsed, dict):
        raise PlanValidationError(
            f"快照必须是 JSON **对象**（工作台 state 是个字典），实际是 {type(parsed).__name__}"
        )
    return text


def derive_title(snapshot_text: str, explicit: str = "") -> str:
    """标题：显式给的优先；没给就从快照里**尽力**找产品名；再兜底成 `未命名方案`。"""
    if explicit.strip():
        return explicit.strip()[:TITLE_MAX_LENGTH]
    try:
        parsed = json.loads(snapshot_text)
    except json.JSONDecodeError:  # pragma: no cover - 快照已经过 normalize_snapshot
        return TITLE_FALLBACK
    form = parsed.get("form") if isinstance(parsed, dict) else None
    if isinstance(form, Mapping):
        for key in _TITLE_KEYS:
            candidate = form.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()[:TITLE_MAX_LENGTH]
    return TITLE_FALLBACK


def _check_enum(name: str, value: str, allowed: tuple[str, ...]) -> None:
    """枚举校验。**在服务层挡一次**（数据库的 CHECK 是第二道防线，不是第一道）。"""
    if value not in allowed:
        raise PlanValidationError(f"{name} 只能是 {'、'.join(allowed)} 之一，收到 {value!r}")


class PlanService:
    """方案的业务入口。"""

    def __init__(
        self,
        settings: Settings | None = None,
        repository: PlanRepository | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._repository = repository or PlanRepository(
            self._settings.plans_db_path,
            busy_timeout_ms=self._settings.sqlite_busy_timeout_ms,
        )

    @property
    def repository(self) -> PlanRepository:
        return self._repository

    @property
    def db_path(self) -> Path:
        return self._repository.path

    def ensure_ready(self) -> None:
        """建表（幂等）。**应用启动时调用** —— 见 `main.py` 的 lifespan。"""
        self._repository.ensure_schema()

    # ---------------------------------------------------------------- 写

    def create(self, payload: PlanCreate) -> PlanRecord:
        """新建一条方案。返回**落库后**的完整记录（含生成好的 id 与时间戳）。"""
        # 枚举清单只从 `plan_models` 取（那边还同时被数据库 CHECK 与 smoke 断言引用）——
        # 在这里再抄一份就是第二份真相，改一处忘一处不会有任何报错。
        _check_enum("entry_mode", payload.entry_mode, ENTRY_MODES)
        _check_enum("current_stage", payload.current_stage, STAGES)
        _check_enum("status", payload.status, STATUSES)

        snapshot = normalize_snapshot(payload.snapshot)
        timestamp = now_iso()
        # 调用方可以指定 id（会话层的"标题兜底 = 记录 id"要用到，见 plan_models.PlanCreate.id）
        plan_id = payload.id or uuid.uuid4().hex
        self._repository.insert(
            {
                "id": plan_id,
                "title": derive_title(snapshot, payload.title),
                "entry_mode": payload.entry_mode,
                "current_stage": payload.current_stage,
                "status": payload.status,
                "snapshot": snapshot,
                "created_at": timestamp,
                "updated_at": timestamp,
            }
        )
        record = self.get(plan_id)
        if record is None:  # pragma: no cover - 刚插进去就读不到，只能是并发删了
            raise RuntimeError(f"方案 {plan_id} 插入后读不到 —— 请检查库文件是否被并发清理")
        return record

    def update(self, plan_id: str, payload: PlanUpdate) -> PlanRecord | None:
        """局部更新。`None` = 这条不存在（**不抛错**：由调用方决定是不是 404）。"""
        changes: dict[str, Any] = {}
        for field in ("entry_mode", "current_stage", "status"):
            value = getattr(payload, field)
            if value is not None:
                changes[field] = value
        if payload.snapshot is not None:
            changes["snapshot"] = normalize_snapshot(payload.snapshot)

        if payload.title is not None:
            # 显式传了空标题 → 退回到"从快照推导"，而不是把标题写成空串
            base = changes.get("snapshot")
            if base is None:
                existing = self._repository.get(plan_id)
                if existing is None:
                    return None
                base = str(existing["snapshot"])
            changes["title"] = derive_title(str(base), payload.title)

        hit = self._repository.update(plan_id, changes, updated_at=now_iso())
        return self.get(plan_id) if hit else None

    def delete(self, plan_id: str) -> bool:
        return self._repository.delete(plan_id)

    # ---------------------------------------------------------------- 读

    def list(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        entry_mode: str | None = None,
        status: str | None = None,
    ) -> list[PlanSummary]:
        """列表（按 `updated_at` 倒序）。**不含快照**。"""
        if entry_mode is not None:
            _check_enum("entry_mode", entry_mode, ENTRY_MODES)
        if status is not None:
            _check_enum("status", status, STATUSES)
        rows = self._repository.list_summaries(
            limit=max(1, min(int(limit), 200)),  # 上限 200：列表接口不该被拿来导出全库
            offset=max(0, int(offset)),
            entry_mode=entry_mode,
            status=status,
        )
        return [PlanSummary(**row) for row in rows]

    def get(self, plan_id: str) -> PlanRecord | None:
        row = self._repository.get(plan_id)
        return PlanRecord(**row) if row else None

    def count(self) -> int:
        return self._repository.count()
