"""`generation_jobs` 表的**仓储层**：一组 SQL，标准库 `sqlite3`，不引 ORM。

与 `plan_repository` 是**同一个库文件**（`settings.plans_db_path`）：一张表存方案快照，
一张表存生成任务。共用的连接与 PRAGMA 走 `plan_repository.open_connection()` —— 一份实现。

## 为什么不碰 `PRAGMA user_version`

`user_version` 是**库级**的，而 `plan_repository.ensure_schema()` 已经把它当作
"plans 表的版本"在用（当前 1）。这里再去写一个自己的版本号，两边就会互相覆盖：
`plans` 的迁移判断会读到 jobs 的版本，反之亦然 —— 而且不会有任何报错，
只会在某次真实迁移时把数据改坏。

所以本表只做 `CREATE TABLE IF NOT EXISTS`（幂等，无历史数据要搬）。
将来这张表真要迁移，正确做法是**两张表共用一个库级版本号**，由一处统一管理。

## 时间戳与排序

与 `plan_repository` 完全一致：`created_at / updated_at` 存 ISO8601 UTC（毫秒）字符串，
字典序 = 时间序。
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from services.plan_repository import open_connection
from services.plan_service import now_iso

logger = logging.getLogger(__name__)

TABLE_NAME = "generation_jobs"

# 三个枚举的允许取值写进 CHECK：数据库自己兜底。取值清单与 `services/job_models.py`
# 的 Literal **必须一致** —— `smoke_check.py` 有一条断言直接比对这两处。
_CREATE_TABLE = f"""
CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
    id             TEXT PRIMARY KEY,
    session_id     TEXT NOT NULL,
    artifact       TEXT NOT NULL
                   CHECK (artifact IN ('prd', 'api-docs', 'prompts',
                                       'optimize-prd', 'optimize-api-docs', 'optimize-prompts')),
    status         TEXT NOT NULL
                   CHECK (status IN ('pending', 'running', 'completed', 'failed', 'cancelled')),
    phase          TEXT NOT NULL
                   CHECK (phase IN ('writing', 'reviewing', 'rewriting', 'generating', 'done')),
    payload_json   TEXT NOT NULL,
    draft_content  TEXT NOT NULL DEFAULT '',
    previous_draft TEXT NOT NULL DEFAULT '',
    review_json    TEXT,
    result_json    TEXT,
    error          TEXT,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
)
"""

_LEGACY_TABLE = f"{TABLE_NAME}__legacy"
"""重建旧表时的临时表名（见 `_needs_artifact_migration` / `ensure_schema`）。"""

_LEGACY_ARTIFACT_CHECK = "optimize-prd"
"""旧版建表语句里**没有**这个取值 —— 用它判断"这张表要不要迁移"。

