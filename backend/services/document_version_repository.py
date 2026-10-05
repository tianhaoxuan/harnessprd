"""`documents` / `document_versions` 两张表的**仓储层**：一组 SQL，标准库 `sqlite3`，不引 ORM。

## 与 `plan_repository` / `job_repository` 的关系

三组表（`plans`、`generation_jobs`、`documents` + `document_versions`）在**同一个库文件**里
（`settings.plans_db_path`），连接与三个 PRAGMA 统一走 `plan_repository.open_connection()` ——
一份实现。各写一份的话，"连接参数悄悄不一致"不会有任何报错，表现是偶发的
`database is locked`（见 `plan_repository.open_connection` 的说明）。

## ⚠️ 本模块替代了需求原文里的 `db/database.py` + `init_db()`

本仓库的建表约定是「**每张表一个仓储模块 + `ensure_schema()`**，由 service 的
`ensure_ready()` 在 `main.py` 的 lifespan 里调用」—— `plan_repository` 与 `job_repository`
都是这个形状。另起一个 `db/` 包会变成**第三套**建表与连接实现，改 PRAGMA 要改两处，
而漏改一处不会有任何报错。所以这里跟随既有约定，功能与 `init_db()` 等价：
`ensure_schema()` 幂等，启动时无条件调用。

## 事务边界在 service，不在本层

本模块的读写都**接收一个 `conn`**，由调用方用 `connection()` 把
「插入新版本 + 切换 `current_version_id` + 推进 `updated_at`」包在**同一个事务**里 ——
这是需求点名的要求（checkpoint / restore / update_current 必须同事务）。
仓储只管 SQL，不管边界。

## ⚠️ 不碰 `PRAGMA user_version`

那是 **`plans` 表的库级版本号**（`plan_repository.SCHEMA_VERSION`，当前 1）。
在这里再写一个自己的版本号，两边会互相覆盖，且只会在某次真实迁移时暴露
（`job_repository` 的模块 docstring 有完整说明）。两张新表只做 `CREATE TABLE IF NOT EXISTS`。

## 枚举取值为什么定义在本模块

`doc_type` 与 `source_kind` 的允许取值**就是本文件里两条 CHECK 约束的取值**，
所以清单和建表语句放在一起、由 service 反向 import。这与 `job_repository` 把 CHECK 写在
建表语句里是同一个道理，区别只在于这里让**仓储当唯一来源**，而不是在 models 模块里再抄一份
（`plan_models` / `job_models` 那种做法需要 `smoke_check.py` 再补一条"两处是否一致"的断言；
这里一个来源就不需要那条断言了）。
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from enum import Enum
from pathlib import Path
from typing import Any, Literal

from services.plan_repository import dumps_snapshot, open_connection

logger = logging.getLogger(__name__)

DOCUMENTS_TABLE = "documents"
VERSIONS_TABLE = "document_versions"

DocType = Literal["prd", "api-docs", "prompts"]
"""槽位类型。**三种，且顺序固定**（列表接口按这个顺序回三种槽位）。

