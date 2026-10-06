"""对话路由。

**本层纪律：只做参数解析与响应组装，不写业务逻辑。** 编排在 `services/conversation_service.py`。

路径最终是 `/api/v1/conversation/*` —— `/api/v1` 由 `main.py` 挂载 `api_router` 时加。

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/questions` | 表单题目定义（已实现） |
| POST | `/start-stream` | 首轮流式对话（SSE） |
| POST | `/continue-stream` | 接续流式对话（SSE） |
| POST | `/generate-prd-stream` | 生成 PRD（SSE）⚠️ **已弃用** |
| POST | `/generate-api-docs-stream` | 从 PRD 生成接口文档（SSE）⚠️ **已弃用** |
| POST | `/generate-prompts-stream` | 从 PRD 生成提示词套件（SSE）⚠️ **已弃用** |
| POST | `/generate-prd-from-summary-stream` | 从摘要生成 PRD（SSE）⚠️ **已弃用** |
| POST | `/optimize-document-stream` | 按反馈修订文档的某一节（SSE，F8.6）⚠️ **已弃用** |

## ⚠️ 五个 `generate-*` / `optimize-*` 已弃用：正式入口是 `POST /api/jobs`

Generation Job（`api/jobs.py` + `services/job_runner.py`）落地之后，"生成产物"与
"按反馈改一节"这两件事都有了**后台任务**这条正确的路：任务状态落库、刷新不断、
断开 SSE 照样跑完、草稿可恢复。

而下面这五个是**前台流式补全**：请求断了这一轮就没了，产物不落库。
它们仍在（`deprecated=True`）只是为了不破坏既有回归脚本与旧调用方，**新代码不要再用**。

| 弃用的前台接口 | 换成 |
| --- | --- |
| `generate-prd-stream` / `generate-prd-from-summary-stream` | `POST /api/jobs`（`artifact=prd`） |
| `generate-api-docs-stream` | `POST /api/jobs`（`artifact=api-docs`） |
| `generate-prompts-stream` | `POST /api/jobs`（`artifact=prompts`） |
| `optimize-document-stream` | `POST /api/jobs`（`artifact=optimize-*`） |

澄清那两条（`start-stream` / `continue-stream`）**没有**弃用 —— 澄清本来就是短交互，
任务化的收益抵不过复杂度（需求也明确"本次不改造澄清"）。

## ⚠️ 这几个 POST 都是「无状态补全」，不是状态变更

设计对状态变更只留**一条入口** —— `POST /sessions/{id}/events`
（`docs/状态数据设计.md` §4.2）；多开一个写接口，就多一条绕过转换表与 guard 校验的路径。

这些端点**不落任何状态**（不写会话、不改 `state`、不存历史），所以现在不违规。
但**会话持久化落地后必须改形**，正确做法是：

    写：POST /sessions/{id}/events { event: "reply", payload: { content } } → 202 + 快照
    读：单独的流 / 轮询端点（SSE，或 `会话持久化方案` §7.5 的 2s 轮询）

而不是让前端继续直连补全接口。**实现会话层时要把这一点一起改掉。**

### 四个生成接口的两处额外说明

**1. 它们没有满足「生成必须是后台任务」这条要求。**
`docs/会话持久化方案.md` §7.1 要求生成不绑在 HTTP 请求上（否则刷新会 abort 请求、
结果无人接收）。这四个接口是**前台流式补全**：请求断了，这一轮的产物就没了。
它之所以能存在，是因为 F3.14 需要"15s 内界面不能空白"的等待指示 ——
也就是说，**它只能当进度/预览通道，不能当产物的权威写入路径**。产物落库必须另走后台任务。

**2. 客户端要自己带一堆状态。** `form` / `known_info` / `prd_content` … 本来都该由
服务端按 `session_id` 取（`会话持久化方案` §7.3「服务端 `state` 是真相」）。
现在没有 `SessionStore`，取不到，只能让调用方传。这是**过渡形状**，
与 `start-stream` / `continue-stream` 是同一个代价 —— 见 `api/schemas.py`
「产物生成 / 修订」那一节。

## SSE 协议

三个**命名事件**（不是裸 `data: 文本`）::

    event: chunk
    data: {"text": "我看了你的表单…"}

    event: done
    data: {"chunks": 726, "chars": 1332}

    event: error
    data: {"type": "TimeoutError", "message": "…"}

⚠️ **POST + SSE 意味着前端不能用 `EventSource`**（它只支持 GET），要用
`fetch` + `ReadableStream` 手动解析帧。好处是随之没有「断线自动重连」的问题，
所以不需要 `[DONE]` 哨兵 —— `done` 事件就是明确的终止信号。

⚠️ **配置类错误不会走到流里。** 缺 API Key 之类的在开流前就被拦成 **503**
（见 `_ensure_llm_ready`），否则会变成"HTTP 200 + 流里一个 error 事件"，
与既有的「服务端未就绪返 503」约定（F6.5）矛盾，前端也不好处理。

⚠️ **入参校验同理，必须在开流前完成**：`api/schemas.py` 用 `NonBlankStr` 把"只有空白"
的必填项拦成 **422**。否则服务层的 `ValueError` 发生在 async generator 产出第一个 chunk
之前，会被 `_make_sse_generator` 转成"200 + error 事件" —— 同一个毛病。
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Mapping
from typing import Any, Protocol

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import StreamingResponse
from langchain_core.language_models.chat_models import BaseChatModel

from api.deps import ConversationServiceDep, DocumentServiceDep
from api.schemas import (
    ContinueStreamRequest,
    DocumentPlanView,
    GenerateApiDocsRequest,
    GeneratePrdFromSummaryRequest,
    GeneratePrdRequest,
    GeneratePromptsRequest,
    OptimizeDocumentRequest,
    StartStreamRequest,
    SyncSummaryRequest,
    SyncSummaryResponse,
)
from core.questions import QuestionsConfig, load_questions
from services.conversation_service import ConversationService
from services.document_plan import build_plan
from services.document_service import (
    DocumentService,
    SummarySyncError,
)


from core.request_context import get_request_id
from services.llm import LlmConfigError, StreamOutcome
from services.llm_metrics import RunMetricsCollector, finalize_run, log_run_summary
from services.token_estimator import BudgetExceededError
from services.state import DocKind

logger = logging.getLogger(__name__)

router = APIRouter()

SSE_MEDIA_TYPE = "text/event-stream"
"""`Content-Type` 由 `StreamingResponse(media_type=...)` 设置 —— Starlette 会补上
`; charset=utf-8`（中文必需）。所以它**不放进 `SSE_HEADERS`**，免得出现两份声明。"""

SSE_HEADERS: dict[str, str] = {
    # 流式响应不能被缓存
    "Cache-Control": "no-cache",
    # 保持长连接
    "Connection": "keep-alive",
    # 关掉 Nginx 等反向代理的响应缓冲。
    # 不关的话流会被攒起来一次性发出去，"逐字"效果没了 —— 本地开发看不出来，
    # 一挂反代就暴露，而且很难联想到是这里。
    "X-Accel-Buffering": "no",
}


# ---------------------------------------------------------------- SSE 组装


def _sse_event(event: str, data: Mapping[str, Any]) -> str:
    """组装一帧 SSE。

    `json.dumps(..., ensure_ascii=False)` 有两个作用：
    - 中文保持可读，不变成 `\\uXXXX`
    - 文本里的换行被转义成 `\\n`（两个字面字符），所以 **data 段永远只有一行**，
      天然满足 SSE「一帧多行时每行都要加 `data: ` 前缀」的格式要求
    """
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


async def _make_sse_generator(
    source: AsyncIterator[str],
    outcome: StreamOutcome | None = None,
    run_type: str | None = None,
) -> AsyncIterator[str]:
    """把**裸文本片段**的异步迭代器包装成 SSE 事件流。

    服务层只产出文本、不感知传输协议（`services` 不 import `fastapi`），
    协议细节全部收在这里。

    产出顺序：任意多个 `chunk` →（`done` 或 `error`）。
    其中：

    - 上游正常结束 → `done`（带片段数与字符数，便于前端/日志核对）
    - 中途抛异常 → `error`，然后**不再补 `done`**（避免前端把失败当成功收尾）
    - 客户端断开 → 生成器被关闭（`GeneratorExit`，不是 `Exception`），
      所以**不会**误发一个 error 事件

    `outcome` 是服务层填好的收尾信息（目前只有"是否被输出上限截断"）。
    **必须带上它**：截断在正文里看不出来（末尾就是半行表格），
    不带的话前端会拿一份残缺文档当成功收下。
    """
    chunks = 0
    chars = 0
    # 这一趟流程的总账。在 generator 体内 start：它会随当前 asyncio 任务
    # 传播给下游 service 里的 LLM 包装层（同一执行上下文）。
    run = RunMetricsCollector.start(run_type) if run_type else None
    try:
        async for piece in source:
            if not piece:
                continue  # 空片段不产帧，省得前端收到空事件
            chunks += 1
            chars += len(piece)
            yield _sse_event("chunk", {"text": piece})
    except Exception as exc:  # noqa: BLE001 - 流里任何异常都要转成可读事件
        logger.exception("SSE 流中途失败")
        yield _sse_event(
            "error",
            _budget_error_payload(exc)
            or {"type": type(exc).__name__, "message": str(exc), "request_id": get_request_id()},
        )
        # 失败也交账：否则「出错那趟调了几次模型」就查不到。
        # `finalize_run` 会把这一片并进同一 `X-Run-ID` 的整份账里（没带就是单片）。
        if run is not None:
            summary = finalize_run(run)
            log_run_summary(summary)
            yield _sse_event("run_summary", summary.to_dict(brief=True))
    else:
        payload: dict[str, Any] = {"chunks": chunks, "chars": chars}
        if outcome is not None:
            # `truncated` 是解释后的结论（前端只看它），`finish_reason` 是厂商原话（排查用）。
            payload["truncated"] = outcome.truncated
            payload["finish_reason"] = outcome.finish_reason
        if outcome is not None and outcome.truncated:
            logger.warning(
                "生成的文档被输出上限截断（finish_reason=%s）：%d 个片段 / %d 字符。"
                "接口文档整份生成必然会这样，需要改成分片，或让用户知道末尾是缺的。",
                outcome.finish_reason,
                chunks,
                chars,
            )
        # 汇总在 done **之前**发（需求：SSE 在 [DONE] 前有 run_summary）
        # finalize_run 内部会 finish()（摘掉收集器），先把上下文占用取出来
        run_context_usage = run.context_usage if run is not None else None
        if run is not None:
            summary = finalize_run(run)
            log_run_summary(summary)
            yield _sse_event("run_summary", summary.to_dict(brief=True))
            if run_context_usage is not None:
                payload["context_usage"] = run_context_usage
        yield _sse_event("done", payload)


def _budget_error_payload(exc: Exception) -> dict[str, Any] | None:
    """超预算的 error 帧：type 可编程判定，且带 request_id 与 context_usage。"""
    if not isinstance(exc, BudgetExceededError):
        return None
    return {
        "type": "TOKEN_BUDGET_EXCEEDED",
        "message": exc.check.message,
        "request_id": get_request_id(),
        "context_usage": exc.check.to_context_usage(),
    }


async def _make_stage_sse_generator(
    events: AsyncIterator[tuple[str, Any]],
    outcome: StreamOutcome | None = None,
    run_type: str | None = None,
) -> AsyncIterator[str]:
    """把**「阶段 + 正文」事件流**包装成 SSE 事件流（双智能体 PRD 专用）。

    与 `_make_sse_generator` 只差一点：多一类 `stage` 命名事件。
    为什么非要单独一类：审核那一段**没有任何正文**（模型只在内部出结论），
    要是只按 `chunk` 发帧，界面上就是长时间一动不动 —— 用户会以为卡死，
    然后刷新页面，把刚写的稿子一起扔掉。

    帧序：任意多个 `stage` / `chunk` →（`done` 或 `error`）。
    **收尾口径与 `_make_sse_generator` 完全一致**（含截断标记）：
    否则「截断必须如实上报」（`HANDOFF.md` §4 坑 #18）会在这条路径上悄悄失效。
    """
    chunks = 0
    chars = 0
    # 与 `_make_sse_generator` 同源：这趟流程的总账（review 路径走的是这个生成器）
    run = RunMetricsCollector.start(run_type) if run_type else None
    try:
        async for kind, payload in events:
            if kind == "stage":
                yield _sse_event("stage", payload)
                continue
            if not payload:
                continue  # 空片段不产帧
            chunks += 1
            chars += len(payload)
            yield _sse_event("chunk", {"text": payload})
    except Exception as exc:  # noqa: BLE001 - 流里任何异常都要转成可读事件
        logger.exception("SSE（双智能体）流中途失败")
        yield _sse_event(
            "error",
            _budget_error_payload(exc)
            or {"type": type(exc).__name__, "message": str(exc), "request_id": get_request_id()},
        )
        if run is not None:
            summary = finalize_run(run)
            log_run_summary(summary)
            yield _sse_event("run_summary", summary.to_dict(brief=True))
    else:
        payload: dict[str, Any] = {"chunks": chunks, "chars": chars}
        if outcome is not None:
            payload["truncated"] = outcome.truncated
            payload["finish_reason"] = outcome.finish_reason
        if outcome is not None and outcome.truncated:
            logger.warning(
                "生成的 PRD 被输出上限截断（finish_reason=%s）：%d 个片段 / %d 字符。",
                outcome.finish_reason,
                chunks,
                chars,
            )
        # 汇总在 done 之前发（与普通生成器口径一致）
        # finalize_run 内部会 finish()（摘掉收集器），先把上下文占用取出来
        run_context_usage = run.context_usage if run is not None else None
        if run is not None:
            summary = finalize_run(run)
            log_run_summary(summary)
            yield _sse_event("run_summary", summary.to_dict(brief=True))
            if run_context_usage is not None:
                payload["context_usage"] = run_context_usage
        yield _sse_event("done", payload)


class _HasLazyModel(Protocol):
    """任何"惰性构造模型"的服务都满足它（`ConversationService` / `DocumentService`）。

    用 Protocol 而不是 `ConversationService | DocumentService` 联合类型：
    这个检查与服务的具体职责**无关**，只依赖"有个 `model` 属性、没配 Key 时抛
    `LlmConfigError`"这一条约定。第三个服务出现时不用再改这里。
    """

    @property
    def model(self) -> BaseChatModel: ...


def _ensure_llm_ready(service: _HasLazyModel) -> None:
    """开流之前先把模型构造出来。

    模型是惰性构造的（没配 Key 时只有真正用才报错）。如果不在开流前触发，
    缺 Key 会变成「HTTP 200 + 流里一个 error 事件」；而在开流前触发，
    就能按既有约定返回 **503**（服务端未就绪，F6.5 / `README.md` 的状态码约定）。
    """
    try:
        _ = service.model
    except LlmConfigError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc


# ---------------------------------------------------------------- 已实现


@router.get("/questions", response_model=QuestionsConfig, summary="表单题目定义")
async def get_questions() -> QuestionsConfig:
    """返回 20 题定义 + `version`，供前端渲染表单。

    **题目定义必须由服务端下发，前端不能自己存一份**
    （`docs/对话阶段设计.md` §8 第 7 项）—— 与提示词外置同理，两份定义必然漂移。

    路由只做一件事：调 `core.questions` 的加载器。读文件、建模、缓存都在那边。

    带上的 `version` 是关键：会话按**创建时**的 `form_version` 解释作答，而不是按当前配置
    （`docs/状态数据设计.md` §6.2）—— 否则改一次表单，历史会话就全乱。
    """
    return load_questions()


@router.post("/start-stream", summary="首轮流式对话（SSE）")
async def start_stream(
    payload: StartStreamRequest,
    service: ConversationServiceDep,
) -> StreamingResponse:
    """用表单作答开场（默认 S0），逐段流式返回 AI 的回复文本。

    入参的 `form` 会被渲染进 `{form_summary}`，未答的题标「**未填写**」——
    这个标记不是装饰：少了它模型会拿邻近内容顶替（`HANDOFF.md` §4 坑 #3）。
    """
    _ensure_llm_ready(service)

    outcome = StreamOutcome()
    stream = service.start_conversation_stream(
        payload.form,
        stage=payload.stage,
        round_index=payload.round_index,
        max_rounds=payload.max_rounds,
        outcome=outcome,
    )
    return StreamingResponse(
        _make_sse_generator(stream, outcome, run_type="chat_start"), media_type=SSE_MEDIA_TYPE, headers=SSE_HEADERS
    )


@router.post("/continue-stream", summary="接续流式对话（SSE）")
async def continue_stream(
    payload: ContinueStreamRequest,
    service: ConversationServiceDep,
) -> StreamingResponse:
    """带上历史与用户本轮输入，继续当前阶段的对话，流式返回。

    `history` 用请求侧的精简形态（`ConversationTurn`：只有 role + content）——
    完整的消息契约（含 `id` / `stage` / `kind` / `created_at`）是响应侧的
    `api.schemas.Message`，两者不要混用。

    ⚠️ `stage` 由调用方传入，**还不是服务端状态机推进的结果**
    （`docs/对话阶段设计.md` §8 第 5 项要求服务端推进）。会话层落地后这里会改成
    从快照读 stage。
    """
    _ensure_llm_ready(service)

    outcome = StreamOutcome()
    stream = service.continue_conversation_stream(
        payload.form,
        [turn.model_dump() for turn in payload.history],
        payload.user_input,
        stage=payload.stage,
        round_index=payload.round_index,
        max_rounds=payload.max_rounds,
        known_info=payload.known_info,
        open_questions=payload.open_questions,
        conflicts=payload.conflicts,
        outcome=outcome,
    )
    return StreamingResponse(
        _make_sse_generator(stream, outcome, run_type="chat_continue"), media_type=SSE_MEDIA_TYPE, headers=SSE_HEADERS
    )


@router.post(
    "/sync-summary-from-conversation",
    response_model=SyncSummaryResponse,
    summary="从对话回填结构化摘要（非流式）",
)
async def sync_summary_from_conversation(
    payload: SyncSummaryRequest,
    service: DocumentServiceDep,
) -> SyncSummaryResponse:
    """把对话里用户**明确说过**的补充与修正回填进结构化摘要，返回完整结构。

    规则（只回填 / 不确定保持原值 / 不新增字段 / 返回完整结构）写在
    `core/prompts/sync_summary.md` 里；**服务端另外做两道硬保证**，不指望模型永远听话：

    - 越界键（schema 之外的顶层键与元素键）一律丢弃，并在 `dropped_keys` 里报出来；
    - 模型把原有内容的字段返回成空值时**不覆盖**，保留原值。

    **为什么不是流式**：输出是一小段 JSON，调用方要拿整份结构做 diff；
    流式既没有意义，半截 JSON 也没法用。同理，模型给出不可解析的 JSON 时**直接报 502**，
    而不是返回半个摘要 —— 那会让前端显示一堆假改动。
    """
    _ensure_llm_ready(service)

    try:
        result = await service.sync_requirements_summary(
            summary=payload.summary,
            history=[turn.model_dump() for turn in payload.history],
        )
    except SummarySyncError as exc:
        # 上游（模型）给了不可用的输出 —— 这是网关类错误，不是客户端的错（422 不合适）
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=f"回填失败：{exc}"
        ) from exc

    return SyncSummaryResponse(
        summary=result.summary,
        changed=list(result.changed),
        dropped_keys=list(result.dropped_keys),
        truncated=result.truncated,
        finish_reason=result.finish_reason,
    )




@router.post(
    "/generate-prd-from-summary-stream",
    summary="从结构化摘要生成 PRD（SSE）",
    deprecated=True,
)
async def generate_prd_from_summary_stream(
    payload: GeneratePrdFromSummaryRequest,
    service: DocumentServiceDep,
) -> StreamingResponse:
    """**直接从结构化摘要生成 PRD**，流式返回 Markdown。

    ⚠️ **已弃用（deprecated）：正式入口是 `POST /api/jobs`**（`artifact=prd`）。
    本接口是**前台流式**：请求断了这一轮就没了，产物不落库。它保留是因为
    `validate_prompts.py` / `skill_prd_check.py` 等回归脚本按裸文本契约在用它。
    **新代码不要再用它**。

    与任务化的差别（不是等价替换，读之前要知道）：任务里的 PRD 走**双智能体**
    （写手 + 审核员，见 `services/job_runner.py`），而本接口默认 `review=false` 是单智能体。
    想要"审一遍再交稿"请用 `/api/jobs`；想要"一次裸文本、不审"才用它。

    与 `generate-prd-stream` 是**两条并存的路径**（见
    `DocumentService.generate_prd_from_summary_stream` 的对比表）：
    这条的输入是技能包 8 字段摘要，不经过 20 题表单与对话澄清。

    ⚠️ 因此这条路径的 `[待确认]` 会明显更多 —— 对话澄清补的那些细节它没有。
    设计意图是"摘要已经够完整时走最短链路"，不是替代澄清流程。

    `review=true` 时走**双智能体**（Writer 写初稿 → Reviewer 审核 → 有问题最多重写
    `PRD_MAX_REWRITES` 次），会多出一类 `stage` 命名事件，前端据此显示
    「正在写 / 正在审核 / 正在重写」。代价是**调用数与耗时翻倍**：实测审核通过
    2 次调用 / 5.6 秒，触发重写 3 次调用 / 10.5 秒（单次生成是 1 次 / 约 5.7 秒）。
    默认 `false`，因为这条路径也被回归脚本按裸文本用着。
    """
    _ensure_llm_ready(service)

    outcome = StreamOutcome()
    history = [turn.model_dump() for turn in payload.history]
    if payload.review:
        events = service.generate_prd_with_review_events(
            summary=payload.summary, history=history, outcome=outcome
        )
        return StreamingResponse(
            _make_stage_sse_generator(events, outcome, run_type="generate_prd_from_summary"),
            media_type=SSE_MEDIA_TYPE,
            headers=SSE_HEADERS,
        )
    stream = service.generate_prd_from_summary_stream(
        summary=payload.summary, history=history, outcome=outcome
    )
    return StreamingResponse(
        _make_sse_generator(stream, outcome, run_type="generate_prd_from_summary"), media_type=SSE_MEDIA_TYPE, headers=SSE_HEADERS
    )

# ---------------------------------------------------------------- 产物生成 / 修订
#
# 四个路由的形状完全一致：`_ensure_llm_ready` 前置检查 → 调服务的 async generator
# → `_make_sse_generator` 包成 SSE 帧。业务逻辑（提示词组装、占位符注入、链条约束）
# 全在 `services/document_service.py`，本层只做参数转发。
#
# ⚠️ 这四个**不是**产物落库的路径（前台流式，请求断了就没了），
# 详见模块 docstring「四个生成接口的两处额外说明」。


@router.get("/document-plan", response_model=DocumentPlanView, summary="分片计划（不调模型）")
async def get_document_plan(kind: DocKind) -> DocumentPlanView:
    """给出某份产物的**分片计划**：分几次调用、每次生成哪一段。

    **为什么必须有这个接口**：整份接口文档（实测 17257 字符）与提示词套件（18354 字符）
    必然撞上单次输出上限被截断（`HANDOFF.md` §4 坑 #18），必须分片。
    而"分哪些片"是**服务端口径**，不能由前端自己抄一份章节清单
    （坑 #10：清单只有一份真相，就在 `gen_*.md` 里）——
    所以计划由服务端解析提示词文件得出，前端只管照着循环。

    **纯函数、无副作用、不调模型**：结果只取决于提示词文件内容，可以放心缓存/重放。
    清单解析失败会抛 500（`DocumentPlanError`）—— 刻意**不**回退成"单次整份"，
    否则产物会在没人察觉的情况下重新变成残的。
    """
    return DocumentPlanView.from_plan(build_plan(kind))


@router.post("/generate-prd-stream", summary="生成 PRD（SSE）", deprecated=True)
async def generate_prd_stream(
    payload: GeneratePrdRequest,
    service: DocumentServiceDep,
) -> StreamingResponse:
    """生成 PRD，逐段流式返回 Markdown。

    ⚠️ **已弃用（deprecated）：正式入口是 `POST /api/jobs`**（`artifact=prd`）。
    理由是需求里的「生成必须是后台任务」：本接口是**前台流式**，刷新页面就等于丢掉这一轮，
    产物也不落库（`services/job_runner.py` 才是落库 + 断连续跑的那条路）。
    保留它是为了不破坏既有回归脚本与旧调用方。

    一次调用产出一份产物。**整份 PRD 实测能一次生成完**（默认走技能包的 6 章结构、
    未截断，见 `backend/validation_out/prd_skill.txt`）。
    早先"整份 PRD 装不下单次输出上限"的判断**已被实测推翻** ——
    真正会撞上限的是**接口文档**（见 `generate-api-docs-stream`）。

    PRD 用哪套结构由服务端配置 `PRD_USE_SKILL` 决定（默认技能包 6 章；
    关掉才是 `gen_prd.md` 的 15 章 + 2 附录），**前端不传这个开关** ——
    两套结构的章节号互不兼容，让客户端选会立刻出现"界面说一套、产物另一套"。

    上游只有表单与对话：PRD 是链条的源头，没有别的输入可追溯（`HANDOFF.md` §1）。
    """
    _ensure_llm_ready(service)

    outcome = StreamOutcome()
    stream = service.generate_prd_stream(**payload.generation_kwargs(), outcome=outcome)
    return StreamingResponse(
        _make_sse_generator(stream, outcome, run_type="generate_prd"), media_type=SSE_MEDIA_TYPE, headers=SSE_HEADERS
    )


@router.post("/generate-api-docs-stream", summary="从 PRD 生成接口文档（SSE）", deprecated=True)
async def generate_api_docs_stream(
    payload: GenerateApiDocsRequest,
    service: DocumentServiceDep,
) -> StreamingResponse:
    """从**已通过审核的 PRD** 推导接口文档，逐段流式返回 Markdown。

    ⚠️ **已弃用（deprecated）：正式入口是 `POST /api/jobs`**（`artifact=api-docs`）。
    任务那条路会**按服务端的分片计划逐片生成再拼接**（本接口要靠调用方自己循环），
    而且是后台执行、断开不丢。见 `services/job_runner.py` 的模块 docstring。

    `prd_content` 必填且不能为空白（`NonBlankStr` → **422**）。这不是形式主义：
    链条约束要求每个接口都能回指到 PRD 的 FR 编号（`gen_common.md` §三份产物），
    没有 PRD 可读，模型只能凭空造接口 —— 而造出来的东西**看起来完全像真的**。
    与其生成一份不可追溯的文档，不如在边界上就拒掉。
    """
    _ensure_llm_ready(service)

    outcome = StreamOutcome()
    stream = service.generate_api_docs_stream(
        **payload.generation_kwargs(), prd_content=payload.prd_content, outcome=outcome
    )
    return StreamingResponse(
        _make_sse_generator(stream, outcome, run_type="generate_api_docs"), media_type=SSE_MEDIA_TYPE, headers=SSE_HEADERS
    )


@router.post("/generate-prompts-stream", summary="从 PRD 生成提示词套件（SSE）", deprecated=True)
async def generate_prompts_stream(
    payload: GeneratePromptsRequest,
    service: DocumentServiceDep,
) -> StreamingResponse:
    """生成提示词套件，逐段流式返回。

    ⚠️ **已弃用（deprecated）：正式入口是 `POST /api/jobs`**（`artifact=prompts`）。
    理由同 `generate-api-docs-stream`：任务那条路自带分片计划与后台执行，
    而本接口是前台流式 + 调用方自己循环。

    ⚠️ 产出是**多文件**格式，用 `=== FILE: <相对路径> ===` 分隔各文件
    （`docs/提示词套件模板.md`）—— **调用方负责按这个分隔行切分**，
    服务层与传输层都不解析它。

    `api_content` 可以不传（服务层填「（尚无）」），但 `prd_content` 必填，
    理由同 `generate-api-docs-stream`。
    """
    _ensure_llm_ready(service)

    outcome = StreamOutcome()
    stream = service.generate_prompts_stream(
        **payload.generation_kwargs(),
        prd_content=payload.prd_content,
        api_content=payload.api_content,
        outcome=outcome,
    )
    return StreamingResponse(
        _make_sse_generator(stream, outcome, run_type="generate_prompts"), media_type=SSE_MEDIA_TYPE, headers=SSE_HEADERS
    )


@router.post(
    "/optimize-document-stream",
    summary="按反馈修订文档的某一节（SSE，F8.6）",
    deprecated=True,
)
async def optimize_document_stream(
    payload: OptimizeDocumentRequest,
    service: DocumentServiceDep,
) -> StreamingResponse:
    """只修订文档的**一节**（F8.6「针对不合格项一键重生成对应章节」），逐段流式返回。

    ⚠️ **已弃用（2026-10-05）**：前端改为走 `POST /api/jobs`（
    `artifact=optimize-prd|optimize-api-docs|optimize-prompts`）+ 订阅 Job 进度 ——
    理由是这条前台流会在客户端断开时**丢掉整趟修订**（用户等了半天，一次刷新全没）。
    保留它是因为对外契约只增不减，且"按反馈改一节"的服务层实现（
    `document_service.optimize_document_stream`）仍由 Job runner 复用；
    新代码请用 Job 那条路（它还会把改好的那一节**拼回整篇**写进会话，这条不会）。

    system prompt 仍用该产物自己的 `gen_*`（含 `gen_common` 基线）—— 修订不是另一种
    文档类型，规则完全一样；变的只是 human message。

    ⚠️ `current_content`（该节现有正文）必填：只给反馈不给原文，模型会从零重写，
    **用户手改过的内容全丢** —— 而 `会话持久化方案` §7.4 恰恰要求重生成前对已手改
    内容二次确认。

    ⚠️ 修订 `kind` 为 `api` / `prompts` 时同样必须给 `prd_content`
    （`model_validator` → **422**），理由同 `generate-api-docs-stream`。

    ✅ **已用真实模型验证过（`kind=prd`）**：`deepseek-chat`，2.9 秒 / 375 个片段 /
    668 字符 / 未截断（`finish_reason=stop`）；返回正文**只含被要求的那一节**
    （第 2 章没有被连带重写），并按反馈把「谁 / 什么场景 / 现在怎么做 / 痛在哪」逐条补上，
    摘要里查不到的项一律标 `[待确认]`。
    ✅ **三个变体都用真实模型验证过**（`deepseek-chat`，三次都是 `finish_reason=stop`、
    未截断，且返回正文**只含被要求的那一节** —— 这是「修订」而不是「重生成」的关键）：

    | 变体 | 耗时 | 片段 / 字符 | 关键观察 |
    | --- | --- | --- | --- |
    | `prd` | 2.9 秒 | 375 / 668 | 没连带重写下一章；缺失项标 `[待确认]` |
    | `api` | 3.5 秒 | 559 / 1245 | 按反馈补了「PRD 来源」列，API-01 → FR-01、API-02 → FR-02，链条回指成立 |
    | `prompts` | 7.0 秒 | 964 / 1638 | 只回 `=== FILE: 05-verify.md ===` 那一个文件，没把整套重写 |
    """
    _ensure_llm_ready(service)

    outcome = StreamOutcome()
    stream = service.optimize_document_stream(**payload.optimize_kwargs(), outcome=outcome)
    return StreamingResponse(
        _make_sse_generator(stream, outcome, run_type="optimize_document"), media_type=SSE_MEDIA_TYPE, headers=SSE_HEADERS
    )
