"""Generation Job 的 **Service 层**：任务 CRUD 与业务规则，SQL 一行没有。

分层：`api/jobs.py` → **本文件** → `job_repository`（只懂 SQL）。
与仓库其它 service 一致：不 import fastapi、不 import LLM 模块。

## 本层负责的三条规则

| 规则 | 为什么在这一层 |
| --- | --- |
| **同会话 + 同产物只允许一个在跑的任务**（`DuplicateRunningJob` → 409） | 后端不去重的话，用户连点两次「生成」就会有两个 runner 同时写同一份产物，最终落库的是**后写完的那一份**（而不是任何一份完整的），而且两者都往会话同步正文 —— 现象是"生成结果时好时坏"。锁是唯一防线 |
| **payload 只保留该产物允许的键**，其余丢弃并记 warning | payload 是要落库的原样输入。多传的键（比如给 PRD 传 `prd_content`）说明调用方对形状理解有偏差，静默存下来只会让排查更难 |
| **草稿落库的节流判断**（`should_persist`） | 每个 token 一次事务会让 SQLite 每秒写几百次；节流策略是业务决定（500ms），不该散落在 runner 里 |

## 时间戳与状态流转

时间戳统一走 `plan_service.now_iso()`（与方案表同一个口径：UTC、毫秒、字典序即时间序）。
状态流转是**单向**的（见 `job_models.JobStatus`），本层只提供 `update_job` 这个
"改哪些列就写哪些列"的入口，不替 runner 决定该走到哪个状态 ——
那是 runner 的编排逻辑，放在这里会让"任务跑到哪一步"有两个来源。
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from core.config import Settings, get_settings
from services.job_models import (
    ARTIFACT_CONFLICT_GROUP,
    ARTIFACT_DOC_KIND,
    ARTIFACT_INITIAL_PHASE,
    ARTIFACT_LABEL,
    JOB_ARTIFACTS,
    PAYLOAD_ALLOWED_KEYS,
    JobRecord,
    JobSnapshot,
    TERMINAL_STATUSES,
)
from services.job_repository import JobRepository
from services.plan_service import now_iso

logger = logging.getLogger(__name__)

DRAFT_PERSIST_INTERVAL_SECONDS = 0.5
"""草稿落库的节流间隔（需求建议 500ms）。