⚠️ 与内部 `DocKind`（`prd` / `api` / `prompts`）**不是一回事**：对外的产物名是需求给定的
`api-docs`，内部视图键是 `api`。两者之间的映射在 `services/job_models.ARTIFACT_DOC_KIND`
里定义一次，不要在这里再写一张表。
"""

DOC_TYPES: tuple[DocType, ...] = ("prd", "api-docs", "prompts")
"""全部合法 `doc_type`。**顺序也是 `_CREATE_DOCUMENTS` 里 CHECK 的书写顺序**。"""


class VersionSourceKind(str, Enum):
    """一个版本是**怎么来的**。取值即 `document_versions.source_kind` 的 CHECK 取值。

    | 取值 | 行为 |
    | --- | --- |
    | `generate` | 首次或整份重新生成 Job 完成 → **升** `version_no`，切 current |
    | `optimize` | 优化 Job 完成 → **只改 current 正文**，不升号 |
    | `auto_save` | Session save / debounce 同步 current → 不升号 |
    | `checkpoint` | 用户「保存为新版本」 → 升号，切 current |
    | `restore` | 用户「恢复此版本」 → 基于历史正文升新版 |
    | `import` | 老数据迁移导入 |

    ⚠️ **`optimize` / `auto_save` 不会留在任何一行上**：它们只 UPDATE current 那一行，
    而那一行的 `source_kind` 保持它**出生时**的取值（见 service 的
    `update_current_content`）。这是需求定死的——"仅 UPDATE current 行的 content 与
    metadata_json，version_no 不变"。所以这张表里看不到"最后一次是自动保存"，
    要那个信息得看 `updated_at`。
    """

    GENERATE = "generate"
    OPTIMIZE = "optimize"
    AUTO_SAVE = "auto_save"
    CHECKPOINT = "checkpoint"
    RESTORE = "restore"
    IMPORT = "import"


VERSION_SOURCE_KINDS: tuple[str, ...] = tuple(kind.value for kind in VersionSourceKind)
"""全部合法 `source_kind`（= CHECK 的取值清单）。顺序即建表语句里的书写顺序。"""

_CREATE_DOCUMENTS = f"""
CREATE TABLE IF NOT EXISTS {DOCUMENTS_TABLE} (
    id                 TEXT PRIMARY KEY,
    session_id         TEXT NOT NULL,
    doc_type           TEXT NOT NULL
                       CHECK (doc_type IN ('prd', 'api-docs', 'prompts')),
    current_version_id TEXT,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL,
    UNIQUE(session_id, doc_type)
)
"""

_CREATE_VERSIONS = f"""
CREATE TABLE IF NOT EXISTS {VERSIONS_TABLE} (
    id                TEXT PRIMARY KEY,
    document_id       TEXT NOT NULL,
    version_no        INTEGER NOT NULL,
    content           TEXT NOT NULL DEFAULT '',
    source_kind       TEXT NOT NULL
                      CHECK (source_kind IN ('generate', 'optimize', 'auto_save',
                                             'checkpoint', 'restore', 'import')),
    source_job_id     TEXT,
    parent_version_id TEXT,
    metadata_json     TEXT,
    created_at        TEXT NOT NULL,
    UNIQUE(document_id, version_no)
)
"""

_CREATE_INDEXES = (
    f"CREATE INDEX IF NOT EXISTS idx_{DOCUMENTS_TABLE}_session"
    f" ON {DOCUMENTS_TABLE}(session_id)",
    f"CREATE INDEX IF NOT EXISTS idx_{VERSIONS_TABLE}_document"
    f" ON {VERSIONS_TABLE}(document_id)",
)

DOCUMENT_COLUMNS: tuple[str, ...] = (
    "id",
    "session_id",
    "doc_type",
    "current_version_id",
    "created_at",
    "updated_at",
)
"""`documents` 的列清单（`SELECT` 显式列出，不用 `SELECT *`：将来加列时不会把意外的东西带进 API）。"""

VERSION_COLUMNS: tuple[str, ...] = (
    "id",
    "document_id",
    "version_no",
    "content",
    "source_kind",
    "source_job_id",
    "parent_version_id",
    "metadata_json",
    "created_at",
)

# 槽位查询统一带 `current_version_no`（LEFT JOIN 取），所以三种读路径（按会话列、取单个、
# 建完再读）返回的**形状完全一致** —— `DocumentSlot(**row)` 到处都能直接构造。
# 分开写两套 SELECT 的话，"某个入口少带一列"会让模型校验失败，而报错点在离原因很远的地方。
_SELECT_DOCUMENT = f"""
SELECT d.{', d.'.join(DOCUMENT_COLUMNS)}, v.version_no AS current_version_no
FROM {DOCUMENTS_TABLE} d
LEFT JOIN {VERSIONS_TABLE} v ON v.id = d.current_version_id
"""


class DocumentVersionRepository:
    """两个槽位表与版本表的读写。**只做持久化，不做业务判断**（那是 `document_version_service`）。"""

    def __init__(self, db_path: Path, *, busy_timeout_ms: int = 5000) -> None:
        self._path = Path(db_path)
        self._busy_timeout_ms = max(0, int(busy_timeout_ms))

    @property
    def path(self) -> Path:
        return self._path

    # ---------------------------------------------------------------- 建表

    def ensure_schema(self) -> None:
        """建两张表 + 两条索引。**幂等**，可以在每次启动时无条件调用。"""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as conn:
            conn.execute(_CREATE_DOCUMENTS)
            conn.execute(_CREATE_VERSIONS)
            for statement in _CREATE_INDEXES:
                conn.execute(statement)
        logger.info(
            "文档槽位与版本表就绪：%s（tables=%s, %s）",
            self._path,
            DOCUMENTS_TABLE,
            VERSIONS_TABLE,
        )

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        """开一条连接（转调模块级 `open_connection`，PRAGMA 只有一份实现）。

        **一次 `with` = 一个事务**：调用方把"插版本 + 切 current + 推进 updated_at"
        全放在同一个 `with` 里，就是需求要的原子性。
        """
        with open_connection(self._path, busy_timeout_ms=self._busy_timeout_ms) as conn:
            yield conn

    def table_sql(self, table: str) -> str:
        """`sqlite_master` 里那张表的建表语句（自检用：断言 CHECK 与枚举一致、表真的建出来了）。"""
        with self.connection() as conn:
            row = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
            ).fetchone()
        return str(row[0]) if row else ""

    # ---------------------------------------------------------------- documents 写

    @staticmethod
    def insert_document(conn: sqlite3.Connection, row: Mapping[str, Any]) -> None:
        """插入一个槽位。字段缺一个就报错（**不做隐式兜底** —— 缺字段是调用方的 bug）。"""
        missing = [name for name in DOCUMENT_COLUMNS if name not in row]
        if missing:
            raise ValueError(f"插入文档槽位时缺字段：{missing}")
        placeholders = ", ".join("?" for _ in DOCUMENT_COLUMNS)
        conn.execute(
            f"INSERT INTO {DOCUMENTS_TABLE} ({', '.join(DOCUMENT_COLUMNS)})"
            f" VALUES ({placeholders})",
            tuple(row[name] for name in DOCUMENT_COLUMNS),
        )

    @staticmethod
    def insert_document_or_ignore(conn: sqlite3.Connection, row: Mapping[str, Any]) -> bool:
        """同上，但撞上 `UNIQUE(session_id, doc_type)` 时**静默跳过**，返回是否真的插入。

        "没有就建一个"在并发下必然会撞：两个请求同时发现槽位不存在、同时插入。
        靠 `INSERT OR IGNORE` + 事后重读，比"先查后插再 catch IntegrityError"少一段
        容易写错的异常处理（而且 catch 那种写法会连**别的**约束冲突一起吞掉）。
        """
        missing = [name for name in DOCUMENT_COLUMNS if name not in row]
        if missing:
            raise ValueError(f"插入文档槽位时缺字段：{missing}")
        placeholders = ", ".join("?" for _ in DOCUMENT_COLUMNS)
        cursor = conn.execute(
            f"INSERT OR IGNORE INTO {DOCUMENTS_TABLE} ({', '.join(DOCUMENT_COLUMNS)})"
            f" VALUES ({placeholders})",
            tuple(row[name] for name in DOCUMENT_COLUMNS),
        )
        return cursor.rowcount > 0

    @staticmethod
    def set_current_version(
        conn: sqlite3.Connection, document_id: str, version_id: str, *, updated_at: str
    ) -> None:
        """把 current 指向某个版本，并推进槽位的 `updated_at`（两件事必须一起发生）。"""
        conn.execute(
            f"UPDATE {DOCUMENTS_TABLE} SET current_version_id = ?, updated_at = ? WHERE id = ?",
            (version_id, updated_at, document_id),
        )

    @staticmethod
    def touch_document(conn: sqlite3.Connection, document_id: str, *, updated_at: str) -> None:
        """只推进 `updated_at`（改 current 正文、改备注时用；current 不变）。"""
        conn.execute(
            f"UPDATE {DOCUMENTS_TABLE} SET updated_at = ? WHERE id = ?",
            (updated_at, document_id),
        )

    # ---------------------------------------------------------------- documents 读

    @staticmethod
    def _to_dict(row: Any) -> dict[str, Any]:
        return dict(row)

    def get_document(
        self, conn: sqlite3.Connection, session_id: str, doc_type: str
    ) -> dict[str, Any] | None:
        row = conn.execute(
            f"{_SELECT_DOCUMENT} WHERE d.session_id = ? AND d.doc_type = ?",
            (session_id, doc_type),
        ).fetchone()
        return self._to_dict(row) if row else None

    def list_documents(self, conn: sqlite3.Connection, session_id: str) -> list[dict[str, Any]]:
        """这个会话下的所有槽位（**不过滤 doc_type**：调用方要按 `DOC_TYPES` 补齐三种）。"""
        rows = conn.execute(
            f"{_SELECT_DOCUMENT} WHERE d.session_id = ?",
            (session_id,),
        ).fetchall()
        return [self._to_dict(row) for row in rows]

    def count_documents(self) -> int:
        with self.connection() as conn:
            return int(conn.execute(f"SELECT COUNT(*) FROM {DOCUMENTS_TABLE}").fetchone()[0])

    def count_versions(self) -> int:
        with self.connection() as conn:
            return int(conn.execute(f"SELECT COUNT(*) FROM {VERSIONS_TABLE}").fetchone()[0])

    # ---------------------------------------------------------------- document_versions 写

    @staticmethod
    def insert_version(conn: sqlite3.Connection, row: Mapping[str, Any]) -> None:
        """插入一个版本快照。

        **历史行只 INSERT，不 UPDATE 正文** —— 这是整个数据模型的核心原则
        （需求开篇："历史版本只追加、不可原地修改"）。唯一允许被改的是 current 那一行的
        `content` / `metadata_json`（`update_current_content` / `update_version_metadata`），
        以及任意一行的 `metadata.change_note`（用户备注）。
        同一 `(document_id, version_no)` 重复插入由表的 UNIQUE 兜底。
        """
        missing = [name for name in VERSION_COLUMNS if name not in row]
        if missing:
            raise ValueError(f"插入文档版本时缺字段：{missing}")
        placeholders = ", ".join("?" for _ in VERSION_COLUMNS)
        conn.execute(
            f"INSERT INTO {VERSIONS_TABLE} ({', '.join(VERSION_COLUMNS)})"
            f" VALUES ({placeholders})",
            tuple(row[name] for name in VERSION_COLUMNS),
        )

    @staticmethod
    def update_version_content(
        conn: sqlite3.Connection,
        version_id: str,
        *,
        content: str,
        metadata_json: str | None,
    ) -> None:
        """只改正文与 metadata（**不碰 `version_no` / `source_kind` / 时间戳**）。"""
        conn.execute(
            f"UPDATE {VERSIONS_TABLE} SET content = ?, metadata_json = ? WHERE id = ?",
            (content, metadata_json, version_id),
        )

    @staticmethod
    def update_version_metadata(
        conn: sqlite3.Connection, version_id: str, *, metadata_json: str | None
    ) -> None:
        """只改 metadata（用户备注走这条；**正文与 `version_no` 一动不动**）。"""
        conn.execute(
            f"UPDATE {VERSIONS_TABLE} SET metadata_json = ? WHERE id = ?",
            (metadata_json, version_id),
        )

    # ---------------------------------------------------------------- document_versions 读

    def get_version(
        self, conn: sqlite3.Connection, version_id: str
    ) -> dict[str, Any] | None:
        row = conn.execute(
            f"SELECT {', '.join(VERSION_COLUMNS)} FROM {VERSIONS_TABLE} WHERE id = ?",
            (version_id,),
        ).fetchone()
        return self._to_dict(row) if row else None

    def get_current_version(
        self, conn: sqlite3.Connection, document_id: str
    ) -> dict[str, Any] | None:
        """槽位当前生效的那个版本（`documents.current_version_id` 指向的行）。"""
        row = conn.execute(
            f"SELECT v.{', v.'.join(VERSION_COLUMNS)} FROM {VERSIONS_TABLE} v"
            f" JOIN {DOCUMENTS_TABLE} d ON d.current_version_id = v.id"
            " WHERE d.id = ?",
            (document_id,),
        ).fetchone()
        return self._to_dict(row) if row else None

    def list_versions(
        self, conn: sqlite3.Connection, document_id: str
    ) -> list[dict[str, Any]]:
        """版本列表，**按 `version_no` 降序**（最新的在最上面，供侧栏直接用）。"""
        rows = conn.execute(
            f"SELECT {', '.join(VERSION_COLUMNS)} FROM {VERSIONS_TABLE}"
            " WHERE document_id = ? ORDER BY version_no DESC",
            (document_id,),
        ).fetchall()
        return [self._to_dict(row) for row in rows]

    @staticmethod
    def max_version_no(conn: sqlite3.Connection, document_id: str) -> int:
        """当前最大版本号；没有版本时返回 `0`。

        ⚠️ 必须与"插入 + 切 current"在**同一个事务**里读：分开两次连接的话，两个并发的
        checkpoint 会算出一模一样的 `version_no`，第二个插入撞 UNIQUE 报错
        （错误信息是 `UNIQUE constraint failed`，离"并发"这个原因很远）。
        """
        row = conn.execute(
            f"SELECT COALESCE(MAX(version_no), 0) FROM {VERSIONS_TABLE} WHERE document_id = ?",
            (document_id,),
        ).fetchone()
        return int(row[0]) if row else 0

    def version_numbers(self, conn: sqlite3.Connection, document_id: str) -> list[int]:
        """这个槽位已有的全部版本号（升序）。自检用。"""
        rows = conn.execute(
            f"SELECT version_no FROM {VERSIONS_TABLE} WHERE document_id = ?"
            " ORDER BY version_no ASC",
            (document_id,),
        ).fetchall()
        return [int(row[0]) for row in rows]


def dumps_metadata(value: Mapping[str, Any] | None) -> str | None:
    """把 metadata 序列化成紧凑 JSON 文本；空字典 → `None`（库里不存 `"{}"`）。

    与 `plan_repository.dumps_snapshot` 同口径（`ensure_ascii=False` 让中文备注在
    `sqlite3` 命令行里直接可读、紧凑以免一份 metadata 占几倍体积）。
    """
    if not value:
        return None
    return dumps_snapshot(value)


def load_metadata(metadata_json: str | None) -> dict[str, Any]:
    """解析 `metadata_json`。空 / 坏数据返回 `{}` 而不是抛 —— 坏数据不该让查询接口 500。"""
    if not metadata_json:
        return {}
    try:
        parsed = json.loads(metadata_json)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}
