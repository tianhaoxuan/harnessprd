"""API 请求/响应模型 —— 路由层的数据契约。

**放什么**：跨路由共享、或直接对外暴露的模型（`SessionSnapshot`、事件请求……）。
**不放什么**：

- 领域模型（服务层内部用）→ `services/`
- 静态资源的形状（表单题目）→ `core/questions.py`（定义跟着配置走）
- 单个路由私有的入参 → 留在那个路由文件里，被两处用到再挪过来

所有字段都标了出处。**设计里未定的部分不猜** —— 带 ⚠️ 的字段对应文档中的待确认项，
实现时可能变；提前定义只会造出第二份契约（`HANDOFF.md` §4 坑 #10）。
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, Field, field_validator, model_validator

# 枚举定义在 services/state.py（内部状态契约）。api → services 是允许的方向；
# import 复用而不是各写一份 —— 两份枚举必然漂移。
from services.state import DialogueStage, DocKind, DocStatus, SessionPhase

# 轮次上限的单一来源（`docs/对话阶段设计.md` §5.4）。
from services.conversation_service import DEFAULT_MAX_ROUNDS

# 分片范围用服务层的 dataclass 直接表达，不在 API 层再定义一个同形模型 ——
# 那样 `DocumentScopeView` → `DocumentScope` 的转换就会变成两份契约的同步工作。
from services.document_service import DocumentScope

# Generation Job 的枚举与 payload 规则同理：定义在 services/job_models.py，
# 这里复用。三个 Literal 直接就是数据库 CHECK 的那三个取值。
from services.job_models import (
    PAYLOAD_REQUIRED_KEYS,
    JobArtifact,
    JobPhase,
    JobSnapshot,
    JobStatus,
)

# ⚠️ **必须起别名**：本文件第 215 行已经有一个同名的 `SessionSummary`（给占位接口
# `/api/v1/sessions` 用的状态机版契约：`state: SessionPhase` + `datetime` 时间）。
# 直接 `import SessionSummary` 会被后面那个类定义**遮蔽** —— 于是
# `SessionStoreListView` 的注解解析到另一个类，Service 返回的对象校验不过，
# 报错还是一句很难懂的"Input should be a valid ... instance of SessionSummary"（已实测踩到）。
from services.session_models import SessionSummary as StoredSessionSummary

# 文档槽位与版本链的领域模型 + 枚举，同理复用（定义在 services/，此处不重写字段清单）。
# ⚠️ 枚举定义在 `document_version_repository`（它同时是两张表 CHECK 约束的取值来源）——
# 单一来源，不需要 `smoke_check.py` 再补一条"两处是否一致"的断言。
from services.document_version_repository import DocType, VersionSourceKind
from services.document_version_service import (
    CONTENT_PREVIEW_CHARS,
    DocumentSlot,
    DocumentVersionRecord,
    content_preview,
)

__all__ = [
    # 枚举（定义在 services/state.py，此处复用）
    "DialogueStage",
    "DocKind",
    "DocStatus",
    "SessionPhase",
    # 对话内部结构
    "Conflict",
    "DialogueView",
    "Message",
    "PendingQuestion",
    # 文档视图
    "DocumentView",
    "FailureView",
    "GenerationView",
    # 会话
    "SessionListResponse",
    "SessionSnapshot",
    "SessionSummary",
    # 请求体
    "EventRequest",
    # 对话流（SSE）
    "ContinueStreamRequest",
    "ConversationTurn",
    "StartStreamRequest",
    # 产物生成 / 修订（SSE）
    "DocumentGenerationRequest",
    "DocumentScopeView",
    "GenerateApiDocsRequest",
    "GeneratePrdFromSummaryRequest",
    "GeneratePrdRequest",
    "GeneratePromptsRequest",
    "OptimizeDocumentRequest",
    # 对话 → 结构化摘要回填（非流式）
    "SyncSummaryRequest",
    "SyncSummaryResponse",
    # 接口文档 RAG 检索（非流式，不调模型）
    # Generation Job（`/api/jobs/*`）
    "CreateJobRequest",
    "CreateJobResponse",
    "JobSnapshotView",
    # 文档槽位与版本链（`/api/session/{id}/documents/*`）
    "DocumentCheckpointRequest",
    "DocumentCheckpointResponse",
    "DocumentRestoreResponse",
    "DocumentSlotListView",
    "DocumentSlotView",
    "DocumentVersionDetailView",
    "DocumentVersionListView",
    "DocumentVersionNoteRequest",
    "DocumentVersionNoteResponse",
    "DocumentVersionSummaryView",
]


def _non_blank(value: str) -> str:
    """拒绝"只有空白"的字符串。"""
    if not value.strip():
        raise ValueError("不能是空白字符")
    return value


NonBlankStr = Annotated[str, AfterValidator(_non_blank)]
"""**必填且有实质内容**的文本。

