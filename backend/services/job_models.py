"""Generation Job 的领域模型与枚举。

## 什么是 Generation Job

把「生成 PRD / 接口文档 / 提示词套件」从**绑在浏览器 SSE 连接上的前台补全**
（`api/conversation.py` 的 4 个 `*-stream` 接口）改成**后台独立任务**：

- 任务状态与草稿存 SQLite（`generation_jobs` 表）；
- 执行在后台协程里跑（`services/job_runner.py`）；
- 进度通过进程内广播（`services/job_bus.py`）推给**当前订阅中**的客户端；
- 客户端断开 SSE **不**取消任务 —— `document_service` 的生成器照常跑完、照常落库。

这是 `docs/会话持久化方案.md` §7.1「生成必须是后台任务」的落地，也是
`api/conversation.py` 模块 docstring 里那句"只能当进度/预览通道"的收尾。

## 枚举取值为什么写在这里（并且被数据库 CHECK 再写一遍）

与 `plan_models.py` 同一套理由：`api/schemas.py` 复用这里的 Literal，
数据库的 CHECK 是**第二道防线**（不指望只有服务层会写这张表）。
`scripts/smoke_check.py` 有一条断言直接比对"表的 CHECK"与"这里的元组"，改一处忘另一处会被当场抓住。

## `artifact` 与内部 `DocKind` 不是一回事

对外的 `artifact` 取值是需求给定的 `prd | api-docs | prompts`，而内部
（`services/state.py` 的 `DocKind`）是 `prd | api | prompts` —— **`api-docs` ↔ `api`**。
映射只在本模块的 `ARTIFACT_DOC_KIND` 里定义一次：两边各写一张表必然漂移。

## 优化任务（`optimize-*`）与生成任务共用同一张表与同一条流水线

`optimize-prd` / `optimize-api-docs` / `optimize-prompts` 是**按用户指令重写某一节**
（F8.6），复用同一个 `generation_jobs` 表、同一个 `job_bus`、同一个 runner 骨架与
同一个 SSE 协议。与生成任务的**三处**差别（实现时最容易搞错的地方）：

| | 生成任务 | 优化任务 |
| --- | --- | --- |
| 会话 `viewState` | 跑时切 `generating-*`，收尾落 `review-*` | **全程保持 `review-*`**（用户就停在审阅页） |
| 界面阶段 | 有 Stepper（起草 / 审查 / 修订） | 没有阶段，只有一行"正在重写这一节…" |
| `draft_content` 的语义 | 产物**全文**的增量 | 模型这次输出的**那一节**（拼接由收尾时做，见 `section_edit`） |

第三条是刻意的：优化时模型只回一节，而界面与快照要的都是**整篇**。
把它拼回全文（`replace_section`）需要"优化前的整篇"，那件事发生在收尾
（`job_runner._finish_optimize`），所以草稿列先存原文、`result_json` 存拼好的全文。
"""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, Field

from services.state import DocKind

GenerateArtifact = Literal["prd", "api-docs", "prompts"]
"""**整篇生成**的三种产物。与前端产物卡片的三个入口一一对应。"""

OptimizeArtifact = Literal["optimize-prd", "optimize-api-docs", "optimize-prompts"]
"""**按指令优化一节**的三种产物（F8.6）。一对一是刻意的：优化哪一份就写哪一份。"""

JobArtifact = Literal[
    "prd", "api-docs", "prompts", "optimize-prd", "optimize-api-docs", "optimize-prompts"
]
"""用户在界面上点的动作：整篇生成，或按指令优化某一节。"""

JobStatus = Literal["pending", "running", "completed", "failed", "cancelled"]
"""任务状态机（单向，除了 `pending → running` 之外都只从 `running` 出发）：

    pending ──runner 启动──> running ──┬─> completed
                                       ├─> failed
                                       └─> cancelled

`cancelled` 目前没有取消入口（需求里没要），保留它是为了**服务重启后的收尾**有处可去：
启动时扫描到的 `running` 任务按需求置为 `failed`（"服务重启，任务中断"），
而将来真要加"用户取消"时不必再改 CHECK。
"""