为什么需要迁移：SQLite **不能改 CHECK 约束**（没有 `ALTER TABLE … DROP CONSTRAINT`），
而 `CREATE TABLE IF NOT EXISTS` 对已存在的表什么都不做。于是"新增 optimize artifact"
这件事在**已有的库**上会表现为：插入优化任务时抛
`IntegrityError: CHECK constraint failed` —— 报错点在写库，离"表是旧版"这个原因很远。
所以这里显式重建（复制数据 → 删旧表 → 改名 → 重建索引）。"""

_CREATE_INDEXES = (
    # 需求点名要的那条：按会话查任务
    f"CREATE INDEX IF NOT EXISTS idx_{TABLE_NAME}_session ON {TABLE_NAME}(session_id)",
    # "同会话同产物是否已有 running 任务"是**每次创建任务都要查**的（防连点），
    # 它落在 (session_id, artifact, status) 上。没有这条索引时它退化成全表扫描，
    # 而这张表只增不减（每次生成一行）。
    f"CREATE INDEX IF NOT EXISTS idx_{TABLE_NAME}_running"
    f" ON {TABLE_NAME}(session_id, artifact, status)",
)

COLUMNS: tuple[str, ...] = (
    "id",
    "session_id",
    "artifact",
    "status",
    "phase",
    "payload_json",
    "draft_content",
    "previous_draft",
    "review_json",
    "result_json",
    "error",
    "created_at",
    "updated_at",
)
"""列清单（`SELECT` 显式列出，不用 `SELECT *`：将来加列时不会把意外的东西带进 API）。"""

# 允许被 `update()` 改的列。`id` / `session_id` / `artifact` / `created_at` **不在其中**：
# 它们是这条记录的身份，改了就不是同一个任务了。
UPDATABLE_COLUMNS: tuple[str, ...] = (
    "status",
    "phase",
    "draft_content",
    "previous_draft",
    "review_json",
    "result_json",
    "error",
)


class JobRepository:
    """生成任务的读写。**只做持久化，不做业务判断**（那是 `job_service` 的事）。"""

    def __init__(self, db_path: Path, *, busy_timeout_ms: int = 5000) -> None:
        self._path = Path(db_path)
        self._busy_timeout_ms = max(0, int(busy_timeout_ms))

    @property
    def path(self) -> Path:
        return self._path

    # ---------------------------------------------------------------- 建表

    def ensure_schema(self) -> None:
        """建表 + 迁移 + 建索引。**幂等**，可以在每次启动时无条件调用。

        刻意**不**碰 `PRAGMA user_version`（见模块 docstring：那是 plans 表的版本）。

        迁移只在"旧表的 CHECK 里没有 optimize 取值"时发生，而且是**复制数据**式的重建：
        库里可能正躺着一个在跑的任务（它的 `draft_content` 是用户等了几分钟换来的），
        所以不能 `DROP TABLE` 了事。
        """
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with open_connection(self._path, busy_timeout_ms=self._busy_timeout_ms) as conn:
            if self._needs_artifact_migration(conn):
                self._rebuild_for_optimize_artifacts(conn)
            conn.execute(_CREATE_TABLE)
            # 索引放在**迁移之后**建：重建过程会先删旧表（旧表上的同名索引随它一起消失），
            # 反过来写的话 `CREATE INDEX IF NOT EXISTS` 会命中旧表上的同名索引而跳过，
            # 等旧表一删，索引就没了 —— 而且不会有任何报错。
            for statement in _CREATE_INDEXES:
                conn.execute(statement)
        logger.info("生成任务表就绪：%s（table=%s）", self._path, TABLE_NAME)

    @staticmethod
    def _needs_artifact_migration(conn: sqlite3.Connection) -> bool:
        """已有表的建表语句里是否缺少 optimize 取值（表不存在时返回 `False`）。"""
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (TABLE_NAME,)
        ).fetchone()
        if row is None or not row[0]:
            return False
        return _LEGACY_ARTIFACT_CHECK not in str(row[0])

    @staticmethod
    def _rebuild_for_optimize_artifacts(conn: sqlite3.Connection) -> None:
        """把旧表换成带新 CHECK 的表，**数据一行不丢**。"""
        conn.execute(f"ALTER TABLE {TABLE_NAME} RENAME TO {_LEGACY_TABLE}")
        conn.execute(_CREATE_TABLE)
        conn.execute(
            f"INSERT INTO {TABLE_NAME} ({', '.join(COLUMNS)})"
            f" SELECT {', '.join(COLUMNS)} FROM {_LEGACY_TABLE}"
        )
        conn.execute(f"DROP TABLE {_LEGACY_TABLE}")
        logger.warning(
            "生成任务表已迁移：artifact 的 CHECK 增加 optimize-* 取值（旧数据已原样复制）"
        )

    # ---------------------------------------------------------------- 写

    def insert(self, row: Mapping[str, Any]) -> None:
        """插入一行。字段缺一个就报错（**不做隐式兜底** —— 缺字段是调用方的 bug）。"""
        missing = [name for name in COLUMNS if name not in row]
        if missing:
            raise ValueError(f"插入生成任务时缺字段：{missing}")
        placeholders = ", ".join("?" for _ in COLUMNS)
        with open_connection(self._path, busy_timeout_ms=self._busy_timeout_ms) as conn:
            conn.execute(
                f"INSERT INTO {TABLE_NAME} ({', '.join(COLUMNS)}) VALUES ({placeholders})",
                tuple(row[name] for name in COLUMNS),
            )

    def update(self, job_id: str, changes: Mapping[str, Any], *, updated_at: str | None = None) -> bool:
        """局部更新；返回是否命中一行。

        - 不在 `UPDATABLE_COLUMNS` 里的键**直接忽略**（不是报错）：调用方多传一个
          内部字段不该让整个 runner 崩掉，但也不能悄悄改到身份列。
        - `updated_at` 由调用方给或就地取（时间戳属于业务层的事，与 `plan_repository` 同口径）。
        """
        fields = [name for name in changes if name in UPDATABLE_COLUMNS]
        assignments = ", ".join(f"{name} = ?" for name in fields + ["updated_at"])
        values = [changes[name] for name in fields] + [updated_at or now_iso(), job_id]
        with open_connection(self._path, busy_timeout_ms=self._busy_timeout_ms) as conn:
            cursor = conn.execute(
                f"UPDATE {TABLE_NAME} SET {assignments} WHERE id = ?", tuple(values)
            )
            return cursor.rowcount > 0

    def mark_stale_running_as_failed(self, message: str) -> list[str]:
        """把库里所有 `running` / `pending` 的任务置为 `failed`，返回被改动的 id 列表。

        **服务重启时调用**（`main.py` 的 lifespan）：单进程 `asyncio.create_task` 的任务
        在重启后必然已经消失，而库里留着 `running` 会让前端永远显示"生成中"
        （`会话持久化方案` §7.4 的孤儿生成态）。V1 不自动续跑，只如实标记中断。
        """
        stale = self.list_by_status(("pending", "running"))
        if not stale:
            return []
        stamp = now_iso()
        ids = [str(row["id"]) for row in stale]
        with open_connection(self._path, busy_timeout_ms=self._busy_timeout_ms) as conn:
            conn.execute(
                f"UPDATE {TABLE_NAME} SET status = 'failed', error = ?, updated_at = ?"
                " WHERE status IN ('pending', 'running')",
                (message, stamp),
            )
        return ids

    # ---------------------------------------------------------------- 读

    @staticmethod
    def _to_dict(row: Any) -> dict[str, Any]:
        return dict(row)

    def get(self, job_id: str) -> dict[str, Any] | None:
        with open_connection(self._path, busy_timeout_ms=self._busy_timeout_ms) as conn:
            row = conn.execute(
                f"SELECT {', '.join(COLUMNS)} FROM {TABLE_NAME} WHERE id = ?", (job_id,)
            ).fetchone()
        return self._to_dict(row) if row else None

    def get_running(self, session_id: str, artifact: str) -> dict[str, Any] | None:
        """同会话 + 同产物**正在跑**的那个任务（没有则 `None`）。

        "正在跑" = `status IN ('pending','running')` —— `pending` 也算：任务刚创建、
        runner 还没被调度起来的那一瞬间，第二次点击必须一样被拦住，
        否则连点两下就是两个任务同时写同一份产物。
        """
        with open_connection(self._path, busy_timeout_ms=self._busy_timeout_ms) as conn:
            row = conn.execute(
                f"SELECT {', '.join(COLUMNS)} FROM {TABLE_NAME}"
                " WHERE session_id = ? AND artifact = ? AND status IN ('pending', 'running')"
                " ORDER BY created_at DESC, rowid DESC LIMIT 1",
                (session_id, artifact),
            ).fetchone()
        return self._to_dict(row) if row else None

    def list_running(self, session_id: str) -> list[dict[str, Any]]:
        """这个会话下**所有**在跑的任务（不限产物）。

        ⚠️ 判重不能只按 artifact 精确匹配：`prd` 与 `optimize-prd` 写的是同一个字段
        （`documents.prd.content`），谁后写完谁赢。冲突组在 `job_service` 里判，
        这里只负责把"现在有哪些在跑"如实取出来（一趟查询，别做成 N 次）。
        """
        with open_connection(self._path, busy_timeout_ms=self._busy_timeout_ms) as conn:
            rows = conn.execute(
                f"SELECT {', '.join(COLUMNS)} FROM {TABLE_NAME}"
                " WHERE session_id = ? AND status IN ('pending', 'running')"
                " ORDER BY created_at ASC, rowid ASC",
                (session_id,),
            ).fetchall()
        return [self._to_dict(row) for row in rows]

    def list_by_status(self, statuses: Sequence[str]) -> list[dict[str, Any]]:
        if not statuses:
            return []
        placeholders = ", ".join("?" for _ in statuses)
        with open_connection(self._path, busy_timeout_ms=self._busy_timeout_ms) as conn:
            rows = conn.execute(
                f"SELECT {', '.join(COLUMNS)} FROM {TABLE_NAME}"
                f" WHERE status IN ({placeholders}) ORDER BY created_at ASC, rowid ASC",
                tuple(statuses),
            ).fetchall()
        return [self._to_dict(row) for row in rows]

    def running_ids_for_sessions(self, session_ids: Iterable[str]) -> set[str]:
        """这些会话下所有"正在跑"的任务 id（**给会话降级判断用**）。

        会话层只需要知道"这个 job 还在跑吗"，一次查完比逐个 job 查省事
        （`GET /api/session/{id}` 每次都调它，别让它变成 N 次查询）。
        """
        ids = [str(item) for item in session_ids if item]
        if not ids:
            return set()
        placeholders = ", ".join("?" for _ in ids)
        with open_connection(self._path, busy_timeout_ms=self._busy_timeout_ms) as conn:
            rows = conn.execute(
                f"SELECT id FROM {TABLE_NAME}"
                f" WHERE session_id IN ({placeholders}) AND status IN ('pending', 'running')",
                tuple(ids),
            ).fetchall()
        return {str(row["id"]) for row in rows}

    def count(self) -> int:
        with open_connection(self._path, busy_timeout_ms=self._busy_timeout_ms) as conn:
            return int(conn.execute(f"SELECT COUNT(*) FROM {TABLE_NAME}").fetchone()[0])

    def table_sql(self) -> str:
        """`sqlite_master` 里那张表的建表语句（自检用：断言 CHECK 与枚举一致）。"""
        with open_connection(self._path, busy_timeout_ms=self._busy_timeout_ms) as conn:
            row = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (TABLE_NAME,)
            ).fetchone()
        return str(row[0]) if row else ""
