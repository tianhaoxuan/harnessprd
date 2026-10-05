r"""文档槽位与版本链的**业务层**：API 直接调它。

## 数据模型一句话

**一个 Session × 一种 `doc_type` = 一个槽位（`documents` 一行）**，槽位里挂一条版本链
（`document_versions` 多行），`documents.current_version_id` 指向**当前生效的那一版**。

    槽位 (documents)                版本链 (document_versions)
    ┌──────────────────────┐        ┌──────────────────────────────┐
    │ prd                  │───────>│ v3 checkpoint  is_current ✓  │
    │ current_version_id ──┼────────│ v2 generate                  │
    └──────────────────────┘        │ v1 generate                  │
                                    └──────────────────────────────┘

**核心原则：历史行只追加，正文不可原地修改。** 唯一的例外是 current 那一行的
`content`（优化 / 自动保存就地更新，不升号）与任意一行的 `metadata.change_note`
（用户备注）。这条原则是"能恢复、能对比、不会丢用户东西"的全部依据。

## 三种"升号"与两种"不升号"

| `source_kind` | 谁调 | 行为 |
| --- | --- | --- |
| `generate` | 整份生成 Job 完成 | 升号、切 current；**再次**生成也是一版新的，旧版原样保留 |
| `checkpoint` | 用户「保存为新版本」 | 升号、切 current；**不继承**上一版的 metadata |
| `restore` | 用户「恢复此版本」 | 以历史正文升号、切 current；合并历史 metadata |
| `optimize` | 优化 Job 完成 | **只改 current 正文**，`version_no` 不变 |
| `auto_save` | Session save / debounce | 同上，不升号 |
| `import` | 老数据迁移 | 由 02 篇决定是首版还是追加 |

## 两条写入链路（本层被谁调）

    生成 / 优化 Job 完成（job_runner）
      ├─ sync_from_job(...)          ← 版本层：升新版（生成）或就地更新（优化）
      └─ session.sync_*_completed    ← 镜像：session_data.documents.<kind>.content

    用户手改，前端防抖保存（POST /api/session/save）
      └─ session_service
           ├─ maybe_migrate_from_session(...)      ← 老数据补 v1（source_kind=import）
           └─ sync_session_contents(...)           ← source_kind=auto_save，**不升号**

两者的方向刚好相反（Job：版本层 → 镜像；保存：镜像 → 版本层），但**顺序一致**：
都是先写版本层、再写镜像（需求 §七）。本层只负责版本那一半 —— 它**不解析 session_data**
（那是会话层的知识，见 `session_service.session_document_contents`）。

## ⚠️ 需求原文里的一处自相矛盾，这里按"更具体的那一条"实现

- 方法表（§5）的 `restore_version` 第 6 步写的是「**并合并历史版 metadata（如 review）**」；
- `metadata_json` 约定（§7）的表格写的是「checkpoint / **restore** → 不继承（新版无 review）」。

两处对 restore 给出了相反的要求。本实现**采用 §5 的算法**（restore 合并历史 metadata），
理由：restore 出来的新版**正文与历史版逐字相同**，那一版的 review 审的正是这份正文，
所以它是**有效结论**而不是"过期结论"；而 checkpoint 时用户可能已经手改过正文，
继承旧 review 就会让界面把一个没审过的稿子标成"已审"——那是在撒谎。
（`restore` 仍然会丢掉历史版的 `change_note`：备注是"用户对这一版说的话"，
搬到新版上会变成一句答非所问的注释。）

## ⚠️ 依赖方向：documents → plans，**不是** documents → sessions

校验"会话在不在"走的是 `PlanService`（会话与方案是同一张表），异常也用自己的
`DocumentNotFound`。**刻意不 import `services.session_service`**：02 篇的
「Session debounce 同步 current」要求会话层反过来调本层，这里一旦依赖会话层，
那条链路一落地就是循环导入。

## 事务

`checkpoint` / `restore_version` / `update_current_content` 都是"插版本 + 切 current +
推进 `updated_at`"三件事，全部包在**同一个 `with repository.connection()`** 里 ——
一次提交，中途失败则整条回滚（不会出现"版本插进去了但 current 还指着旧的"）。
"""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from core.config import Settings, get_settings
from services.document_version_repository import (
    DOC_TYPES,
    DocType,
    DocumentVersionRepository,
    VersionSourceKind,
    dumps_metadata,
    load_metadata,
)
from services.job_models import content_artifact_of, is_optimize_artifact
from services.plan_service import PlanService, now_iso