JobPhase = Literal["writing", "reviewing", "rewriting", "generating", "done"]
"""任务**当前在哪个阶段**（比 status 细一层，给界面显示"在写 / 在审 / 在改"）。

- `prd`：`writing` → `reviewing` →（有问题则）`rewriting` → … → `done`
- `api-docs` / `prompts`：创建即 `generating`，没有审查环节，完成时 `done`
"""

JOB_ARTIFACTS: tuple[JobArtifact, ...] = (
    "prd",
    "api-docs",
    "prompts",
    "optimize-prd",
    "optimize-api-docs",
    "optimize-prompts",
)
"""全部合法 `artifact`。**顺序也是 CHECK 的书写顺序**（`smoke_check.py` 逐字比对）。"""

GENERATE_ARTIFACTS: tuple[GenerateArtifact, ...] = ("prd", "api-docs", "prompts")
OPTIMIZE_ARTIFACTS: tuple[OptimizeArtifact, ...] = (
    "optimize-prd",
    "optimize-api-docs",
    "optimize-prompts",
)
JOB_STATUSES: tuple[JobStatus, ...] = ("pending", "running", "completed", "failed", "cancelled")
JOB_PHASES: tuple[JobPhase, ...] = ("writing", "reviewing", "rewriting", "generating", "done")

TERMINAL_STATUSES: tuple[JobStatus, ...] = ("completed", "failed", "cancelled")
"""终态。到了这里 runner 不会再动这个任务，SSE 订阅者也该收流了。"""

OPTIMIZE_PREFIX = "optimize-"
"""优化 artifact 的前缀。**只在这一处判断"是不是优化任务"**（`is_optimize_artifact`）。"""


def is_optimize_artifact(artifact: str) -> bool:
    """这个 artifact 是不是"按指令优化一节"。"""
    return artifact.startswith(OPTIMIZE_PREFIX)


def content_artifact_of(artifact: str) -> str:
    """优化 artifact → 它对应的**生成** artifact（`optimize-prd` → `prd`）。

    生成 artifact 原样返回。用途：查冲突组、取视图、找产物 kind —— 三个地方都要
    "这条任务动的是哪一份文档"，各写一次 `removeprefix` 迟早漏一处。
    """
    if is_optimize_artifact(artifact):
        return artifact[len(OPTIMIZE_PREFIX) :]
    return artifact


ARTIFACT_DOC_KIND: dict[str, DocKind] = {
    "prd": "prd",
    "api-docs": "api",
    "prompts": "prompts",
    "optimize-prd": "prd",
    "optimize-api-docs": "api",
    "optimize-prompts": "prompts",
}
"""对外 `artifact` → 内部 `DocKind`（`api-docs` ↔ `api`）。**唯一的映射处。**

优化 artifact 也在这里（它动的是同一份文档），所以 `docKindFor` 对六种取值都成立。
"""

ARTIFACT_GENERATING_VIEW: dict[str, str] = {
    "prd": "generating-prd",
    "api-docs": "generating-api-docs",
    "prompts": "generating-prompts",
}
"""**生成**任务在跑时，会话快照的 `viewState` 取哪个值（`plan_models.PlanStage` 的合法取值）。

⚠️ 优化任务**不用**这张表 —— 它全程停在 `review-*`（用户就在审阅页上，
切到 `generating-*` 会让人以为整篇在重生成）。
"""

ARTIFACT_REVIEW_VIEW: dict[str, str] = {
    "prd": "review-prd",
    "api-docs": "review-api-docs",
    "prompts": "review-prompts",
    "optimize-prd": "review-prd",
    "optimize-api-docs": "review-api-docs",
    "optimize-prompts": "review-prompts",
}
"""任务收尾（完成或失败）后落回的审核视图。优化任务**跑的时候**也停在这里。"""