为什么必须有：一次生成会推几百到上千个 chunk，每个 chunk 一次 UPDATE 就是
每秒上百次事务（SQLite 的写锁是**库级**的，还会顺带卡住方案列表的读）。
节流之后最坏情况丢 0.5 秒的增量 —— 而**权威全文在 `done` 时必然落一次**，
所以丢的只是"刷新时看到的最后一个半句"，不是产物本身。
"""


class JobNotFound(LookupError):
    """任务不存在。继承 `LookupError`：HTTP 层据此返 **404**（与 `SessionNotFound` 同口径）。"""


class DuplicateRunningJob(RuntimeError):
    """与这个会话里**正在跑**的任务冲突。HTTP 层据此返 **409**。

    ⚠️ 冲突的对方**不一定是同一个 artifact**：`prd` 与 `optimize-prd` 写的是同一个字段，
    也在同一冲突组里（见 `job_models.ARTIFACT_CONFLICT_GROUP`）。所以文案要把
    "谁在挡着"说清楚，否则用户按"同产物"去理解，会以为是自己点重了。
    """

    def __init__(
        self,
        session_id: str,
        artifact: str,
        running_id: str,
        running_artifact: str | None = None,
    ) -> None:
        blocker = running_artifact or artifact
        same = blocker == artifact
        super().__init__(
            f"该会话的{ARTIFACT_LABEL.get(blocker, blocker)}任务已在运行（job={running_id}）；"
            + (
                "等它结束，或先看它的进度再决定要不要重来"
                if same
                else "同一份文档不能同时生成与优化 —— 等它结束再试"
            )
        )
        self.session_id = session_id
        self.artifact = artifact
        self.running_id = running_id
        self.running_artifact = blocker


def should_persist(last_persist_at: float, now: float) -> bool:
    """距上次落库是否已超过节流间隔。**纯函数**，便于自检直接断言（不用跑生成）。"""
    return (now - last_persist_at) >= DRAFT_PERSIST_INTERVAL_SECONDS


class JobService:
    """生成任务的业务入口。"""

    def __init__(
        self,
        settings: Settings | None = None,
        repository: JobRepository | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._repository = repository or JobRepository(
            self._settings.plans_db_path,
            busy_timeout_ms=self._settings.sqlite_busy_timeout_ms,
        )

    @property
    def repository(self) -> JobRepository:
        return self._repository

    @property
    def db_path(self) -> Path:
        return self._repository.path

    def ensure_ready(self) -> None:
        """建表（幂等）。应用启动时调用 —— 见 `main.py` 的 lifespan。"""
        self._repository.ensure_schema()

    # ---------------------------------------------------------------- 创建

    def create_job(self, session_id: str, artifact: str, payload: Mapping[str, Any]) -> str:
        """新建任务，返回 `job_id`。**与在跑的任务冲突时抛 `DuplicateRunningJob`（HTTP 409）。**

        新任务落库时 `status=pending`、`phase` 按产物取初值（PRD 生成是 `writing`，
        其余一路 `generating`）—— runner 起来后立刻置 `running`。

        `pending` / `running` 两种状态**都算"在跑"**：任务创建与 runner 被调度之间
        有一个极短的窗口，连点两下正好落在这个窗口里时，只拦 `running` 是拦不住的。

        ## 冲突判定按**冲突组**，不是按 artifact 字符串

        见 `job_models.ARTIFACT_CONFLICT_GROUP`：`prd` 与 `optimize-prd` 写的是同一个字段
        （`documents.prd.content`），不能同时跑 —— 只比字符串会放它们一起进来，
        于是两份输出互相覆盖，而用户看到的是"优化没生效"或"生成结果丢了"。
        不同文档之间（`prd` 与 `api-docs`）**不冲突**，各写各的字段，并行安全。
        """
        self._check_artifact(artifact)
        group = ARTIFACT_CONFLICT_GROUP[artifact]
        for row in self._repository.list_running(session_id):
            running_artifact = str(row["artifact"])
            if running_artifact in group:
                raise DuplicateRunningJob(session_id, artifact, str(row["id"]), running_artifact)

        job_id = uuid.uuid4().hex
        stamp = now_iso()
        self._repository.insert(
            {
                "id": job_id,
                "session_id": session_id,
                "artifact": artifact,
                "status": "pending",
                "phase": ARTIFACT_INITIAL_PHASE[artifact],
                "payload_json": _dump_payload(artifact, payload),
                "draft_content": "",
                "previous_draft": "",
                "review_json": None,
                "result_json": None,
                "error": None,
                "created_at": stamp,
                "updated_at": stamp,
            }
        )
        logger.info(
            "创建生成任务：job=%s session=%s artifact=%s", job_id, session_id, artifact
        )
        return job_id

    # ---------------------------------------------------------------- 读

    def get_job(self, job_id: str) -> JobRecord:
        """取任务。不存在 → `JobNotFound`（HTTP 404）。"""
        row = self._repository.get(job_id)
        if row is None:
            raise JobNotFound(f"生成任务不存在：{job_id}")
        return JobRecord(**row)

    def get_snapshot(self, job_id: str) -> JobSnapshot:
        """对外快照（少解析一次 JSON 的那一层）。"""
        return JobSnapshot.from_record(self.get_job(job_id))

    def get_running_job(self, session_id: str, artifact: str) -> JobRecord | None:
        row = self._repository.get_running(session_id, artifact)
        return JobRecord(**row) if row else None

    def list_running(self) -> list[JobRecord]:
        return [JobRecord(**row) for row in self._repository.list_by_status(("pending", "running"))]

    def list_running_for_session(self, session_id: str) -> list[JobRecord]:
        """这个会话下**正在跑**的任务（**带 `artifact`**，不只 id）。

        与 `running_ids_for_sessions` 的差别就是那两个字段：会话层在同步文档版本时要知道
        "正在生成的是**哪一份**产物"（那一份的半成品不许被固化成版本），
        而只拿到一串 id 是答不出来的。判重用的 `list_running(session_id)` 在仓储上，
        这里把同一份结果包成 `JobRecord` 返回，免得调用方直接伸手进仓储。
        """
        return [JobRecord(**row) for row in self._repository.list_running(session_id)]

    def running_ids_for_sessions(self, session_ids: Sequence[str]) -> set[str]:
        """给会话降级判断用：这些会话下还有哪些任务在跑（见 `session_service`）。"""
        return self._repository.running_ids_for_sessions(session_ids)

    def is_running(self, job_id: str) -> bool:
        """这个任务现在还在跑吗。**会话降级判断的唯一依据。**"""
        row = self._repository.get(job_id)
        return bool(row) and row["status"] not in TERMINAL_STATUSES

    # ---------------------------------------------------------------- 写

    def update_job(self, job_id: str, **fields: Any) -> None:
        """局部更新。**只写显式给出的列**，并总是推进 `updated_at`。

        JSON 列（`review_json` / `result_json`）由调用方自己序列化 ——
        本层不猜"这个 dict 是要存成哪一列"，那种猜测出错时是静默的。
        """
        self._repository.update(job_id, fields)

    def append_draft(self, job_id: str, chunk: str) -> str:
        """把一段增量追加进 `draft_content`，返回新的全文。

        ⚠️ 实现是"读-改-写"（先取当前草稿再拼），而不是 SQL 的 `draft_content = draft_content || ?`。
        理由：调用方需要**返回的最新全文**（发 `done` / 写会话都要它），而 SQL 拼接拿不回结果，
        还得再查一次 —— 两次往返之间还可能有别的写入者。
        单个任务的草稿**只有它的 runner 会写**，所以这里不存在丢更新。

        ⚠️ **runner 现在不调它**：runner 的任务是"每个 chunk 都要落库 + 广播"，
        逐次 `SELECT` 一遍全文太贵（一次生成上千个 chunk），所以它在内存里累加
        （`job_runner._DraftState`）、按 500ms 节流整份写回（`update_job`）。
        本方法留给"不持有内存状态"的调用方（例如将来从别处补写草稿的场景）。
        """
        record = self.get_job(job_id)
        return record.draft_content + chunk

    # ---------------------------------------------------------------- 服务重启

    def fail_stale_jobs(self, message: str = "服务重启，任务中断") -> list[str]:
        """把库里遗留的 `pending` / `running` 任务置为 `failed`（**启动时调用**）。

        `asyncio.create_task` 的任务活不过进程重启，而库里留着 `running` 会让界面
        一直显示"生成中"。V1 只如实标记中断、不自动续跑（需求 §十二）：
        草稿保留在 `draft_content` 里，用户看到的是"断在哪儿了"，不是"还在跑"。
        """
        ids = self._repository.mark_stale_running_as_failed(message)
        if ids:
            logger.warning("服务重启：%d 个未完成的生成任务已标记为 failed（%s）", len(ids), message)
        return ids

    # ---------------------------------------------------------------- 内部

    @staticmethod
    def _check_artifact(artifact: str) -> None:
        """枚举校验。**在服务层挡一次**（数据库的 CHECK 是第二道防线，不是第一道）。"""
        if artifact not in JOB_ARTIFACTS:
            raise ValueError(f"artifact 只能是 {'、'.join(JOB_ARTIFACTS)} 之一，收到 {artifact!r}")


def _dump_payload(artifact: str, payload: Mapping[str, Any]) -> str:
    """按产物过滤 payload 并序列化。

    - 白名单之外的键**丢弃**（记 warning）：说明调用方对形状理解有偏差，
      静默存下来会让"这条任务当时到底喂了什么"更难查；
    - `ensure_ascii=False` 与 `plan_repository.dumps_snapshot` 同口径：
      中文在库里应当可读。
    """
    allowed = PAYLOAD_ALLOWED_KEYS[artifact]
    kept = {key: payload[key] for key in allowed if key in payload}
    dropped = sorted(set(payload) - set(kept))
    if dropped:
        logger.warning(
            "创建 %s 任务时丢弃了 payload 里不认识的键：%s（允许的键：%s）",
            artifact,
            "、".join(dropped),
            "、".join(allowed),
        )
    return json.dumps(kept, ensure_ascii=False, separators=(",", ":"))


def artifact_doc_kind(artifact: str) -> str:
    """对外 `artifact` → 内部 `DocKind`。映射只有 `job_models` 那一份。"""
    return ARTIFACT_DOC_KIND[artifact]


__all__ = [
    "DRAFT_PERSIST_INTERVAL_SECONDS",
    "DuplicateRunningJob",
    "JobNotFound",
    "JobService",
    "artifact_doc_kind",
    "should_persist",
]