logger = logging.getLogger(__name__)

CONTENT_PREVIEW_CHARS = 200
"""版本列表里 `content_preview` 取正文前多少字符。

⚠️ 只供内部与后续扩展用，**不是自动摘要**（前端侧栏不拿它当摘要展示）。
真正的"这一版是什么"由用户备注（`metadata.change_note`）承担。
"""


class DocumentVersionError(Exception):
    """本模块所有异常的基类（便于调用方一次捕获整类）。"""


class DocumentNotFound(DocumentVersionError, LookupError):
    """会话 / 槽位 / 版本查不到 → HTTP **404**。

    继承 `LookupError` 而不是自定义异常：语义对（"查不到"就是 `LookupError`），
    HTTP 层据此返 404。本层**不 import fastapi** —— 传输协议的细节归 `api/`。
    """


class InvalidDocumentRequest(DocumentVersionError, ValueError):
    """请求本身不合法 → HTTP **400**。

    四类：`doc_type` 不在枚举里、restore 的目标就是 current、restore 的历史版没有正文、
    checkpoint 时"既没有 current 也没带 content"（无内容可保存）。
    """


# ---------------------------------------------------------------- 纯函数（可单测，不碰库）


def content_preview(content: str, limit: int = CONTENT_PREVIEW_CHARS) -> str:
    """正文前 `limit` 个字符（版本列表用）。

    刻意**不补省略号**：调用方可能要拿这一小段做二次处理（例如取首行当标题），
    多出来的 `…` 会污染内容。要显示"还有更多"是前端的事。
    """
    return content[:limit]


def merge_metadata(
    base: Mapping[str, Any] | None, patch: Mapping[str, Any] | None
) -> dict[str, Any]:
    """**浅**合并 metadata：同名键由 `patch` 覆盖；`patch` 里值为 `None` 的键表示**删除**。

    为什么是浅合并而不是深合并：`run_summary` / `review` / `change_note` 都是**顶层键**，
    整块替换正是需求要的语义（"新 Job 的 run_summary、review 覆盖同名字段"）。
    深合并会让"新 run_summary 换了个 request_id、却把旧 steps 留下了"这种
    半新半旧的结果看起来完全正常。

    "`None` = 删除"这一条是给用户清空备注用的（`change_note: ""` → 键消失，
    而不是留一个空串让前端判断"这算有备注吗"）。
    """
    merged: dict[str, Any] = dict(base or {})
    for key, value in (patch or {}).items():
        if value is None:
            merged.pop(key, None)
        else:
            merged[key] = value
    return merged


# ---------------------------------------------------------------- 领域模型


class DocumentSlot(BaseModel):
    """一个文档槽位（`documents` 一行 + 当前版本的版本号）。

    `current_version_no` 由仓储 LEFT JOIN 带出来（不是表里的列）——
    三种读路径形状一致，`DocumentSlot(**row)` 到处都能直接构造。
    """

    id: str
    session_id: str
    doc_type: DocType
    current_version_id: str | None = None
    """指向 `document_versions.id`。**尚未生成任何版本时为 `None`。**"""
    current_version_no: int | None = None
    created_at: str
    updated_at: str

    @property
    def has_version(self) -> bool:
        return self.current_version_id is not None


class DocumentVersionRecord(BaseModel):
    """一个版本快照（`document_versions` 一行，含全文）。

    ⚠️ 对外视图（不含全文的列表项、含全文的详情）在 `api/schemas.py`，两者刻意分开：
    内部记录可以自由加列，对外契约只增不减。
    """

    id: str
    document_id: str
    version_no: int
    content: str = ""
    source_kind: VersionSourceKind
    source_job_id: str | None = None
    parent_version_id: str | None = None
    metadata_json: str | None = None
    created_at: str

    def metadata(self) -> dict[str, Any]:
        """解析 `metadata_json`（空 / 坏数据返回 `{}`）。"""
        return load_metadata(self.metadata_json)

    def change_note(self) -> str | None:
        """用户备注（`metadata.change_note`）；没有或为空串 → `None`。"""
        note = self.metadata().get("change_note")
        return note if isinstance(note, str) and note.strip() else None