ARTIFACT_INITIAL_PHASE: dict[str, JobPhase] = {
    "prd": "writing",
    "api-docs": "generating",
    "prompts": "generating",
    # 优化是单段流（没有写 / 审 / 改），与接口文档单次生成同形
    "optimize-prd": "generating",
    "optimize-api-docs": "generating",
    "optimize-prompts": "generating",
}
"""创建时的初始 phase：只有 PRD 生成有"起草 / 审查 / 改写"三段，其余一路 `generating`。"""

ARTIFACT_RUN_TYPE: dict[str, str] = {
    "prd": "generate_prd_from_summary",
    "api-docs": "generate_api_docs",
    "prompts": "generate_prompts",
    # ⚠️ 与 `api/conversation.py` 的 optimize 路由**逐字一致**：同一个动作换个 run_type，
    # "按类型统计优化耗时"这件事就会在两个入口之间对不上。
    "optimize-prd": "optimize_document",
    "optimize-api-docs": "optimize_document",
    "optimize-prompts": "optimize_document",
}
"""观测口径里的 `run_type`。与 `api/conversation.py` 里同一条链路的取值**逐字一致** ——
换个名字会让"按 run_type 统计各产物耗时"这件事在两个入口之间对不上。"""

ARTIFACT_LABEL: dict[str, str] = {
    "prd": "PRD",
    "api-docs": "接口文档",
    "prompts": "提示词套件",
    "optimize-prd": "PRD 优化",
    "optimize-api-docs": "接口文档优化",
    "optimize-prompts": "提示词套件优化",
}
"""给人看的名字（409 的提示文案、日志都用它）。"""

ARTIFACT_CONFLICT_GROUP: dict[str, frozenset[str]] = {
    # 同一份文档不能同时"整篇生成"和"按指令优化"：两者都会把正文写进同一个字段，
    # 谁后写完谁赢 —— 结果是两份内容互相覆盖，而用户看到的是"优化没生效"或"生成丢了"。
    "prd": frozenset({"prd", "optimize-prd"}),
    "optimize-prd": frozenset({"prd", "optimize-prd"}),
    "api-docs": frozenset({"api-docs", "optimize-api-docs"}),
    "optimize-api-docs": frozenset({"api-docs", "optimize-api-docs"}),
    "prompts": frozenset({"prompts", "optimize-prompts"}),
    "optimize-prompts": frozenset({"prompts", "optimize-prompts"}),
}
"""`artifact` → 与它**互斥**的那些 artifact（含它自己）。

⚠️ **不能退化成"按 artifact 字符串精确匹配"**（那是这张表存在的原因）：
同一个 session 下 `prd` 在跑时又点 PRD 优化，两者写的都是 `documents.prd.content`；
只比字符串会放它进来，于是两份输出互相覆盖。不同的文档之间（如 `prd` 与 `api-docs`）
**不冲突** —— 它们各写各的字段，并行完全安全。
"""

PRD_REVIEW_RESULT_KEY = "prdReviewResult"
"""PRD 审查结论写回会话快照时的键名。

只有 PRD 有审查环节（`document_service.generate_prd_with_review_events`），
所以另两份产物不写这个键 —— 编一个空壳出来只会让前端以为"审过了、没问题"。
"""

