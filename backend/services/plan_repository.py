"""方案的**仓储层**：一张表 + 一组 SQL，标准库 `sqlite3`，不引 ORM。

## 为什么用标准库而不是 SQLAlchemy

这一层只有一张表、五种操作。ORM 带来的映射与迁移能力这里用不上，却要多一个依赖、
多一层"对象与行不同步"的排查面。stdblib 的 `sqlite3` 足够，而且**方言就是 SQLite 本身**
（将来真要换 Postgres 时，改动点集中在本文件里，反而好换）。

## 连接策略：**每次操作开一条新连接**

SQLite 的连接**不能跨线程共享**（`sqlite3` 默认会直接抛 `ProgrammingError`）。FastAPI 的
同步路由跑在线程池里，所以"模块级单连接"这种写法在这里迟早炸。每次操作新开一条连接是
最不容易出错的方案 —— SQLite 打开本地文件的成本是微秒级，这个量级（单机、单进程、
方案列表）根本不需要连接池。

并发写靠 `PRAGMA busy_timeout` 等待，而不是自己写重试循环；`journal_mode=WAL` 让读不阻塞写。

## 时间与排序

`created_at / updated_at` 存 **ISO8601 UTC（毫秒精度）字符串**，因为 SQLite 没有原生日期类型。
同一个格式下**字典序 = 时间序**，所以 `ORDER BY updated_at DESC` 直接可用，不需要转换。
（毫秒而不是秒：同一秒内建两条时，秒精度会让它们完全并列 —— 见 `plan_service.now_iso`。）
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

TABLE_NAME = "plans"
"""表名：一条记录就是一份"方案"，表名用复数（与 `sessions` 那类资源的命名习惯一致）。"""

SCHEMA_VERSION = 1
"""`PRAGMA user_version` 的值。改了表结构就 +1，并在 `ensure_schema()` 里加迁移分支。"""

# 三个枚举的允许取值写进 CHECK：**数据库自己兜底**，不指望只有服务层会写这张表。
# ⚠️ 与 `services/plan_models.py` 的 Literal **必须一致** —— `scripts/smoke_check.py`
# 有一条断言直接比对这两处，改一处忘另一处会被当场抓住。
_CREATE_TABLE = f"""
CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
    id            TEXT    PRIMARY KEY,
    title         TEXT    NOT NULL,
    entry_mode    TEXT    NOT NULL
                  CHECK (entry_mode IN ('structured', 'prd-shortcut', 'prompts-debug')),
    current_stage TEXT    NOT NULL
                  CHECK (current_stage IN ('form', 'chatting', 'generating-prd', 'review-prd',
                                           'generating-api-docs', 'review-api-docs',
                                           'generating-prompts', 'review-prompts', 'done')),
    status        TEXT    NOT NULL
                  CHECK (status IN ('draft', 'active', 'archived', 'failed')),
    snapshot      TEXT    NOT NULL,
    created_at    TEXT    NOT NULL,
    updated_at    TEXT    NOT NULL
)
"""

_CREATE_INDEXES = (
    # 列表默认按最近更新倒序 —— 这是唯一必建的索引
    f"CREATE INDEX IF NOT EXISTS idx_{TABLE_NAME}_updated_at ON {TABLE_NAME} (updated_at DESC)",
    # 按入口/状态过滤是列表页最常见的筛选组合
    f"CREATE INDEX IF NOT EXISTS idx_{TABLE_NAME}_entry_status"
    f" ON {TABLE_NAME} (entry_mode, status)",
)

COLUMNS: tuple[str, ...] = (
    "id",
    "title",
    "entry_mode",
    "current_stage",
    "status",
    "snapshot",
    "created_at",
    "updated_at",
)
"""列清单（`SELECT` 显式列出，不用 `SELECT *`：将来加列时不会把意外的东西带进 API）。"""

_SUMMARY_COLUMNS = ("id", "title", "entry_mode", "current_stage", "status", "created_at", "updated_at")


class PlanSchemaError(RuntimeError):
    """库文件的表结构比本代码期新（或不是本项目建的）。

    刻意**抛错而不是自动改**：这种不一致必须有人看一眼，静默迁移会把数据改坏。
    """


class PlanStorageError(RuntimeError):
    """库文件打不开（目录不存在、不可写、磁盘满、路径其实是个目录……）。

    单独一类而不是让它冒 `sqlite3.OperationalError`：那个报错只说
    "unable to open database file"，**不带路径** —— 而排查时第一件想知道的事就是路径。
    """

    def __init__(self, path: Path, cause: Exception) -> None:
        super().__init__(
            f"打不开方案库 {path}：{cause}。"
            "检查这个目录是否存在且可写（SQLite 还要在同目录建 `-wal`/`-shm` 两个附属文件）。"
        )
        self.path = path


class PlanRepository:
    """方案的读写。**只做持久化，不做业务判断**（那是 `plan_service` 的事）。"""

    def __init__(self, db_path: Path, *, busy_timeout_ms: int = 5000) -> None:
        self._path = Path(db_path)
        self._busy_timeout_ms = max(0, int(busy_timeout_ms))

    @property
    def path(self) -> Path:
        return self._path

    # ---------------------------------------------------------------- 建表

    def ensure_schema(self) -> None:
        """建表 + 建索引 + 版本校验。**幂等**：可以在每次启动时无条件调用。"""
        # 目录不存在时先建：默认路径在 `backend/` 下（已存在），但测试会指到临时目录
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            if version > SCHEMA_VERSION:
                raise PlanSchemaError(
                    f"{self._path} 的 schema 版本是 {version}，本代码只认到 {SCHEMA_VERSION}；"
                    "这个库文件比当前代码新，请用对应版本的代码打开（或换一个库文件）。"
                )
            conn.execute(_CREATE_TABLE)
            for statement in _CREATE_INDEXES:
                conn.execute(statement)
            if version < SCHEMA_VERSION:
                # v0 → v1 就是"建这张表"，没有历史数据要搬
                conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        logger.info("方案库就绪：%s（table=%s, schema_version=%d）", self._path, TABLE_NAME, SCHEMA_VERSION)

    # ---------------------------------------------------------------- 写

    def insert(self, row: Mapping[str, Any]) -> None:
        """插入一行。字段缺一个就报错（**不做隐式兜底** —— 缺字段是调用方的 bug）。"""
        missing = [name for name in COLUMNS if name not in row]
        if missing:
            raise ValueError(f"插入方案时缺字段：{missing}")
        placeholders = ", ".join("?" for _ in COLUMNS)
        with self._connect() as conn:
            conn.execute(
                f"INSERT INTO {TABLE_NAME} ({', '.join(COLUMNS)}) VALUES ({placeholders})",
                tuple(row[name] for name in COLUMNS),
            )

    def update(self, plan_id: str, changes: Mapping[str, Any], *, updated_at: str) -> bool:
        """局部更新；返回是否命中一行。`updated_at` 由调用方给（时间戳属于业务层的事）。"""
        fields = [name for name in changes if name in COLUMNS and name not in ("id", "created_at")]
        if not fields:
            # 没有任何字段要改：仍然要推进 updated_at（"用户点了保存"这件事本身有意义），
            # 否则界面上会出现"改了但时间没变"，看起来像没保存成功。
            fields = []
        assignments = ", ".join(f"{name} = ?" for name in fields + ["updated_at"])
        values = [changes[name] for name in fields] + [updated_at, plan_id]
        with self._connect() as conn:
            cursor = conn.execute(
                f"UPDATE {TABLE_NAME} SET {assignments} WHERE id = ?", tuple(values)
            )
            return cursor.rowcount > 0

    def delete(self, plan_id: str) -> bool:
        with self._connect() as conn:
            cursor = conn.execute(f"DELETE FROM {TABLE_NAME} WHERE id = ?", (plan_id,))
            return cursor.rowcount > 0

    # ---------------------------------------------------------------- 读

    def list_summaries(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        entry_mode: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        """列表页数据。**不读 `snapshot`** —— 这是把摘要单独成列的全部意义。"""
        where: list[str] = []
        params: list[Any] = []
        if entry_mode is not None:
            where.append("entry_mode = ?")
            params.append(entry_mode)
        if status is not None:
            where.append("status = ?")
            params.append(status)
        clause = f" WHERE {' AND '.join(where)}" if where else ""
        # 兜底用 `rowid DESC`（后插入的在前），**不是** `id ASC`：id 是随机十六进制，
        # 拿它兜底会让"同一毫秒写入的两条"顺序看起来随机。rowid 随插入单调递增且稳定。
        sql = (
            f"SELECT {', '.join(_SUMMARY_COLUMNS)} FROM {TABLE_NAME}{clause}"
            " ORDER BY updated_at DESC, rowid DESC LIMIT ? OFFSET ?"
        )
        with self._connect() as conn:
            rows = conn.execute(sql, (*params, limit, offset)).fetchall()
        return [dict(row) for row in rows]

    def get(self, plan_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT {', '.join(COLUMNS)} FROM {TABLE_NAME} WHERE id = ?", (plan_id,)
            ).fetchone()
        return dict(row) if row else None

    def count(self) -> int:
        with self._connect() as conn:
            return int(conn.execute(f"SELECT COUNT(*) FROM {TABLE_NAME}").fetchone()[0])

    def table_sql(self) -> str:
        """`sqlite_master` 里那张表的建表语句（自检用：断言 CHECK 与枚举一致）。"""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (TABLE_NAME,)
            ).fetchone()
        return str(row[0]) if row else ""

    # ---------------------------------------------------------------- 连接

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """开一条连接，用完提交/回滚并关闭。

        三个 PRAGMA 各有理由：
        - `journal_mode=WAL`：读不阻塞写（列表页读的时候后台还能写）
        - `busy_timeout`：写锁冲突时**等**而不是立刻抛 `database is locked`
        - `foreign_keys=ON`：SQLite 默认是关的，将来加外键时不会踩空
        """
        try:
            conn = sqlite3.connect(str(self._path), timeout=self._busy_timeout_ms / 1000)
        except sqlite3.Error as exc:
            raise PlanStorageError(self._path, exc) from exc
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute(f"PRAGMA busy_timeout = {self._busy_timeout_ms}")
            conn.execute("PRAGMA foreign_keys = ON")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def dumps_snapshot(value: Mapping[str, Any] | Sequence[Any] | str) -> str:
    """把快照序列化成**紧凑的 JSON 对象文本**（`ensure_ascii=False`）。

    `ensure_ascii=False` 是刻意的：中文产品名在库里应当可读（`sqlite3` 命令行里直接看得到），
    转义成 `\\uXXXX` 只会让排查变难。紧凑（无缩进）是为了不让一份 state 占几倍的体积。
    """
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