# ---------------------------------------------------------------- Service


class DocumentVersionService:
    """文档槽位与版本链的业务入口。SQL 一行没有（全在 `document_version_repository`）。"""

    def __init__(
        self,
        settings: Settings | None = None,
        plans: PlanService | None = None,
        repository: DocumentVersionRepository | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        # 会话（= 方案）存储：本层只用它回答"这个 session_id 存在吗"。
        self._plans = plans or PlanService(self._settings)
        self._repository = repository or DocumentVersionRepository(
            self._settings.plans_db_path,
            busy_timeout_ms=self._settings.sqlite_busy_timeout_ms,
        )

    @property
    def repository(self) -> DocumentVersionRepository:
        return self._repository

    @property
    def db_path(self) -> Path:
        return self._repository.path

    def ensure_ready(self) -> None:
        """建两张表（幂等）。**应用启动时调用** —— 见 `main.py` 的 lifespan。"""
        self._repository.ensure_schema()

    # ------------------------------------------------------------ 校验

    @staticmethod
    def _require_doc_type(doc_type: str) -> DocType:
        """`doc_type` 必须在枚举里，否则 **400**（不是 422：它在路径里，形状是合法的）。"""
        if doc_type not in DOC_TYPES:
            raise InvalidDocumentRequest(
                f"doc_type 只能是 {'、'.join(DOC_TYPES)} 之一，收到 {doc_type!r}"
            )
        return doc_type  # type: ignore[return-value]

    def _require_session(self, session_id: str) -> None:
        """会话必须已存在，否则 **404**（见模块 docstring 的依赖方向说明）。"""
        if self._plans.get(session_id) is None:
            raise DocumentNotFound(f"会话不存在：{session_id}")

    # ------------------------------------------------------------ 槽位

    def ensure_document(self, session_id: str, doc_type: str) -> DocumentSlot:
        """取槽位，没有就建一个（`current_version_id` 为空）。**幂等**。

        这是所有对外方法的第一步（需求 §5 的 `ensure_document`）。校验也在这里：
        `doc_type` 非法 → 400、会话不存在 → 404。

        ⚠️ **GET 接口会调用它，于是"读"会写库**（首次访问时建出三个空槽位）。
        这是需求 §6.1 的形状要求的：响应里 `document_id` 有值、`current_version_id` 为
        `null`，而 `document_id` 只能来自一行真实记录。三个槽位是**预置**而不是"用到才建"，
        所以侧栏永远能拿到稳定的三行。
        """
        doc_type = self._require_doc_type(doc_type)
        self._require_session(session_id)
        with self._repository.connection() as conn:
            return self._ensure_document(conn, session_id, doc_type)

    def _ensure_document(
        self, conn: sqlite3.Connection, session_id: str, doc_type: str
    ) -> DocumentSlot:
        """`ensure_document` 的事务内版本（调用方已经在一次连接里）。"""
        row = self._repository.get_document(conn, session_id, doc_type)
        if row is None:
            timestamp = now_iso()
            # `INSERT OR IGNORE` + 重读：并发下两个请求同时发现槽位不存在时，
            # 后到的那个静默跳过、读回先到的那一行，而不是抛 IntegrityError。
            self._repository.insert_document_or_ignore(
                conn,
                {
                    "id": uuid.uuid4().hex,
                    "session_id": session_id,
                    "doc_type": doc_type,
                    "current_version_id": None,
                    "created_at": timestamp,
                    "updated_at": timestamp,
                },
            )
            row = self._repository.get_document(conn, session_id, doc_type)
        if row is None:  # pragma: no cover - INSERT OR IGNORE 之后必然读得到
            raise DocumentNotFound(f"文档槽位建不出来：session={session_id} doc_type={doc_type}")
        return DocumentSlot(**row)

    def get_documents_by_session(self, session_id: str) -> list[DocumentSlot]:
        """三种槽位的摘要，**顺序固定 = `DOC_TYPES`**（前端侧栏按这个顺序排）。

        尚无版本的槽位也返回，`current_version_id` / `current_version_no` 为 `None`。
        全部三个槽位在**同一个事务**里补齐，避免"列表里时有时无"。
        """
        self._require_session(session_id)
        with self._repository.connection() as conn:
            known = {
                row["doc_type"] for row in self._repository.list_documents(conn, session_id)
            }
            timestamp = now_iso()
            for doc_type in DOC_TYPES:
                if doc_type in known:
                    continue
                self._repository.insert_document_or_ignore(
                    conn,
                    {
                        "id": uuid.uuid4().hex,
                        "session_id": session_id,
                        "doc_type": doc_type,
                        "current_version_id": None,
                        "created_at": timestamp,
                        "updated_at": timestamp,
                    },
                )
            rows = {
                row["doc_type"]: row
                for row in self._repository.list_documents(conn, session_id)
            }
        return [DocumentSlot(**rows[doc_type]) for doc_type in DOC_TYPES if doc_type in rows]

    # ------------------------------------------------------------ 读版本

    def list_versions(self, session_id: str, doc_type: str) -> list[DocumentVersionRecord]:
        """版本列表，**按 `version_no` 降序**（最新的在最上面）。尚无版本 → `[]`。

        槽位不存在时会**建出空槽位**（与 `ensure_document` 同一条规则），
        所以"从没生成过"和"生成过但被清空"都稳定地回 `[]` 而不是 404。
        """
        slot = self.ensure_document(session_id, doc_type)
        with self._repository.connection() as conn:
            rows = self._repository.list_versions(conn, slot.id)
        return [DocumentVersionRecord(**row) for row in rows]

    def get_version(
        self, session_id: str, doc_type: str, version_id: str
    ) -> DocumentVersionRecord:
        """单版详情。版本不存在、或**不属于这个槽位** → 404。

        两种失败刻意**不区分**：对调用方而言这是同一个 URL 上取不到这个版本，
        而区分开（"id 存在但属于别的会话"）等于泄露别的会话有没有这个 id。
        """
        slot = self.ensure_document(session_id, doc_type)
        with self._repository.connection() as conn:
            row = self._repository.get_version(conn, version_id)
        if row is None or str(row["document_id"]) != slot.id:
            raise DocumentNotFound(f"版本不存在：{version_id}")
        return DocumentVersionRecord(**row)

    def get_current_version(
        self, session_id: str, doc_type: str
    ) -> DocumentVersionRecord | None:
        """当前生效的版本；尚无版本 → `None`（**不是 404**：没有版本是合法状态）。"""
        slot = self.ensure_document(session_id, doc_type)
        with self._repository.connection() as conn:
            row = self._repository.get_current_version(conn, slot.id)
        return DocumentVersionRecord(**row) if row else None

    # ------------------------------------------------------------ 写版本

    def create_initial_version(
        self,
        session_id: str,
        doc_type: str,
        content: str,
        *,
        source_kind: VersionSourceKind = VersionSourceKind.GENERATE,
        source_job_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> DocumentVersionRecord:
        """首版：`version_no=1`、`parent_version_id=None`、设为 current。

        语义上是"这个槽位的第一版"（无父版本）。已经有版本时调用它**不会报错**，
        但会插出一个 `parent_version_id=None` 的新版 —— 那是调用方的时序 bug，
        正常路径应由 `sync_from_job` 分流到 `append_generated_version`。
        """
        slot = self.ensure_document(session_id, doc_type)
        with self._repository.connection() as conn:
            return self._append(
                conn,
                slot,
                content=content,
                source_kind=source_kind,
                source_job_id=source_job_id,
                parent_version_id=None,
                metadata=metadata,
            )

    def append_generated_version(
        self,
        session_id: str,
        doc_type: str,
        content: str,
        *,
        source_kind: VersionSourceKind = VersionSourceKind.GENERATE,
        source_job_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> DocumentVersionRecord:
        """整份重新生成：**升号、切 current，旧版原样保留**。

        `parent_version_id` 指向切换前的 current —— 版本链因此能回答"这一版是从哪一版
        长出来的"（restore 出来的那一版也一样，它指向当时的 current 而不是被恢复的那一版；
        "恢复自哪一版"记在 `metadata.restored_from_version_*` 里，两者不冲突）。
        """
        slot = self.ensure_document(session_id, doc_type)
        with self._repository.connection() as conn:
            current = self._repository.get_current_version(conn, slot.id)
            return self._append(
                conn,
                slot,
                content=content,
                source_kind=source_kind,
                source_job_id=source_job_id,
                parent_version_id=str(current["id"]) if current else None,
                metadata=metadata,
            )

    def update_current_content(
        self,
        session_id: str,
        doc_type: str,
        content: str,
        *,
        metadata: Mapping[str, Any] | None = None,
    ) -> DocumentVersionRecord:
        """**就地**更新 current 版本的正文与 metadata，`version_no` **不变**。

        用于"优化 Job 完成"与"Session 自动保存"：它们改的是同一版稿子，不是新的一版。
        升号会让侧栏每敲一次键盘就多出一个历史版本。

        ⚠️ 只改 `content` / `metadata_json`：`source_kind`、`source_job_id`、`created_at`
        **一概不动** —— 这一版"出生于"某次生成这件事不会因为一次优化而改变（需求 §4 的
        写入规则只点了这两个字段）。

        没有 current 版本时抛 `DocumentNotFound`：这是调用方的时序 bug
        （应先 `create_initial_version`），而不是用户输入问题，所以不返 400。
        """
        slot = self.ensure_document(session_id, doc_type)
        if not content.strip():
            raise InvalidDocumentRequest("无内容可保存：正文是空的")
        with self._repository.connection() as conn:
            current = self._repository.get_current_version(conn, slot.id)
            if current is None:
                raise DocumentNotFound(
                    f"槽位还没有 current 版本，无法就地更新"
                    f"（session={session_id} doc_type={doc_type}；应先建首版）"
                )
            merged = merge_metadata(load_metadata(current["metadata_json"]), metadata)
            self._repository.update_version_content(
                conn,
                str(current["id"]),
                content=content,
                metadata_json=dumps_metadata(merged),
            )
            self._repository.touch_document(conn, slot.id, updated_at=now_iso())
            row = self._repository.get_version(conn, str(current["id"]))
        if row is None:  # pragma: no cover - 刚更新过的行读不到只能是并发删了
            raise DocumentNotFound(f"版本刚更新就读不到：{current['id']}")
        record = DocumentVersionRecord(**row)
        self._log_sync(doc_type, "update_current", record)
        return record

    def checkpoint(
        self, session_id: str, doc_type: str, content: str | None = None
    ) -> DocumentVersionRecord:
        """用户「保存为新版本」：把当前正文固化成新的一版并切 current。

        1. `ensure_document`；
        2. 取 current；`content` 不传就用 current 的正文（**前端没给全文时也一样能用**）；
        3. 新 `version_no = max + 1`；`parent_version_id` = 旧 current；
        4. INSERT 新版本 → UPDATE `current_version_id`（**同一事务**）；
        5. 返回新版本。

        ⚠️ **完全不继承 metadata**：新版没有 review、也没有备注（需求 §7 的 review 规则表：
        checkpoint 不继承）。用户备注通过 `update_version_note` **事后**写到任意一版上 ——
        checkpoint 时不让填备注是刻意的：那时候用户还没法引用「第几版」。
        （restore 是另一回事：它合并历史 metadata，因为它复制的正是被审过的那份正文 ——
        见模块 docstring 里"需求自相矛盾"那一节。）
        """
        slot = self.ensure_document(session_id, doc_type)
        with self._repository.connection() as conn:
            current = self._repository.get_current_version(conn, slot.id)
            resolved = content if content is not None else (
                str(current["content"]) if current else None
            )
            if resolved is None or not resolved.strip():
                raise InvalidDocumentRequest(
                    "无内容可保存：该槽位还没有 current 版本，且请求体没有带 content"
                )
            return self._append(
                conn,
                slot,
                content=resolved,
                source_kind=VersionSourceKind.CHECKPOINT,
                source_job_id=None,
                parent_version_id=str(current["id"]) if current else None,
                metadata=None,
            )

    def restore_version(
        self, session_id: str, doc_type: str, version_id: str
    ) -> DocumentVersionRecord:
        """用户「恢复此版本」：以历史版正文**新建下一版**并设为 current。

        **不修改、不删除任何历史行** —— 恢复是"往前走一步"，不是"回退指针"。
        所以 current=v2 时恢复 v1 会得到 v3（正文同 v1），而 v1、v2 仍原样在列表里。
        这正是"历史只追加"的价值：恢复之后用户还可能反悔再恢复回 v2。

        校验：版本必须属于本槽位（否则 404）、不能恢复 current 本身、历史版必须有正文（否则 400）。
        metadata 合并历史版（含 review）并记 `restored_from_version_*`，但**丢掉 `change_note`**
        （见模块 docstring 的说明）。
        """
        slot = self.ensure_document(session_id, doc_type)
        with self._repository.connection() as conn:
            target = self._repository.get_version(conn, version_id)
            if target is None or str(target["document_id"]) != slot.id:
                raise DocumentNotFound(f"版本不存在：{version_id}")
            current = self._repository.get_current_version(conn, slot.id)
            if current is not None and str(current["id"]) == str(target["id"]):
                raise InvalidDocumentRequest(
                    f"v{target['version_no']} 就是当前版本，无需恢复"
                )
            body = str(target["content"] or "")
            if not body.strip():
                raise InvalidDocumentRequest(
                    f"v{target['version_no']} 没有正文，恢复它只会得到一个空版本"
                )
            metadata = merge_metadata(load_metadata(target["metadata_json"]), None)
            metadata.pop("change_note", None)
            metadata["restored_from_version_id"] = str(target["id"])
            metadata["restored_from_version_no"] = int(target["version_no"])
            return self._append(
                conn,
                slot,
                content=body,
                source_kind=VersionSourceKind.RESTORE,
                source_job_id=None,
                parent_version_id=str(current["id"]) if current else None,
                metadata=metadata,
            )

    def update_version_note(
        self,
        session_id: str,
        doc_type: str,
        version_id: str,
        change_note: str | None,
    ) -> DocumentVersionRecord:
        """给**任意**一版（含历史版与 current）写用户备注 → `metadata.change_note`。

        **不碰正文、不升 `version_no`**（需求 §6.6）。传 `None` 或空串 = 清空备注
        （键会被删掉，而不是留一个空串 —— 前端于是可以统一用"有没有这个键"判断）。

        ⚠️ 刻意**不推进槽位的 `updated_at`**：备注是"用户对某一版说的话"，
        不是"这份文档的当前内容变了"。推进它会让侧栏的最近更新时间在只写了一句备注后跳动，
        连带把"最后改的是正文"这个信号稀释掉。
        """
        slot = self.ensure_document(session_id, doc_type)
        with self._repository.connection() as conn:
            row = self._repository.get_version(conn, version_id)
            if row is None or str(row["document_id"]) != slot.id:
                raise DocumentNotFound(f"版本不存在：{version_id}")
            note = (change_note or "").strip()
            merged = merge_metadata(
                load_metadata(row["metadata_json"]),
                # 空串 → None → `merge_metadata` 删掉这个键
                {"change_note": note or None},
            )
            self._repository.update_version_metadata(
                conn, str(row["id"]), metadata_json=dumps_metadata(merged)
            )
            updated = self._repository.get_version(conn, str(row["id"]))
        if updated is None:  # pragma: no cover - 刚更新过的行读不到只能是并发删了
            raise DocumentNotFound(f"版本刚更新就读不到：{version_id}")
        return DocumentVersionRecord(**updated)

    # ------------------------------------------------------------ Job 写入入口（02 篇接线用）

    def sync_from_job(
        self,
        *,
        session_id: str,
        artifact: str,
        content: str,
        job_id: str | None = None,
        review: Mapping[str, Any] | None = None,
        run_summary: Mapping[str, Any] | None = None,
        job_status: str | None = None,
    ) -> DocumentVersionRecord:
        """Job 收尾时把产物落成一个版本 —— `job_runner` 调的就是这个方法。

        本方法只负责**选规则**，落库全部复用上面那几个方法（规则只有一份）：

        | Job | 已有 current？ | 走哪条 | `version_no` |
        | --- | --- | --- | --- |
        | 整份生成 | 否 | `create_initial_version` | 1 |
        | 整份生成 | 是 | `append_generated_version` | +1 |
        | 优化 | 否 | `create_initial_version`（`source_kind=optimize`） | 1 |
        | 优化 | 是 | `update_current_content` | **不变** |

        ⚠️ **只收 `artifact`，不收 `doc_type` / `source_kind`**：那三个值（`doc_type`、
        `source_kind`、以及走哪条规则）全部由 `artifact` 一个入参推导出来，调用方不可能
        传出一个自相矛盾的组合（例如 `doc_type="prd"` 配 `source_kind="generate"` 却是
        `optimize-prd` 任务）。需求 §2 那张 `ARTIFACT_TO_DOC_TYPE` 表就是
        `job_models.content_artifact_of`，本层不另抄一份。

        `review` **只在 PRD 的整份生成时写入**（需求 §7）：优化不重新审稿，
        不带 `review` 就不会覆盖旧值（`merge_metadata` 是浅合并），
        界面上的"审核通过 / 还有 N 条意见"因此不会凭空消失。

        `job_status="failed"` 用于"任务失败但留下了半成品"那条路径（需求 §3）：
        版本照写（用户刷新后能看到写到哪了），但在 metadata 里标明这一版是残的。
        """
        doc_type = self._require_doc_type(content_artifact_of(artifact))
        slot = self.ensure_document(session_id, doc_type)

        metadata: dict[str, Any] = {}
        if run_summary:
            metadata["run_summary"] = dict(run_summary)
        if job_status:
            metadata["job_status"] = job_status
        optimize = is_optimize_artifact(artifact)
        if review and doc_type == "prd" and not optimize:
            metadata["review"] = dict(review)

        with self._repository.connection() as conn:
            has_current = self._repository.get_current_version(conn, slot.id) is not None

        if has_current:
            if optimize:
                return self.update_current_content(
                    session_id, doc_type, content, metadata=metadata
                )
            return self.append_generated_version(
                session_id, doc_type, content, source_job_id=job_id, metadata=metadata
            )
        return self.create_initial_version(
            session_id,
            doc_type,
            content,
            source_kind=VersionSourceKind.OPTIMIZE if optimize else VersionSourceKind.GENERATE,
            source_job_id=job_id,
            metadata=metadata,
        )

    # ------------------------------------------------------------ Session 镜像 ↔ 版本层

    def maybe_migrate_from_session(
        self, session_id: str, contents: Mapping[str, str]
    ) -> list[DocumentVersionRecord]:
        """老 Session 的**一次性**迁移：把镜像里的正文导入成 v1（`source_kind=import`）。

        需求 §5：对三种 `doc_type`，**若镜像有正文、且这个槽位还没有 current 版本**，
        就 `create_initial_version(..., source_kind=import)`。已经有版本的一律跳过 ——
        "只迁移一次"就落在这一条判断上，不需要另记一个"迁移过了"的标记
        （那种标记会与版本表本身不同步，而且没法解释"用户把版本删光了"的情况）。

        空 / 全空白的正文不算内容（§4：空字符串不创建版本）。
        返回**本次真正导入**的那些版本（空列表 = 没什么可迁的）。
        """
        migrated: list[DocumentVersionRecord] = []
        names: list[str] = []
        for doc_type in DOC_TYPES:
            content = contents.get(doc_type, "")
            if not content.strip():
                continue
            if self.get_current_version(session_id, doc_type) is not None:
                continue
            record = self.create_initial_version(
                session_id, doc_type, content, source_kind=VersionSourceKind.IMPORT
            )
            migrated.append(record)
            names.append(f"{doc_type}=v{record.version_no}")
        if migrated:
            logger.info(
                "老会话 %s 的正文已导入文档版本层：%s（source_kind=import）",
                session_id,
                "、".join(names),
            )
        return migrated

    def sync_session_contents(
        self,
        session_id: str,
        contents: Mapping[str, str],
        *,
        source_kind: VersionSourceKind = VersionSourceKind.AUTO_SAVE,
    ) -> list[DocumentVersionRecord]:
        """把镜像里的**手改**正文同步进 current 版本（需求 §4）。

        对每个有正文的 `doc_type`：

        | 情况 | 动作 | `version_no` |
        | --- | --- | --- |
        | 还没有 current 版本 | `create_initial_version(source_kind)` | 1 |
        | current 正文与镜像**已经一致** | 什么都不做 | 不变 |
        | 不一致 | `update_current_content` | **不变** |

        ⚠️ **绝不 checkpoint**：前端是防抖保存（800ms、最长 2s），每次 save 都升号会让侧栏
        每敲几个字就多出一版。需求 §4 对此有明确要求（"不要每次 save 都调用 checkpoint"）。
        "已经一致就跳过"是常态路径 —— 优化的自动保存、以及任务收尾后来一次防抖保存，
        走的都是它，于是不会白写一次库。

        ⚠️ 空字符串**既不创建版本、也不删除已有版本**（本阶段不支持清空文档）。
        """
        written: list[DocumentVersionRecord] = []
        for doc_type in DOC_TYPES:
            content = contents.get(doc_type, "")
            if not content.strip():
                continue
            current = self.get_current_version(session_id, doc_type)
            if current is None:
                written.append(
                    self.create_initial_version(
                        session_id, doc_type, content, source_kind=source_kind
                    )
                )
                continue
            if content == current.content:
                # 常态：镜像与 current 一致（防抖保存很频繁，这一步省下的写库不是小数）
                continue
            # metadata 不传：就地更新**保留**这一版出生时的 run_summary / review
            written.append(self.update_current_content(session_id, doc_type, content))
        return written

    # ------------------------------------------------------------ 内部：插一版并切 current

    def _append(
        self,
        conn: sqlite3.Connection,
        slot: DocumentSlot,
        *,
        content: str,
        source_kind: VersionSourceKind,
        source_job_id: str | None,
        parent_version_id: str | None,
        metadata: Mapping[str, Any] | None,
    ) -> DocumentVersionRecord:
        """**所有"升号"路径的唯一出口**：算号 → INSERT 版本 → 切 current → 读回。

        集中在这一处的好处：`version_no` 的算法（`max + 1`）、"版本号必须与插入在同一个
        事务里算"、以及"空正文不许入链"这条底线都只需要写一次。
        任何新加的"升号"来源（例如 `import`）都自动继承这些性质。

        收的是**槽位**而不是 `document_id`：日志那一行要 `session_id` 与 `doc_type`
        （需求 §十），而它们就在槽位上 —— 比让每个调用方各传一遍可靠。
        """
        if not content.strip():
            raise InvalidDocumentRequest("无内容可保存：正文是空的")
        timestamp = now_iso()
        version_no = self._repository.max_version_no(conn, slot.id) + 1
        version_id = uuid.uuid4().hex
        metadata_json = dumps_metadata(dict(metadata or {}))
        self._repository.insert_version(
            conn,
            {
                "id": version_id,
                "document_id": slot.id,
                "version_no": version_no,
                "content": content,
                "source_kind": source_kind.value
                if isinstance(source_kind, VersionSourceKind)
                else str(source_kind),
                "source_job_id": source_job_id,
                "parent_version_id": parent_version_id,
                "metadata_json": metadata_json,
                "created_at": timestamp,
            },
        )
        self._repository.set_current_version(
            conn, slot.id, version_id, updated_at=timestamp
        )
        row = self._repository.get_version(conn, version_id)
        if row is None:  # pragma: no cover - 刚插进去就读不到只能是并发删了
            raise DocumentNotFound(f"版本刚插入就读不到：{version_id}")
        record = DocumentVersionRecord(**row)
        # 动作名从**版本号**推：v1 就是"建首版"，其余都是"追加新版"。
        # 比让每个调用方各传一个 `action` 字符串可靠 —— 那种参数迟早有一处写错。
        self._log_sync(slot, record, "create_initial" if version_no == 1 else "append")
        return record

    # ------------------------------------------------------------ 一行 JSON 的同步日志

    @staticmethod
    def _log_sync(slot: DocumentSlot, record: DocumentVersionRecord, action: str) -> None:
        """打一行 `event=document_sync`（需求 §十）—— 对照 Job id 与版本变化用。

        **debug 级**：正常运维不需要它，而排查"版本没对上"时它是第一手材料。
        （需求 §十 说"实现期可打临时日志，合并前删除或降为 debug"，这里直接就是终态。）

        ⚠️ 为什么是**整行 JSON** 而不是 `logger.debug(..., extra={...})`：
        `core/logging.py` 的格式串只有 `%(message)s`，`extra` 里的键**根本不会被打印**
        —— 那样写等于没写日志、还看不出问题。`services/llm_metrics.py` 是同一个口径
        （`logger.info(json.dumps(payload, ensure_ascii=False))`）。
        """
        if not logger.isEnabledFor(logging.DEBUG):
            return  # 省掉一次 dumps：这条路径每次保存都会被调到
        logger.debug(
            json.dumps(
                {
                    "event": "document_sync",
                    "session_id": slot.session_id,
                    "document_id": slot.id,
                    "doc_type": slot.doc_type,
                    "action": action,
                    "version_no": record.version_no,
                    "source_kind": record.source_kind.value,
                    "job_id": record.source_job_id,
                },
                ensure_ascii=False,
            )
        )