PAYLOAD_REQUIRED_KEYS: dict[str, tuple[str, ...]] = {
    "prd": ("requirements_summary",),
    "api-docs": ("prd_content",),
    "prompts": ("prd_content",),
    # 优化：五项缺一不可。比需求文档的示例多两项（`section` / `document_content`），
    # 理由是**服务层的既有契约**：
    #   - `optimize_document_stream` 必须知道"修订哪一节"（`section`，逐字匹配标题）；
    #   - `current_content` 在那条契约里是**该节的现有正文**（不是整篇）——
    #     需求示例把它当成整篇，照字面实现会把整篇文档当"一节"喂给模型；
    #   - 收尾要把模型输出拼回整篇（`section_edit.replace_section`），所以要 `document_content`。
    "optimize-prd": ("doc_type", "section", "current_content", "document_content", "instruction"),
    "optimize-api-docs": (
        "doc_type",
        "section",
        "current_content",
        "document_content",
        "instruction",
    ),
    "optimize-prompts": (
        "doc_type",
        "section",
        "current_content",
        "document_content",
        "instruction",
    ),
}
"""每种产物**必须**带的 payload 键。

在路由层就校验（→ 422），而不是等 runner 跑起来才在协程里抛 ——
任务创建成功却立刻失败，用户拿到的是一个 job_id 和一条错误，比当场被拒更难查
（与 `api/conversation.py`「入参校验必须在开流前完成」是同一条纪律）。"""

PAYLOAD_ALLOWED_KEYS: dict[str, tuple[str, ...]] = {
    "prd": ("requirements_summary", "conversation_messages"),
    "api-docs": ("prd_content", "conversation_messages"),
    "prompts": ("prd_content", "api_docs_content", "conversation_messages"),
    # 优化：前五项见 `PAYLOAD_REQUIRED_KEYS`；`context` 是可选的一包生成侧上下文
    # （`form` / `known_info` / `open_questions` / `conflicts` / `outline` / `scope_spec` /
    #  `prd_content` / `api_content` / `doc_title`），原样转给 `optimize_document_stream`。
    # 拆成一个个顶层键会让"哪些是本产物要的"变得不可读，所以这里用一层嵌套。
    "optimize-prd": (
        "doc_type",
        "section",
        "current_content",
        "document_content",
        "instruction",
        "context",
    ),
    "optimize-api-docs": (
        "doc_type",
        "section",
        "current_content",
        "document_content",
        "instruction",
        "context",
    ),
    "optimize-prompts": (
        "doc_type",
        "section",
        "current_content",
        "document_content",
        "instruction",
        "context",
    ),
}
"""各类产物**允许**带的 payload 键（多传的键在服务层被丢弃并记 warning）。

`conversation_messages` 是 `[{role, content}]`，直接喂给生成器当 history ——
与 `api/schemas.py` 的 `ConversationTurn` 同形。
"""

OPTIMIZE_CONTEXT_KEYS: tuple[str, ...] = (
    "form",
    "known_info",
    "open_questions",
    "conflicts",
    "outline",
    "scope_spec",
    "prd_content",
    "api_content",
    "doc_title",
)
"""优化任务 `payload.context` 里**允许**出现的键（= `optimize_document_stream` 除
`kind` / `section` / `feedback` / `current_content` 之外的可选入参）。

白名单是刻意的：`context` 原样透传给服务层，不设白名单的话调用方多写一个键
就会变成 `TypeError: unexpected keyword argument`（在后台协程里炸，排查很远）。
"""