存在的理由：`Field(min_length=1)` 拦不住 `"   "`（三个空格），而这类输入一旦流到服务层，
`document_service._require_text` 会在**第一个 chunk 之前**抛 `ValueError` ——
但服务层的生成方法是 async generator，异常发生在 `_make_sse_generator` 内部，
于是客户端拿到的是 **HTTP 200 + 一个 `error` 事件**，而不是 4xx。

`api/conversation.py` 的模块 docstring 明确反对这种形状（配置类错误必须走 503，
不能变成"200 + 流里一个 error"）。所以**在边界上就拦掉**，服务层那道
`_require_text` 退化为纵深防御。
"""

# ---------------------------------------------------------------- 对话内部结构
# 枚举（SessionPhase / DocKind / DocStatus / DialogueStage）已在文件头从
# services.state 导入 —— 那是内部状态契约的唯一来源。
#
# 以下为**对外视图**模型。设计明确要求内外分离
# （docs/状态数据设计.md §1 原则②）：内部可自由增删，对外只增不减。


class Message(BaseModel):
    id: str
    role: Literal["user", "ai"]
    stage: DialogueStage
    kind: Literal["say", "question", "answer", "summary"]
    content: str = Field(description="AI 侧为 message 正文（Markdown）")
    created_at: datetime


class PendingQuestion(BaseModel):
    """AI 提出的、等用户回答的问题。

    `suggested_answer` 为必填 —— 对应提示词技巧 A2「每问必带建议答案」，
    也是「10 分钟拿到文档」能成立的前提（用户只需判断，不必从零描述）。
    """

    id: str
    question: str
    suggested_answer: str
    reason: str
    dimension: str | None = Field(
        default=None, description="F3.8 的 5 个维度之一（目标用户 / 核心场景 / 成功指标 / 范围边界 / 硬约束）"
    )
    answered: bool = False


class Conflict(BaseModel):
    # ⚠️ 设计原文用 `with_`（JSON 键会是 `with_`）。`with` 是 Python 关键字，
    # 不能做字段名。若想暴露更干净的键名（如 `source`），先改设计文档再加别名。
    with_: str = Field(description="冲突来源，如「表单 need_auth」/「S1 已确认的动线」")
    detail: str
    raised_at: datetime
    resolved_at: datetime | None = None
    resolution: str | None = None


class DialogueView(BaseModel):
    """快照里的对话视图 —— **不含完整 `messages`**（单独分页取）。"""

    messages: list[Message] = Field(default_factory=list, description="快照里只带最近 N 条")
    pending_questions: list[PendingQuestion] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list, description="用户跳过的项 → PRD 的开放问题章")
    conflicts: list[Conflict] = Field(default_factory=list)


# ---------------------------------------------------------------- 文档视图
# 出处：docs/状态数据设计.md §2.3


class DocumentView(BaseModel):
    """快照里的文档视图 —— **不含 `content`**（正文单独接口取，可能上万字符）。"""

    kind: DocKind
    status: DocStatus
    version: int = Field(description="重生成次数，从 1 开始")
    edited_by_user: bool = Field(description="改过就要在重生成前二次确认（F4.12）")
    approved_at: datetime | None = None
    stale_reason: str | None = None
    updated_at: datetime


class GenerationView(BaseModel):
    document: DocKind
    started_at: datetime
    attempt: int
    scope: Literal["full", "section"] = "full"
    section: str | None = None


class FailureView(BaseModel):
    document: DocKind
    error: str
    at: datetime
    attempts: int


# ---------------------------------------------------------------- 会话
# 出处：docs/状态数据设计.md §4.3（+ docs/会话持久化方案.md §9 的 revision）


class SessionSnapshot(BaseModel):
    """对外只读视图 —— 字段**只增不减**，否则前端会被内部重构反复打断。"""

    id: str
    title: str | None = None
    state: SessionPhase
    revision: int = Field(
        description="乐观锁版本。写操作要带 expected_revision，不符返回 409"
    )
    created_at: datetime
    updated_at: datetime

    step: int = Field(description="进度：1–9，对应主流程九步，给进度条用")
    step_label: str

    form: dict[str, str] = Field(default_factory=dict, description="键 = 题目 id")
    dialogue: DialogueView | None = None
    documents: dict[DocKind, DocumentView] = Field(default_factory=dict)
    active_generation: GenerationView | None = None
    failure: FailureView | None = None

    actions: list[str] = Field(
        default_factory=list,
        description=(
            "⭐ 当前状态允许的操作，由转换表（docs/状态机设计.md §3.2）推导。"
            "前端不自己判断「现在能不能点通过」，直接用这个列表渲染按钮 —— "
            "这样转换表是唯一真相，前端按钮与状态机规则不可能漂移"
        ),
    )


class SessionSummary(BaseModel):
    """列表项。

    ⚠️ 设计文档**没有**定义这个模型（`SessionStore.list()` 的返回类型 `SessionSummary`
    只是被引用）。这里取最小可用字段，实现时以实际列表页需要为准。
    """

    id: str
    title: str | None = None
    state: SessionPhase
    created_at: datetime
    updated_at: datetime


class SessionListResponse(BaseModel):
    items: list[SessionSummary]
    next_cursor: str | None = Field(
        default=None, description="分页游标；为 None 表示没有下一页"
    )


# ---------------------------------------------------------------- 请求体
# 出处：docs/状态数据设计.md §4.2 + docs/会话持久化方案.md §9


class EventRequest(BaseModel):
    """状态变更请求 —— **所有状态变更的唯一入口**的入参。

    合法事件名见 `docs/状态机设计.md` §3.2 的转换表：这里**刻意不枚举**，
    否则就成了第二份转换表，改了必漂移。
    """

    event: str
    payload: dict[str, Any] = Field(default_factory=dict)
    expected_revision: int | None = Field(
        default=None,
        description="乐观锁。与库中 revision 不符时返回 409，由前端提示并刷新（**不做自动合并**）",
    )


# ---------------------------------------------------------------- 对话流（SSE）
# 注意：这两个是**无状态补全接口**，不是状态变更。
# 设计对状态变更只留一条入口（`POST /sessions/{id}/events`，`状态数据设计` §4.2）；
# 这两个端点目前不落任何状态，所以不违规 —— 会话持久化落地后必须改形。


class ConversationTurn(BaseModel):
    """请求里带上来的历史消息（精简形态）。

    与 `api.schemas.Message`（完整契约，含 `id` / `stage` / `kind` / `created_at`）不同：
    请求侧只需要 role 与正文，其余由服务端补。两者不要混用。
    """

    role: Literal["user", "ai"] = Field(description="`user` = 用户，`ai` = 模型")
    content: str


class StartStreamRequest(BaseModel):
    """首轮流式对话的入参。"""

    form: dict[str, str] = Field(
        description="表单作答。键 = 题目 id，值 = 作答（与后端 `form: dict[str, str]` 同形）"
    )
    stage: DialogueStage = Field(default="S0", description="对话阶段，默认 S0 开场复盘")
    round_index: int = Field(default=1, ge=1)
    max_rounds: int = Field(default=DEFAULT_MAX_ROUNDS, ge=1)


class ContinueStreamRequest(BaseModel):
    """接续对话的入参。"""

    form: dict[str, str]
    history: list[ConversationTurn] = Field(
        default_factory=list, description="按时间正序的历史消息"
    )
    user_input: str = Field(min_length=1, description="用户本轮输入")
    stage: DialogueStage = Field(default="S0")
    round_index: int = Field(default=2, ge=1)
    max_rounds: int = Field(default=DEFAULT_MAX_ROUNDS, ge=1)
    # 下面三项来自会话层（`状态数据设计` §6.4 的 known_info 合并）。
    # 会话状态落地前由调用方带上，落地后应由服务端自己取。
    known_info: str = "（尚无）"
    open_questions: str = "（尚无）"
    conflicts: str = "（尚无）"


# ---------------------------------------------------------------- 产物生成 / 修订（SSE）
#
# ⚠️ **这四个请求体是"无状态补全"形状，不是会话形状。** 与 `start-stream` /
# `continue-stream` 同一个理由与同一个代价：它们让**客户端自己携带状态**
# （form / known_info / prd_content……），而设计明确要求「服务端 `state` 是真相」
# （`会话持久化方案` §7.3、`状态数据设计` §4.3）。
#
# 现在不得不这样，是因为 `SessionStore` 还没实现 —— 没有地方能按 `session_id` 取到这些输入。
# **会话层落地后这四个模型要整体换成 `{session_id}`**，届时路径也应从
# `/conversation/*` 挪到 `/sessions/{id}/documents/*`（那条现在还是 501）。
# 与 `api/conversation.py` 模块 docstring 里那张"必须改形"的清单是同一件事。


class DocumentScopeView(BaseModel):
    """分片范围，对应 `services/document_service.DocumentScope` 的三个占位符。

    | 字段 | 占位符 | 含义 |
    | --- | --- | --- |
    | `outline` | `{doc_outline}` | 完整结构清单 |
    | `scope` | `{generate_scope}` | **本次**要生成的部分 |
    | `spec` | `{scope_spec}` | 本次范围的详细规格（要点、风格、字数上限） |

    ⚠️ **三个要么都给、要么整个 `scope` 都不给。** 它们是一组：只给 `scope` 不给 `spec`，
    `gen_common.md` 的"严格按 `{scope_spec}` 的字数上限控制篇幅"就失去约束，
    而模型仍会以为自己知道范围 —— 这比不传更糟。都不给时服务层按
    `DocumentScope.whole_document()`（不切片）处理。
    """

    outline: NonBlankStr = Field(description="完整结构清单（章节标题 + 风格 + 输入来源）")
    scope: NonBlankStr = Field(description="本次要生成的部分（章节名或文件名列表）")
    spec: NonBlankStr = Field(description="本次范围的详细规格")


class ConversationContext(BaseModel):
    """四个接口共用的**对话侧输入**（对话阶段已经产出的东西）。

    ⚠️ `known_info` 的优先级**高于** `form`（`gen_common.md`：用户明确修正过表单答案时
    以对话为准）。而且它是生成质量的关键：缺 S3 优先级会让第 7 章所有功能都标 P0、
    缺 S4 默认值会让合规小节被权限内容顶替 —— 都是实测过的（`HANDOFF.md` §4 坑 #4）。
    这是**数据流要求，提示词救不了**。
    """

    form: dict[str, str] = Field(
        default_factory=dict,
        description="表单作答。键 = 题目 id。留空表示没有表单输入（未答项由服务层标「未填写」）",
    )
    known_info: str | None = Field(
        default=None,
        description="对话确认的结构化信息。**不传**由服务层填「（尚无）」，不要自己填空串",
    )
    open_questions: str | None = Field(
        default=None, description="用户跳过的项 —— 会原样进文档的「开放问题」章节"
    )
    conflicts: str | None = Field(default=None, description="已发现的矛盾及其解决结论")

    def service_kwargs(self) -> dict[str, Any]:
        """转成 `DocumentService` 的入参（`None` 的字段**丢掉**，不传）。

        为什么要过滤 `None`：服务层给这几个参数定的语义是"不传 = 填「（尚无）」"
        （`document_service._EMPTY`）。直接传 `None` 进去会让 `str.replace(..., None)`
        抛 `TypeError`；而在 API 层再写一遍「（尚无）」就是第二份哨兵定义。
        """
        kwargs: dict[str, Any] = {"form": self.form}
        for name in ("known_info", "open_questions", "conflicts"):
            value = getattr(self, name)
            if value is not None:
                kwargs[name] = value
        return kwargs


class DocumentGenerationRequest(ConversationContext):
    """三份产物**生成**接口的共同入参。"""

    scope: DocumentScopeView | None = Field(
        default=None, description="分片范围。不传 = 单次生成整份（正式流程应分片）"
    )

    def generation_kwargs(self) -> dict[str, Any]:
        """`service_kwargs()` + 转好的 `scope`。"""
        kwargs = self.service_kwargs()
        if self.scope is not None:
            kwargs["scope"] = DocumentScope(
                outline=self.scope.outline,
                scope=self.scope.scope,
                spec=self.scope.spec,
            )
        return kwargs


class GeneratePrdRequest(DocumentGenerationRequest):
    """`POST /conversation/generate-prd-stream` 的入参。

    上游只有表单与对话 —— PRD 是链条的源头，没有别的输入（`HANDOFF.md` §1）。
    """


class GenerateApiDocsRequest(DocumentGenerationRequest):
    """`POST /conversation/generate-api-docs-stream` 的入参。"""

    prd_content: NonBlankStr = Field(
        description="**已通过审核**的 PRD 全文。必填：接口文档的每个接口都要能追溯到 PRD 的 FR"
    )


class GeneratePromptsRequest(DocumentGenerationRequest):
    """`POST /conversation/generate-prompts-stream` 的入参。"""

    prd_content: NonBlankStr = Field(description="**已通过审核**的 PRD 全文。必填")
    api_content: str | None = Field(
        default=None,
        description="已通过审核的接口文档全文。不传由服务层填「（尚无）」"
        "——与 `validate_prompts.py` 验证时的行为一致",
    )


class SyncSummaryRequest(BaseModel):
    """`POST /conversation/sync-summary-from-conversation` 的入参。

    ⚠️ 与上面四个生成接口不同，这是**非流式**的：输出是一小段 JSON，
    调用方要拿整份结构做 diff，流式既没意义、半截 JSON 又没法用。
    """

    summary: dict[str, Any] = Field(
        description="当前结构化需求摘要，形状以 `skills/prd-generator/references/field-schema.json` 为准"
    )
    history: list[ConversationTurn] = Field(
        default_factory=list,
        description=(
            "对话历史，按时间正序。AI 侧建议只传可读正文（`extractStreamingMessage()` 抠出来的那段），"
            "传原始 JSON 信封也能接受 —— 服务层会把 `message` 剥出来"
        ),
    )

    @field_validator("summary")
    @classmethod
    def _summary_not_empty(cls, value: dict[str, Any]) -> dict[str, Any]:
        """空摘要 = 调用方传错了（没有可回填的东西），在边界上就拦成 422。"""
        if not value:
            raise ValueError("summary 不能是空对象 —— 回填需要一份当前摘要作为基准")
        return value


class SyncSummaryResponse(BaseModel):
    """回填结果。

    `summary` 是**完整结构**（未改动的字段原样带回），前端拿它与旧版本 diff 即可；
    `changed` 只是把 diff 的结论顺手给出，省得前端再算一遍。
    """

    summary: dict[str, Any] = Field(description="更新后的完整结构化摘要")
    changed: list[str] = Field(
        default_factory=list, description="内容发生变化的顶层字段名（可直接高亮这几项）"
    )
    dropped_keys: list[str] = Field(
        default_factory=list,
        description="模型自己发明、被服务端丢掉的键（提示词规则 3 的硬保证）。正常应为空",
    )
    truncated: bool = Field(
        default=False,
        description="输出是否撞上单次上限被截断。截断时 JSON 多半不完整，会先报 502",
    )
    finish_reason: str | None = Field(default=None, description="厂商原话（`stop` / `length` …）")


class OptimizeDocumentRequest(ConversationContext):
    """`POST /conversation/optimize-document-stream` 的入参（F8.6 单节重生成）。"""

    kind: DocKind = Field(description="修订哪份产物 —— 决定用哪份 system prompt")
    section: NonBlankStr = Field(
        description="要修订的章节 / 小节名，必须与文档里的标题**逐字一致**"
    )
    feedback: str = Field(
        default="", description="用户反馈（F4.8 打回时给的）。空则由服务层填一句默认说明"
    )
    current_content: NonBlankStr = Field(
        description="**该节的现有正文**。必填：只给反馈不给原文，模型会从零重写，"
        "用户手改过的内容全丢（`会话持久化方案` §7.4 要求重生成前对已手改内容二次确认）"
    )
    doc_title: str | None = Field(default=None, description="文档名。不传由服务层按 `kind` 取")
    outline: str | None = Field(default=None, description="原生成时的结构清单")
    scope_spec: str | None = Field(
        default=None, description="原生成时的详细规格 —— 让修订保持同一套字数与结构约束"
    )
    prd_content: str | None = Field(
        default=None, description="修订 api / prompts 类产物时必填（同生成接口的理由）"
    )
    api_content: str | None = Field(default=None, description="修订 prompts 类产物时可能用到")

    def optimize_kwargs(self) -> dict[str, Any]:
        """转成 `DocumentService.optimize_document_stream` 的入参。

        ⚠️ 这里**不传 `scope`**：那个方法的 `generate_scope` 被**强制**取 `section`
        （否则 system prompt 说"只输出 A"、human message 说"只输出 B"，模型收到两条
        矛盾指令，通常的后果是把整篇重写一遍）。所以它只收 `outline` / `scope_spec`。
        """
        kwargs = self.service_kwargs()
        kwargs["kind"] = self.kind
        kwargs["section"] = self.section
        kwargs["feedback"] = self.feedback
        kwargs["current_content"] = self.current_content
        for name in ("doc_title", "outline", "scope_spec", "prd_content", "api_content"):
            value = getattr(self, name)
            if value is not None:
                kwargs[name] = value
        return kwargs

    @model_validator(mode="after")
    def _require_prd_for_downstream(self) -> OptimizeDocumentRequest:
        """修 api / prompts 类产物时必须给 `prd_content`。

        与 `document_service` 里同一条 `_require_text` 守卫**刻意重复**：这里拦是为了
        返回 **422**（客户端错误），服务层那道是纵深防御 —— 服务层的生成方法是 async
        generator，它的 `ValueError` 会被 `_make_sse_generator` 转成
        "HTTP 200 + 一个 error 事件"，那是 `api/conversation.py` 明确反对的形状。
        """
        if self.kind in ("api", "prompts") and not (self.prd_content or "").strip():
            raise ValueError(f"kind={self.kind} 时必须给 prd_content —— 该类产物必须能追溯到 PRD")
        return self


# ---------------------------------------------------------------- 分片计划


class DocumentPlanPart(BaseModel):
    """分片计划里的一"片"：一次生成调用要产出的那一段。

    字段名与 `DocumentScopeView` 一一对应（`scope` / `spec` / `outline`）——
    调用方把这一片原样塞进生成接口的 `scope` 即可，不需要自己拼字符串。
    """

    index: int = Field(description="从 1 起的序号，用于显示进度")
    label: NonBlankStr = Field(description="给人看的一句话，如「第 7 章（1 章）」")
    scope: NonBlankStr = Field(description="进 `{generate_scope}`：本片的范围")
    spec: NonBlankStr = Field(description="进 `{scope_spec}`：本片的详细规格")
    outline: NonBlankStr = Field(description="进 `{doc_outline}`：整份结构（每片相同）")


class GeneratePrdFromSummaryRequest(BaseModel):
    """`POST /conversation/generate-prd-from-summary-stream` 的入参。

    **只吃结构化摘要**（与技能包 `field-schema.json` 同形）+ 可选对话历史 ——
    不走 20 题表单，也没有 `known_info`：摘要本身就是"已经确认过的信息"。
    """

    summary: dict[str, Any] = Field(
        description="结构化需求摘要，形状以 skills/prd-generator/references/field-schema.json 为准"
    )
    history: list[ConversationTurn] = Field(
        default_factory=list, description="对话历史（可选），仅用于理解摘要里没写全的措辞"
    )
    review: bool = Field(
        default=False,
        description=(
            "是否启用**双智能体**：Writer 写初稿 → Reviewer 审核 → 有问题自动重写（上限 1 次）。"
            "开启后调用数由 1 次变为 2~3 次、耗时约翻倍，且审核期间没有流式输出"
        ),
    )

    @field_validator("summary")
    @classmethod
    def _summary_required(cls, value: dict[str, Any]) -> dict[str, Any]:
        """空摘要 = 没有输入可生成，在边界上拦成 422（服务层还有一道同样的守卫）。"""
        if not value:
            raise ValueError("summary 不能是空对象 —— 这条路径的唯一输入就是结构化摘要")
        return value



class DocumentPlanView(BaseModel):
    """`GET /conversation/document-plan` 的响应。**不调模型**，纯解析 + 分批。"""

    kind: DocKind
    multi_part: bool = Field(description="是否需要分多次调用。`false` = 整份一次生成")
    source: str = Field(description="清单解析自哪个提示词文件（留痕，便于核对来源）")
    parts: list[DocumentPlanPart]

    @classmethod
    def from_plan(cls, plan: Any) -> DocumentPlanView:
        """由 `services.document_plan.DocumentPlan` 构造视图。"""
        return cls(
            kind=plan.kind,
            multi_part=plan.multi_part,
            source=plan.source,
            parts=[
                DocumentPlanPart(
                    index=part.index,
                    label=part.label,
                    scope=part.scope,
                    spec=part.spec,
                    outline=part.outline,
                )
                for part in plan.parts
            ],
        )


# ---------------------------------------------------------------- 会话存储（/api/session/*）
#
# ⚠️ **实体形状不在这里重定义**：列表项直接复用 `services.session_models.SessionSummary`
# （下面以 `StoredSessionSummary` 的名字引用），详情用 `SessionLoad`（在 `api/session.py`
# 里当 `response_model`）—— API 契约与业务模型同源。再抄一份字段清单必然漂移：
# 前端加字段、后端那份悄悄过期，而且不会有任何报错。
#
# ⚠️ 与上面那个同名的 `SessionSummary`（第 215 行）**是两个不同的东西，不要合并**：
#
# | | 本区的 `StoredSessionSummary` | 上面的 `SessionSummary` |
# | --- | --- | --- |
# | 服务谁 | `/api/session/*`（**已实现**的快照存储） | `/api/v1/sessions`（**占位 501**） |
# | 阶段字段 | `entry_mode` + `current_stage` + `status`（字符串，与工作台一一对应） | `state: SessionPhase`（状态机版） |
# | 时间 | `str`（ISO8601 原文，直接透传） | `datetime`（由 pydantic 解析） |
#
# 本区只放**传输层才需要**的三样：请求体、列表信封、删除回执。


class SessionSaveRequest(BaseModel):
    """`POST /api/session/save` 的请求体。"""

    id: str | None = Field(
        default=None,
        description="留空 = 新建（返回新 id）；给出 = 更新该记录（返回同一个 id）",
    )
    session_data: dict[str, Any] | str = Field(
        description=(
            "工作台 state 快照。对象或 JSON 原文都收 —— 原文会**逐字节**存下来"
            "（不重排键、不改空白），所以前端能拿回它自己写进去的那一份。"
        )
    )


class SessionSaveResponse(BaseModel):
    """`POST /api/session/save` 的响应：新建与更新**统一**只回这两项。"""

    id: str
    title: str = Field(
        description="落库后的标题。更新已有记录时它是原值（标题不随更新改写）"
    )


class SessionDeleteResponse(BaseModel):
    """`DELETE /api/session/{id}` 的响应。"""

    ok: bool = True


class SessionStoreListView(BaseModel):
    """`GET /api/session/list` 的信封：`{items: [...]}`。"""

    items: list[StoredSessionSummary] = Field(
        default_factory=list,
        description="摘要列表，按 updated_at 倒序；**不含 session_data**",
    )


# ---------------------------------------------------------------- 文档槽位与版本链
# 路由：`/api/session/{session_id}/documents/*`（6 条，见 `api/documents.py`）。
#
# ⚠️ 路径**不在 `/api/v1` 下** —— 与 `/api/session/*`、`/api/jobs/*` 同族（需求给定的路径），
# 所以 `main.py` 里单独挂、不加版本前缀。
#
# 与上面两个区同理：这里只放**传输层才需要**的形状。领域模型在
# `services/document_version_service.py`（`DocumentSlot` / `DocumentVersionRecord`），
# 本区不复制它们的字段定义，只决定"哪些字段对外可见"（`from_slot` / `from_record`）。
#
# 三条刻意的取舍：
#
# 1. **列表项不含全文**（只给 `content_preview`）。侧栏一次要列出几十版，带上全文就是
#    几十份 Markdown 白传；要看全文走单版详情接口。
# 2. **`change_note` 用 `exclude_if` 而不是 `exclude_none`**：需求要的是"无备注时**省略**这个键"，
#    而 `exclude_none` 会连 `current_version_id: null` 一起吃掉 —— 那是需求明确要求保留的字段
#    （§6.1 的示例里就是 `null`）。
# 3. `source_kind` 直接复用 `services` 的枚举（不是 `str`）：这样 OpenAPI 里能选出合法值，
#    而不是让调用方去猜有哪些取值。


class DocumentSlotView(BaseModel):
    """一个文档槽位的摘要（**不含正文**）。"""

    doc_type: DocType
    document_id: str
    current_version_id: str | None = Field(
        default=None, description="指向当前生效的版本；**尚未生成任何版本时为 null**"
    )
    current_version_no: int | None = Field(default=None, description="尚未生成时同样为 null")
    updated_at: str | None = Field(
        default=None,
        description="**当前版本的**最近更新时间。尚无版本时为 null —— 槽位行本身可能是刚建的，"
        "把它的创建时间当更新时间会让「从没生成过」看起来像「刚更新过」",
    )

    @classmethod
    def from_slot(cls, slot: DocumentSlot) -> DocumentSlotView:
        return cls(
            doc_type=slot.doc_type,
            document_id=slot.id,
            current_version_id=slot.current_version_id,
            current_version_no=slot.current_version_no,
            updated_at=slot.updated_at if slot.has_version else None,
        )


class DocumentSlotListView(BaseModel):
    """`GET .../documents` 的信封。**固定三项**（prd / api-docs / prompts），顺序固定。"""

    items: list[DocumentSlotView] = Field(
        default_factory=list,
        description="固定三项：prd、api-docs、prompts。**没有版本也返回**，只是 current 为 null",
    )


class DocumentVersionSummaryView(BaseModel):
    """版本列表项：**不含全文**，只有预览与用户备注。"""

    id: str
    version_no: int
    source_kind: VersionSourceKind
    created_at: str
    content_preview: str = Field(
        description=f"正文前 {CONTENT_PREVIEW_CHARS} 字。**仅供内部与后续扩展**，"
        "侧栏不拿它当自动摘要展示 —— 那是 `change_note` 的位置"
    )
    is_current: bool
    change_note: str | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
        description="用户备注（`metadata.change_note`）。**没有备注时这个键不出现**",
    )

    @classmethod
    def from_record(
        cls, record: DocumentVersionRecord, *, current_version_id: str | None
    ) -> DocumentVersionSummaryView:
        return cls(
            id=record.id,
            version_no=record.version_no,
            source_kind=record.source_kind,
            created_at=record.created_at,
            content_preview=content_preview(record.content),
            is_current=record.id == current_version_id,
            change_note=record.change_note(),
        )


class DocumentVersionListView(BaseModel):
    """`GET .../versions` 的响应：列表 + 槽位身份（前端据此知道"当前是哪一版"）。"""

    doc_type: DocType
    document_id: str
    current_version_id: str | None = None
    versions: list[DocumentVersionSummaryView] = Field(
        default_factory=list, description="按 version_no **降序**；尚无版本时是空数组"
    )


class DocumentVersionDetailView(BaseModel):
    """`GET .../versions/{version_id}` 的响应：**含全文**，供预览用。"""

    id: str
    version_no: int
    source_kind: VersionSourceKind
    content: str
    source_job_id: str | None = Field(
        default=None, description="生成这一版的 `generation_jobs.id`；checkpoint / restore 为 null"
    )
    parent_version_id: str | None = Field(
        default=None, description="切换前的那一版；**首版为 null**"
    )
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="`run_summary` / `review` / `change_note` / `restored_from_version_*`；"
        "未产生的字段一律省略",
    )
    created_at: str
    is_current: bool

    @classmethod
    def from_record(
        cls, record: DocumentVersionRecord, *, current_version_id: str | None
    ) -> DocumentVersionDetailView:
        return cls(
            id=record.id,
            version_no=record.version_no,
            source_kind=record.source_kind,
            content=record.content,
            source_job_id=record.source_job_id,
            parent_version_id=record.parent_version_id,
            metadata=record.metadata(),
            created_at=record.created_at,
            is_current=record.id == current_version_id,
        )


class DocumentCheckpointRequest(BaseModel):
    """`POST .../versions/checkpoint` 的请求体。**整个请求体可选**。"""

    content: str | None = Field(
        default=None,
        description="前端当前编辑器全文。**不传则用服务端 current 版本的正文** —— "
        "所以「只是打个标记」的调用不必把上万字传一遍",
    )


class DocumentCheckpointResponse(BaseModel):
    """checkpoint 的回执：**只回身份，不回全文**（调用方本来就有）。"""

    version_id: str
    version_no: int
    document_id: str


class DocumentRestoreResponse(BaseModel):
    """restore 的回执：比 checkpoint 多一个 `content`（供 Session 镜像同步）。"""

    version_id: str
    version_no: int
    document_id: str
    content: str = Field(description="新版本的正文（= 被恢复那一版的正文）")


class DocumentVersionNoteRequest(BaseModel):
    """`PATCH .../versions/{version_id}/note` 的请求体。"""

    change_note: str | None = Field(
        default=None,
        description="用户备注。**空串或省略 = 清空备注**（`metadata.change_note` 这个键会被删掉）",
    )


class DocumentVersionNoteResponse(BaseModel):
    """备注写入回执。`change_note` 为 `null` 表示"现在没有备注"（含刚被清空）。"""

    version_id: str
    version_no: int
    change_note: str | None = None


# ---------------------------------------------------------------- Generation Job（`/api/jobs/*`）
#
# 与上一个区同理：这里只放**传输层才需要**的形状（请求体、快照响应、创建回执）。
# 任务本身的领域模型在 `services/job_models.py`（`JobRecord` / `JobSnapshot`），
# 本区不复制它的字段定义，只决定"哪些字段对外可见"。


class CreateJobRequest(BaseModel):
    """`POST /api/jobs` 的请求体。

    ## `payload` 为什么按 artifact 分形状

    三种产物要的输入不一样（PRD 要结构化摘要，后两份要 PRD 正文），所以 `payload`
    是个自由字典 + **按 artifact 校验必需键**（见 `job_models.PAYLOAD_REQUIRED_KEYS`）。
    不在这里写三个子模型的原因：那样每加一种产物就要改三处（模型、路由、runner），
    而校验规则只需要一份清单。

    ⚠️ 校验放在这里（→ **422**）而不是留给 runner：任务创建成功却立刻失败，
    用户拿到的是一个 `job_id` 加一条错误，比当场被拒更难查
    （与 `api/conversation.py`「入参校验必须在开流前完成」是同一条纪律）。
    """

    session_id: NonBlankStr = Field(description="任务挂在哪份会话上（会话必须已存在）")
    artifact: JobArtifact = Field(
        description="生成哪种产物：`prd`（从结构化摘要起草）/ `api-docs`（从 PRD 推导）/ "
        "`prompts`（从 PRD + 接口文档推导）"
    )
    payload: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "各产物的输入。必需键：`prd` → `requirements_summary`；"
            "`api-docs` → `prd_content`；`prompts` → `prd_content`（可选 `api_docs_content`）。"
            "三者都可带 `conversation_messages`（`[{role, content}]`）当对话历史"
        ),
    )

    @model_validator(mode="after")
    def _check_required_payload_keys(self) -> CreateJobRequest:
        required = PAYLOAD_REQUIRED_KEYS[self.artifact]
        missing = [key for key in required if not _has_content(self.payload.get(key))]
        if missing:
            raise ValueError(
                f"{self.artifact} 任务的 payload 缺少必需项：{'、'.join(missing)}"
                f"（该产物需要的键：{'、'.join(required)}）"
            )
        return self


def _has_content(value: Any) -> bool:
    """payload 里的值算不算"有内容"。空字典 / 空串 / 全空白都算没有。

    `api-docs` 的 `prd_content` 是空串时**必须**在 422 被拦下：没有 PRD 可读，
    模型只会凭空造接口，而造出来的东西看起来完全像真的
    （`document_service.generate_api_docs_stream` 的同一条理由）。
    """
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, Mapping):
        return bool(value)
    return True


class CreateJobResponse(BaseModel):
    """`POST /api/jobs` 的响应。

    **只回 `job_id`**（需求如此）：后续查状态走 `GET /api/jobs/{id}`，
    收进度走 `GET /api/jobs/{id}/stream`。这里不回快照是为了让创建路径尽量短 ——
    它本该是一个"登记任务并立刻返回"的动作，不该顺便把草稿也读一遍。
    """

    job_id: str


class JobSnapshotView(BaseModel):
    """`GET /api/jobs/{job_id}` 的响应（也是 SSE 首帧 `snapshot` 的载荷）。

    ⚠️ 与 `services/job_models.JobRecord` 刻意不同：内部记录有 `payload_json` /
    `previous_draft` / `created_at` 这些**不该对外**或暂时不需要对外的列。
    对外契约只增不减，内部字段则可以随便加。
    """

    id: str
    session_id: str
    artifact: JobArtifact
    status: JobStatus
    phase: JobPhase
    draft_content: str = Field(
        default="", description="**权威草稿**。刷新后页面应当先用它填上，不要留白"
    )
    review: dict[str, Any] | None = Field(
        default=None, description="审查结论（仅 PRD）：`{passed, issues, review_model, review_skipped, round}`"
    )
    result: dict[str, Any] | None = Field(
        default=None,
        description="收尾结果：`{content, review, revision_applied, truncated, finish_reason, run_summary}`",
    )
    error: str | None = Field(default=None, description="失败原因（`status=failed` 时才有）")
    doc_type: str | None = Field(
        default=None,
        description="**优化任务**改的是哪份产物（`prd` / `api` / `prompts`）；生成任务为 `null`",
    )
    section: str | None = Field(
        default=None,
        description=(
            "**优化任务**在改哪一节（逐字的标题行）；生成任务为 `null`。"
            "⚠️ 优化时 `draft_content` 是「模型这一节写了多少」，**不是整篇** —— "
            "客户端要靠它把这一节拼回整篇才能正确显示"
        ),
    )

    @classmethod
    def from_snapshot(cls, snapshot: JobSnapshot) -> JobSnapshotView:
        return cls(**snapshot.model_dump(exclude={"created_at", "updated_at"}))