class JobRecord(BaseModel):
    """`generation_jobs` 的一行（**内部**契约，含 payload 与草稿）。

    ⚠️ 对外视图是 `api/schemas.py` 的 `JobSnapshotView`，只暴露需求里点名的那几个字段。
    两者刻意分开：内部可以自由加列（比如 `payload_json`、`previous_draft`），
    对外契约只增不减，否则前端会被内部重构反复打断。
    """

    id: str
    session_id: str
    artifact: JobArtifact
    status: JobStatus
    phase: JobPhase
    payload_json: str
    """创建时的输入参数原文。留着是为了排查（"这次到底喂了什么进去"）。

    ⚠️ **不是重跑用的**：任务跑完再拿旧 payload 重跑，产出的东西与当时的上游
    （PRD / 接口文档）可能已经不是一份了。要重跑请由调用方重新创建任务。
    """
    draft_content: str = ""
    """**权威草稿**。刷新后 snapshot 从这里读；chunk 按节流（默认 500ms）UPDATE 进来。

    为什么它必须是权威：前台流式那套把正文留在浏览器内存里，刷新即丢；
    而"生成到一半刷新，回来还能看到已经写了多少"是任务化最直接的收益。
    """
    previous_draft: str = ""
    """PRD 进入**改写**阶段前保存的初稿（v1）。

    审核发现问题后 runner 会清空 `draft_content` 从头写 v2；不清 v1 的话两稿会拼在一起，
    而拼起来的文档看起来是完整的（没有语法错误），只有内容前后矛盾。
    """
    review_json: str | None = None
    """审查 Agent 的结构化结论（JSON 原文）。仅 PRD 有。"""
    result_json: str | None = None
    """终稿快照（JSON 原文）：完成时是 `{content, review, revision_applied, run_summary}`。

    失败时也会写（带 `run_summary`）—— 否则重连的订阅者拿不到那一趟的账，
    而"出错那趟调了几次模型"正是排查时第一个要看的数。
    """
    error: str | None = None
    created_at: str
    updated_at: str

    def payload(self) -> dict[str, Any]:
        """解析 `payload_json`。坏了返回 `{}` 而不是抛 —— 坏数据不该让查询接口 500。"""
        try:
            parsed = json.loads(self.payload_json)
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}

    def review(self) -> dict[str, Any] | None:
        """解析 `review_json`（没审过 / 坏数据返回 `None`）。"""
        if not self.review_json:
            return None
        try:
            parsed = json.loads(self.review_json)
        except (TypeError, ValueError):
            return None
        return parsed if isinstance(parsed, dict) else None

    def result(self) -> dict[str, Any] | None:
        """解析 `result_json`（还没收尾 / 坏数据返回 `None`）。"""
        if not self.result_json:
            return None
        try:
            parsed = json.loads(self.result_json)
        except (TypeError, ValueError):
            return None
        return parsed if isinstance(parsed, dict) else None


class JobSnapshot(BaseModel):
    """查询接口要回的那几个字段（由 `JobRecord` 派生，**不落库**）。

    `review` / `result` 从 JSON 列解析出来 —— 让前端少解一次字符串。

    后两个字段是**给优化任务的重连用的**（生成任务不需要）：优化时 `draft_content` 是
    "模型这次输出的那一节"，前端要把它拼回整篇才能显示；而拼接需要知道**拼哪一节**。
    它们从 `payload_json` 里取，因此**不需要改表结构**（payload 本来就存着）。
    """

    id: str
    session_id: str
    artifact: JobArtifact
    status: JobStatus
    phase: JobPhase
    draft_content: str = ""
    review: dict[str, Any] | None = None
    result: dict[str, Any] | None = None
    error: str | None = None
    created_at: str = Field(default="")
    updated_at: str = Field(default="")

    doc_type: str | None = None
    """优化任务改的是哪份产物（`prd` / `api` / `prompts`）。仅 `optimize-*` 有。"""
    section: str | None = None
    """优化任务在改哪一节（**逐字**的标题行）。仅 `optimize-*` 有。

    ⚠️ 前端拿它 + `draft_content` 就能把"优化中的那一节"拼回整篇显示 ——
    没有它，重连后只能显示一节片段，用户会以为整篇被替换了。
    """

    @classmethod
    def from_record(cls, record: JobRecord) -> JobSnapshot:
        payload = record.payload() if is_optimize_artifact(record.artifact) else {}
        doc_type = payload.get("doc_type")
        section = payload.get("section")
        return cls(
            id=record.id,
            session_id=record.session_id,
            artifact=record.artifact,
            status=record.status,
            phase=record.phase,
            draft_content=record.draft_content,
            review=record.review(),
            result=record.result(),
            error=record.error,
            created_at=record.created_at,
            updated_at=record.updated_at,
            doc_type=doc_type if isinstance(doc_type, str) else None,
            section=section if isinstance(section, str) else None,
        )
