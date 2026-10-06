r"""Generation Job 的 **runner**：把 `document_service` 的生成器跑成后台任务。

## 它在整条链路上的位置

    浏览器 ──POST /api/jobs──> api/jobs.py ──create_job──> generation_jobs 表
                                   │
                                   └── start_job(job_id) ──> asyncio.Task（**本模块**）
                                                                 │
                    document_service.generate_*（原样复用，一行没改）
                                                                 │
                       每个事件 ──┬─> 节流写库（draft_content / phase / review）
                                  └─> job_bus.publish（推给当前订阅的 SSE 连接）

**SSE 连接与任务是两条命**：连接断了只是少一个订阅者，任务照跑、照落库
（`job_bus.unsubscribe` 不碰 `asyncio.Task`）。这正是需求要的
"客户端断开 SSE 后，后台 runner 仍继续执行直至完成或失败"。

## 收尾时写两个地方，**顺序不能反**

    任务收尾（完成 / 失败 / 取消）
      ├─ 1. 文档版本层（`sessions.documents.sync_from_job`）—— 升新版或就地更新 current
      └─ 2. 会话镜像（`sessions.sync_*`）—— `session_data.documents.<kind>.content`

先版本层、后镜像（需求 §七）：反过来会让刷新后的界面上"侧栏的版本"滞后于
"编辑器里的正文"，而两者本该是同一次任务的结果。

⚠️ **失败也写**（需求 §3）：只要留下了非空的半成品，版本照样写、metadata 里标
`job_status=failed`，用户刷新后能看到写到哪了。优化任务的半成品必须是**拼回整篇**的
结果（`_optimize_partial`），不能是那一节的片段 —— 那会把整篇替换成一段话。

两条写入都是**尽力而为**（`_sync_version` / `_sync_session` 都吞异常只记日志）：
任务的权威副本在 `generation_jobs` 里，落库失败不该把一个已经跑完的任务判成失败。

## 事件协议（对外契约）

| 事件 | 载荷 | 什么时候发 |
| --- | --- | --- |
| `snapshot` | `{status, phase, artifact, draft_content, review}` | 订阅建立时第一条（由 `api/jobs.py` 发，不是这里） |
| `phase` | `{phase, round?, detail?, issues?}` | 任务进入新阶段（写 / 审 / 改写） |
| `text_delta` | `{content}` | 每一个增量片段 |
| `review` | `{content: {passed, issues, review_model, ...}}` | 审查有了结论 |
| `done` | `{artifact, content, final_prd?, review, revision_applied, truncated, finish_reason}` | 成功收尾 |
| `error` | `{message, request_id}` | 失败收尾 |
| `run_summary` | 与 `api/conversation.py` 同形（耗时 / token / 调用次数） | 收尾前（**与 conversation 一致：在 done 之前**） |
| `[DONE]` | — | 流结束（`api/jobs.py` 收到结束哨兵后发） |

## ⚠️ `done` 事件是**本模块**产出的，不是生成器给的

`document_service` 的两类生成器都不产出"终稿"事件：裸文本那条逐段吐字串，
双智能体那条的阶段事件里 `stage=done` 只是"审核流程走完了"。
所以**生成器正常返回 = 任务成功**，`done` 由 `_finish()` 组装（正文取累积的草稿）。
反过来，生成器抛异常 = 任务失败（`_fail()`）。这条映射写在代码里很容易看漏，
所以单独点出来。

## PRD 为什么走双智能体（`generate_prd_with_review_events`）

需求 §四的 phase 映射表（`writer_started` / `review_started` / `rewrite_started`）与
§十的 `review` 事件，**只有双智能体那条生成器会产出** —— 单智能体的
`generate_prd_from_summary_stream` 是裸文本流，没有任何阶段信息。
所以 PRD 任务走双智能体（写手 + 审核员），与产品当前默认的 PRD 生成路径一致
（commit 9d6c993）。

## 接口文档 / 提示词套件为什么在任务里**仍然分片**

这是本实现对需求的一处**有意偏离**，必须写清楚：

`HANDOFF.md` §4 坑 #18/#20 的实测结论是「整份接口文档（约 1.8 万字符）与提示词套件
**必然**被输出上限截断」。前台那套因此改成了"按 `GET /conversation/document-plan`
逐片调用再拼接"。如果任务里退回"一次调用生成整份"，就等于把这个已经修好的 bug
重新引回来 —— 而且这次是**后台任务**，用户拿到的是一份"看起来完整、其实缺了几章"的文档。

所以 runner 对这两份产物**按同一份计划逐片生成**（计划仍由 `services/document_plan.py`
从提示词文件解析、`services/stitch.py` 拼接），所有片段都落在同一个 `generating` 阶段里，
对外仍然是"一个任务、一份草稿、一串 `text_delta`"。`truncated` 取"任一片是否被截断"。

PRD **不分片**：整份 PRD 实测装得下（`HANDOFF.md` §2），分了只是多花时间。

> 代价（如实记下）：分片路径下 `text_delta` 推的是**原始增量**，而终稿是**拼接过**的
> （`stitch_parts` 会丢掉重复的一级标题）。所以"生成中看到的正文"与"done 里的正文"
> 可能差一个重复标题 —— 与前端的既有行为一致（它也是流式累积后再 `stitchParts`）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Mapping, Sequence
from typing import Any

from core.request_context import get_request_id
from services.document_plan import DocumentPlanError, build_plan
from services.document_service import DocumentScope, DocumentService
from services.document_version_service import DocumentVersionService
from services.job_bus import publish, publish_stream_end
from services.job_models import (
    ARTIFACT_DOC_KIND,
    ARTIFACT_RUN_TYPE,
    OPTIMIZE_CONTEXT_KEYS,
    JobRecord,
    content_artifact_of,
    is_optimize_artifact,
)
from services.job_service import JobService, should_persist
from services.llm import StreamOutcome
from services.llm_metrics import RunMetricsCollector, finalize_run, log_run_summary
from services.quality_gate import gate_error, run_quality_gate
from services.section_edit import replace_section
from services.session_service import SessionNotFound, SessionService
from services.stitch import stitch_parts

logger = logging.getLogger(__name__)

# 生成器的阶段名 → (job.phase, SSE 的 phase 事件名)
#
# ⚠️ **只有这一张表**。`document_service.generate_prd_with_review_events` 产出的
# `stage` 取值是 `writing / reviewing / rewriting / done`；需求要求 SSE 侧叫
# `writer_started / review_started / rewrite_started`，而库里存的是前者。
# 两套名字分两处写，迟早出现"库里 writing、界面显示 review_started"这种对不上的情况。
PHASE_MAP: dict[str, tuple[str, str]] = {
    "writing": ("writing", "writer_started"),
    "reviewing": ("reviewing", "review_started"),
    "rewriting": ("rewriting", "rewrite_started"),
    "done": ("done", "done"),
}

_DOC_PLAN_KINDS: dict[str, str] = {"api-docs": "api", "prompts": "prompts"}
"""要分片生成的产物 → `build_plan()` 的 kind（`DocKind` 用的是 `api`，不是 `api-docs`）。"""

# 在跑的任务（job_id → Task）。**必须持有引用**：`asyncio` 只保留任务的弱引用，
# 不存下来会被 GC 掉（任务莫名其妙停在半路，日志里什么都没有）。
_TASKS: dict[str, asyncio.Task] = {}


# ---------------------------------------------------------------- 启动 / 关停


def start_job(
    job_id: str,
    *,
    jobs: JobService,
    sessions: SessionService,
    documents: DocumentService | None = None,
) -> asyncio.Task:
    """把任务丢进后台跑并**立刻返回**（`POST /jobs` 不等生成）。

    `documents` 允许注入：自检脚本用它换成假模型，从而在不花钱的前提下验证
    "写库 / 发事件 / 断开订阅不停任务"这三件事（真模型那条路另有一次端到端验证）。
    """
    task = asyncio.create_task(
        run_job(job_id, jobs=jobs, sessions=sessions, documents=documents),
        name=f"generation-job:{job_id}",
    )
    _TASKS[job_id] = task
    task.add_done_callback(lambda _finished, jid=job_id: _TASKS.pop(jid, None))
    return task


def active_task_count() -> int:
    """还在跑的任务数（自检用；也便于运维观察）。"""
    return len(_TASKS)


def task_for(job_id: str) -> asyncio.Task | None:
    """取任务对象（自检用：断言"订阅者断开后它仍然没结束"）。"""
    return _TASKS.get(job_id)


def shutdown() -> None:
    """取消所有在跑的任务（应用关停时调用）。

    ⚠️ 收尾**不在这里做**：取消点可能落在任何一个 await 上，此时写库、发事件都不可靠
    （事件循环正在收摊）。所以这里只取消，**由下次启动的 `JobService.fail_stale_jobs()`
    统一把它们标成 failed**（需求 §十二：不自动续跑，但必须如实标记中断）。
    """
    for job_id, task in list(_TASKS.items()):
        if not task.done():
            logger.info("关停：取消生成任务 %s", job_id)
            task.cancel()


async def run_job(
    job_id: str,
    *,
    jobs: JobService,
    sessions: SessionService,
    documents: DocumentService | None = None,
) -> None:
    """跑一个任务。**任何异常都在这里收成 `status=failed`**，不让后台任务静默死掉。

    "静默死掉"是这个功能最危险的失败模式：`create_task` 抛出的异常默认只在任务结束时
    打一行 "Task exception was never retrieved"，而库里那条任务会永远停在 `running`，
    前端于是永远转圈。
    """
    service = documents or DocumentService()
    try:
        record = jobs.get_job(job_id)
    except Exception:  # noqa: BLE001 - 任务读不到（被删了 / 库坏了）：没有可收尾的对象
        logger.exception("生成任务 %s 读不到，无法启动", job_id)
        return

    # 这一趟的观测账（与 conversation.py 同一套）：start 在**任务体内**，
    # 这样 contextvar 能顺着 await 传到 document_service 里的每一次模型调用。
    run = RunMetricsCollector.start(ARTIFACT_RUN_TYPE[record.artifact])
    outcome = StreamOutcome()
    state = _DraftState()

    try:
        jobs.update_job(job_id, status="running")
        _sync_session(
            sessions,
            lambda: sessions.sync_job_started(
                session_id=record.session_id, job_id=job_id, artifact=record.artifact
            ),
            "任务开始",
        )
        await _consume(job_id, record, jobs, service, outcome, state)
    except asyncio.CancelledError:
        # 关停 / 显式取消。写库是同步的、仍然可靠；事件就不发了（事件循环在收摊）。
        jobs.update_job(job_id, status="failed", error="任务被取消（服务关停或显式取消）")
        state.flush(jobs, job_id, force=True)
        if is_optimize_artifact(record.artifact):
            # 先把 partial 算出来：版本层与镜像用的是**同一个值**，
            # 在 lambda 里各算一次会拼两遍（`_patched_document` 要重扫整篇）。
            partial = _optimize_partial(record, state.draft)
            _sync_version(sessions.documents, record, partial, job_status="failed")
            # 同 `_fail`：优化任务的草稿是"一节"，不能直接写进产物的 *Content
            _sync_session(
                sessions,
                lambda: sessions.sync_optimize_failed(
                    session_id=record.session_id,
                    job_id=job_id,
                    artifact=record.artifact,
                    partial=partial,
                ),
                "优化取消",
            )
        else:
            _sync_version(sessions.documents, record, state.draft, job_status="failed")
            _sync_session(
                sessions,
                lambda: sessions.sync_job_failed(
                    session_id=record.session_id,
                    job_id=job_id,
                    artifact=record.artifact,
                    draft=state.draft,
                ),
                "任务取消",
            )
        raise
    except Exception as exc:  # noqa: BLE001 - 任何异常都要落成任务失败
        logger.exception("生成任务 %s 失败", job_id)
        await _fail(job_id, record, jobs, sessions, run, state, exc)
        return

    content = state.final_text()
    if not content.strip():
        # 一个字都没有 = 上游什么都没返回。**不能算成功**：用户会拿到一份空的审核页，
        # 而"生成成功但内容为空"在界面上与"还没生成"长得一样。
        await _fail(
            job_id,
            record,
            jobs,
            sessions,
            run,
            state,
            ValueError("生成结束但没有任何内容（模型没有返回，或输出被截断在开头）"),
        )
        return
    if is_optimize_artifact(record.artifact):
        await _finish_optimize(job_id, record, jobs, sessions, run, outcome, state, content)
        return
    await _finish(job_id, record, jobs, sessions, service, run, outcome, state, content)


# ---------------------------------------------------------------- 消费生成器


async def _consume(
    job_id: str,
    record: JobRecord,
    jobs: JobService,
    service: DocumentService,
    outcome: StreamOutcome,
    state: _DraftState,
) -> None:
    """按产物类型跑生成器，把生成器的输出翻译成"写库 + 广播"。"""
    artifact = record.artifact
    payload = record.payload()

    if artifact == "prd":
        summary = payload.get("requirements_summary")
        if not isinstance(summary, Mapping) or not summary:
            raise ValueError("payload.requirements_summary 不能为空 —— PRD 任务的唯一输入")
        events = service.generate_prd_with_review_events(
            summary=summary,
            history=_history(payload),
            outcome=outcome,
        )
        async for kind, item in events:
            if kind == "chunk":
                await _on_delta(job_id, jobs, state, str(item))
            else:
                await _on_stage(job_id, jobs, state, item if isinstance(item, Mapping) else {})
        return

    if artifact == "api-docs":
        prd_content = payload.get("prd_content")
        if not isinstance(prd_content, str) or not prd_content.strip():
            raise ValueError("payload.prd_content 不能为空 —— 接口文档必须从 PRD 推导")
        await _consume_sharded(
            job_id,
            jobs,
            state,
            kind="api-docs",
            make_events=lambda scope: service.generate_api_docs_stream(
                form={}, prd_content=prd_content, scope=scope, outcome=outcome
            ),
            whole_events=lambda: service.generate_api_docs_stream(
                form={}, prd_content=prd_content, outcome=outcome
            ),
        )
        return

    if artifact == "prompts":
        prd_content = payload.get("prd_content")
        if not isinstance(prd_content, str) or not prd_content.strip():
            raise ValueError("payload.prd_content 不能为空 —— 提示词套件必须从 PRD 推导")
        api_content = payload.get("api_docs_content")
        api_text = api_content if isinstance(api_content, str) else None
        await _consume_sharded(
            job_id,
            jobs,
            state,
            kind="prompts",
            make_events=lambda scope: service.generate_prompts_stream(
                form={},
                prd_content=prd_content,
                api_content=api_text,
                scope=scope,
                outcome=outcome,
            ),
            whole_events=lambda: service.generate_prompts_stream(
                form={}, prd_content=prd_content, api_content=api_text, outcome=outcome
            ),
        )
        return

    if is_optimize_artifact(artifact):
        await _consume_optimize(job_id, jobs, service, outcome, state, artifact, payload)
        return

    raise ValueError(f"未知产物类型：{artifact!r} —— 数据库 CHECK 本应先拦住它")


async def _consume_optimize(
    job_id: str,
    jobs: JobService,
    service: DocumentService,
    outcome: StreamOutcome,
    state: _DraftState,
    artifact: str,
    payload: Mapping[str, Any],
) -> None:
    """**按用户指令重写一节**（F8.6）：跑 `optimize_document_stream`，逐段广播。

    ⚠️ 与生成路径最要紧的差别：**这里累积的是"那一节"，不是整篇**。
    模型只回一节（`generate_scope` 被强制对齐到 `section`），而界面与快照要整篇 ——
    拼接发生在收尾（`_finish_optimize`），所以 `draft_content` 存的是节的原文。
    前端拿 `snapshot.section` + 这个原文 + 会话里的整篇自己拼出来显示，语义是一致的。
    """
    doc_type = payload.get("doc_type")
    section = payload.get("section")
    current_content = payload.get("current_content")
    instruction = payload.get("instruction")
    for name, value in (
        ("doc_type", doc_type),
        ("section", section),
        ("current_content", current_content),
        ("instruction", instruction),
    ):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"payload.{name} 不能为空 —— 优化任务必须知道改哪一节、按什么改")

    context = payload.get("context")
    kwargs = dict(context) if isinstance(context, Mapping) else {}
    # 白名单化：`context` 是原样透传给服务层的，多一个键就会变成
    # `TypeError: unexpected keyword argument`（在后台协程里炸，排查很远）。
    unknown = sorted(set(kwargs) - set(OPTIMIZE_CONTEXT_KEYS))
    if unknown:
        logger.warning("优化任务 %s 丢弃了 context 里不认识的键：%s", job_id, "、".join(unknown))
    for key in unknown:
        kwargs.pop(key, None)

    async for chunk in service.optimize_document_stream(
        kind=ARTIFACT_DOC_KIND[artifact],
        section=section,
        feedback=instruction,
        current_content=current_content,
        outcome=outcome,
        **kwargs,
    ):
        await _on_delta(job_id, jobs, state, chunk)


async def _consume_sharded(
    job_id: str,
    jobs: JobService,
    state: _DraftState,
    *,
    kind: str,
    make_events: Any,
    whole_events: Any,
) -> None:
    """接口文档 / 提示词套件的逐片生成（理由见模块 docstring）。

    计划解析失败时**回落到整份单次**并记 warning：那种情况下产物很可能被截断，
    但 `outcome` 会把 `truncated=True` 带进 `done`，用户至少看得见，
    而不是被一份"看起来完整"的残文档蒙过去。
    """
    try:
        plan = build_plan(_DOC_PLAN_KINDS[kind])
    except DocumentPlanError as exc:
        logger.warning(
            "分片计划解析失败（%s），本次按整份单次生成 —— 产物可能被输出上限截断：%s",
            kind,
            exc,
        )
        async for chunk in whole_events():
            await _on_delta(job_id, jobs, state, chunk)
        return

    state.stitch = True
    for part in plan.parts:
        state.begin_piece()
        scope = DocumentScope(outline=part.outline, scope=part.scope, spec=part.spec)
        async for chunk in make_events(scope):
            await _on_delta(job_id, jobs, state, chunk)
        state.end_piece()
        logger.info(
            "生成任务 %s 的第 %d/%d 片完成（%s）",
            job_id,
            part.index,
            len(plan.parts),
            part.label,
        )


async def _on_delta(job_id: str, jobs: JobService, state: _DraftState, chunk: str) -> None:
    """一个增量片段：先进内存、按节流落库，然后**立刻**广播。

    ⚠️ 广播不节流：节流是为了少写库（SQLite 的写锁是库级的），
    而 SSE 的"逐字出现"是用户等待时唯一的反馈 —— 攒 500ms 再发会让它一格一格地跳。
    """
    if not chunk:
        return
    state.add(chunk)
    state.persist_if_due(jobs, job_id)
    await publish(job_id, {"type": "text_delta", "content": chunk})


async def _on_stage(
    job_id: str, jobs: JobService, state: _DraftState, stage: Mapping[str, Any]
) -> None:
    """一个阶段事件：改 `phase`（**立即落库**），必要时把初稿挪进 `previous_draft`。

    阶段变更必须立刻写库：刷新后 `snapshot` 要能告诉用户"现在在审第 1 稿" ——
    那正是"任务化"相对"前台流式"多出来的东西（后者刷新即丢）。
    """
    name = str(stage.get("stage") or "")
    mapped = PHASE_MAP.get(name)
    if mapped is None:
        logger.warning("生成任务 %s 收到不认识的阶段：%r（已忽略）", job_id, name)
        return
    job_phase, sse_phase = mapped

    fields: dict[str, Any] = {"phase": job_phase}
    if name == "rewriting":
        # 改写前把 v1 挪进 previous_draft 并清空草稿：不清的话两稿会拼在一起，
        # 而拼起来的文档"看起来"是完整的（没有语法错误，只是前后矛盾）。
        fields["previous_draft"] = state.draft
        state.reset_draft()

    review = _review_from_stage(stage)
    if review is not None:
        fields["review_json"] = json.dumps(review, ensure_ascii=False)
        state.review = review
    state.flush(jobs, job_id, extra=fields, force=True)

    event: dict[str, Any] = {"type": "phase", "phase": sse_phase}
    for key in ("round", "detail"):
        if stage.get(key) is not None:
            event[key] = stage[key]
    if review is not None:
        # 先发 review 再发 phase：phase 里要带 issues（"在改什么"），
        # 而 issues 就是 review 的结论 —— 顺序反了的话前端可能先拿到 phase 再补 review。
        await publish(job_id, {"type": "review", "content": review})
        event["issues"] = review.get("issues", [])
    await publish(job_id, event)


#: 生成后需要**机器审查**的产物（05 篇）。PRD 不在这里：它那条是"写 → 审 → 改"的回路，
#: 审查已经嵌在 `generate_prd_with_review_events` 里了，不是收尾时才补一次。
_REVIEWED_ARTIFACTS = frozenset({"api-docs", "prompts"})


async def _review_generated_document(
    job_id: str,
    record: JobRecord,
    jobs: JobService,
    service: DocumentService,
    state: _DraftState,
    content: str,
) -> dict[str, Any]:
    """对**刚生成好的整篇文档**审一次，返回归一化结论（**一定会返回**）。

    与 PRD 那条回路的三处不同：
    1. **审完就完了，不 Rewrite**（产品决定）：下游产物的返工成本比 PRD 低，
       而且自动改写会让用户失去"到底改了什么"的控制权；
    2. **审的是拼好的整篇**：接口文档 / 提示词是多分片生成的，只有收尾时才有整篇
       （所以在 runner 收尾、不在 `document_service` 的流里）；
    3. 失败不抛：`service.review_api_docs()` 内部已经把异常降级成 `review_skipped`，
       这里只负责把结论**发出去 + 存下来**。

    ⚠️ 阶段事件必须**先发**：审查是一次没有正文输出的模型调用，不发 `review_started`
    的话，用户在那一二十秒里看到一个完全没动静的进度条（实测踩过：以为卡死了）。
    """
    payload = record.payload()
    prd_content = str(payload.get("prd_content") or "")

    db_phase, sse_phase = PHASE_MAP["reviewing"]
    state.flush(jobs, job_id, extra={"phase": db_phase}, force=True)
    await publish(job_id, {"type": "phase", "phase": sse_phase})

    if record.artifact == "api-docs":
        review = await service.review_api_docs(prd_content=prd_content, content=content)
    else:
        api_content = payload.get("api_docs_content")
        review = await service.review_prompts(
            prd_content=prd_content,
            content=content,
            api_docs_content=api_content if isinstance(api_content, str) else "",
        )

    state.review = review
    state.flush(
        jobs, job_id, extra={"review_json": json.dumps(review, ensure_ascii=False)}, force=True
    )
    await publish(job_id, {"type": "review", "content": review})
    return review


def _review_from_stage(stage: Mapping[str, Any]) -> dict[str, Any] | None:
    """从阶段事件里抠出**审查结论**（`review` 事件载荷 / 库里的 `review_json`）。

    - `rewriting`：上一轮审核发现了问题 → `passed=false`，issues 就是它列的那些；
    - `done`：终局结论 → `passed = 没有问题`，并带上审核模型名与"审核是否被跳过"。
      审核被跳过（模型给的 JSON 解析不了 → `review_skipped=True`）时 `passed` 仍是 true，
      但**必须把 skipped 标出来** —— 否则界面上的"通过"看起来像真的审过了
      （`document_service._review_prd` 的既有约定）。
    """
    name = str(stage.get("stage") or "")
    if name not in ("rewriting", "done"):
        return None
    issues = stage.get("issues")
    issues = issues if isinstance(issues, list) else []
    return {
        "passed": not issues,
        "issues": issues,
        "round": stage.get("round"),
        "review_model": stage.get("review_model"),
        "review_skipped": bool(stage.get("review_skipped", False)),
    }


def _history(payload: Mapping[str, Any]) -> list[dict[str, str]]:
    """把 payload 里的对话历史转成生成器要的 `[{role, content}]`。

    只认 `role` / `content`：多出来的键（前端的 `id` / `stage` / `kind`）对提示词没有意义，
    带进去只会让"这次到底喂了什么"更难读。
    """
    raw = payload.get("conversation_messages")
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return []
    history: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        role = item.get("role")
        content = item.get("content")
        if isinstance(role, str) and isinstance(content, str):
            history.append({"role": role, "content": content})
    return history


# ---------------------------------------------------------------- 收尾


async def _finish(
    job_id: str,
    record: JobRecord,
    jobs: JobService,
    sessions: SessionService,
    service: DocumentService,
    run: Any,
    outcome: StreamOutcome,
    state: _DraftState,
    content: str,
) -> None:
    """成功收尾：落终稿、发 `run_summary` → `done` → 结束哨兵，并同步会话。"""
    review = state.review
    revision_applied = state.rewrites > 0

    # ---------- 05 篇：接口文档 / 提示词套件的**生成后审查**（一次，不 Rewrite）----------
    # 放在 flush(phase=done) **之前**：审查是这一趟的一部分，用户应当先看到"正在审查"，
    # 再看到 done。反过来（先 done 再审查）会出现"已完成的任务又跳回审查中"。
    if record.artifact in ("api-docs", "prompts"):
        review = await _review_generated_document(job_id, record, jobs, service, state, content)

    state.flush(jobs, job_id, extra={"phase": "done"}, force=True)
    if revision_applied:
        # 让 run_summary 也带上"这趟改写过了"（否则只能从 done 的字段里看）
        run.mark_revision(True)
    summary_payload = _finalize(run)
    # 结构校验针对**最终正文**（PRD 走到这里时 rewrite 已经做完，工单 §七 要求 gate 看最终稿）
    gate = _quality_gate_for(record, content)
    # 上游版本快照：接口文档/提示词套件记下它们基于哪一版 PRD（05 篇的过期提示靠它）
    derived = _derived_from(sessions, record)

    done_payload: dict[str, Any] = {
        "type": "done",
        "artifact": record.artifact,
        "content": content,
        "review": review,
        "revision_applied": revision_applied,
        "truncated": outcome.truncated,
        "finish_reason": outcome.finish_reason,
    }
    if record.artifact == "prd":
        # 需求 §十 点名 PRD 的 done 带 `final_prd`。与 `content` 是同一个值 ——
        # 保留两个键是因为对外契约已经写成那样了，改名会让前端少一个字段而不报错。
        done_payload["final_prd"] = content

    result = {
        "content": content,
        "review": review,
        "revision_applied": revision_applied,
        "truncated": outcome.truncated,
        "finish_reason": outcome.finish_reason,
        "run_summary": summary_payload,
    }
    if gate is not None:
        # 只进**落库的结果**（`GET /api/jobs/{id}` 能查到），不进 SSE 的 `done` 帧：
        # 界面上的质量报告是从版本 metadata 读的，动 SSE 契约没有收益。
        result["quality_gate"] = gate
    if derived is not None:
        result["derived_from"] = derived
    jobs.update_job(
        job_id,
        status="completed",
        phase="done",
        draft_content=content,
        result_json=json.dumps(result, ensure_ascii=False),
    )
    # 需求 §七 的顺序：**先版本层，再镜像**。反过来会让刷新后的界面上"侧栏的版本"
    # 滞后于"编辑器里的正文"，而两者本该是同一次任务的结果。
    _sync_version(
        sessions.documents,
        record,
        content,
        review=review,
        summary=summary_payload,
        quality_gate=gate,
        derived_from=derived,
    )
    _sync_session(
        sessions,
        lambda: sessions.sync_job_completed(
            session_id=record.session_id,
            job_id=job_id,
            artifact=record.artifact,
            content=content,
            review=review,
            truncated=outcome.truncated,
        ),
        "任务完成",
    )
    await publish(job_id, {"type": "run_summary", **summary_payload})
    await publish(job_id, done_payload)
    publish_stream_end(job_id)
    logger.info(
        "生成任务 %s 完成：artifact=%s｜%d 字符｜truncated=%s｜改写=%s",
        job_id,
        record.artifact,
        len(content),
        outcome.truncated,
        revision_applied,
    )


async def _fail(
    job_id: str,
    record: JobRecord,
    jobs: JobService,
    sessions: SessionService,
    run: Any,
    state: _DraftState,
    exc: Exception,
) -> None:
    """失败收尾：`status=failed` + `error`，**草稿留给用户**，账照记。

    为什么失败也发 `run_summary`：`conversation.py` 的失败路径就是这么做的
    （"失败也交账：否则出错那趟调了几次模型就查不到"）。任务化之后这一点更重要 ——
    用户只看到一条错误，而"这次调了几次模型、跑了多久"只能从这里看。
    """
    message = f"{type(exc).__name__}: {exc}"[:500]
    state.flush(jobs, job_id, force=True)
    summary_payload = _finalize(run)
    # 优化失败时"草稿"是**一节的片段**：直接写进产物的 *Content 会把整篇替换成一段话。
    # 能拼就拼回整篇（用户至少拿到写了一半的那一节），拼不了就一个字都不动。
    partial = _optimize_partial(record, state.draft) if is_optimize_artifact(record.artifact) else None
    jobs.update_job(
        job_id,
        status="failed",
        error=message,
        result_json=json.dumps(
            {"content": partial or state.draft, "run_summary": summary_payload},
            ensure_ascii=False,
        ),
    )
    # 需求 §3：失败但留下了半成品时**也写版本层**（metadata 带 `job_status=failed`）。
    # 优化路径用拼好的整篇（`partial`，可能为 None）；生成路径用原始草稿。
    failed_content = partial if is_optimize_artifact(record.artifact) else state.draft
    # ⚠️ 失败路径**也跑**结构校验：这一版就是用户会看到的 current（半成品），
    # 报告要说的是"你眼前这份达标没有"。留上一版的绿灯反而是撒谎。
    _sync_version(
        sessions.documents,
        record,
        failed_content,
        summary=summary_payload,
        quality_gate=_quality_gate_for(record, failed_content),
        job_status="failed",
    )
    if is_optimize_artifact(record.artifact):
        _sync_session(
            sessions,
            lambda: sessions.sync_optimize_failed(
                session_id=record.session_id,
                job_id=job_id,
                artifact=record.artifact,
                partial=partial,
            ),
            "优化失败",
        )
    else:
        _sync_session(
            sessions,
            lambda: sessions.sync_job_failed(
                session_id=record.session_id,
                job_id=job_id,
                artifact=record.artifact,
                draft=state.draft,
            ),
            "任务失败",
        )
    await publish(job_id, {"type": "error", "message": message, "request_id": get_request_id()})
    await publish(job_id, {"type": "run_summary", **summary_payload})
    publish_stream_end(job_id)
    logger.warning("生成任务 %s 失败：%s", job_id, message)


def _finalize(run: Any) -> dict[str, Any]:
    """收尾观测账并打日志，返回给 SSE 用的简报（与 conversation.py 同口径）。"""
    summary = finalize_run(run)
    log_run_summary(summary)
    return summary.to_dict(brief=True)


# ---------------------------------------------------------------- 优化任务的收尾


def _patched_document(record: JobRecord, section_text: str) -> str:
    """把模型给出的**那一节**拼回整篇，返回整篇。

    ⚠️ 这是优化任务与生成任务最本质的差别所在，也是**唯一**会把节片段写进
    `documents.*.content` 的地方 —— 直接写片段等于把用户的文档替换成一段话。

    `document_content`（优化前的整篇）缺失时退化成"只用这一节"：宁可显示一节
    （并照常报错/记日志），也不要拿半份去覆盖整篇。**但这条退化路径不该出现** ——
    payload 校验要求它是必填项。
    """
    payload = record.payload()
    document_content = payload.get("document_content")
    section = payload.get("section")
    if not isinstance(document_content, str) or not document_content.strip():
        logger.warning(
            "优化任务 %s 的 payload 里没有 document_content，只能返回这一节（不该发生）",
            record.id,
        )
        return section_text
    if not isinstance(section, str) or not section.strip():
        return section_text
    patched = replace_section(document_content, section, section_text)
    if patched == document_content and section_text.strip():
        # 标题没匹配上（模型给的 section 与正文那行不一致 / 那节已被删）：
        # 如实记一条，否则用户会以为"优化了但看不出变化"是模型的问题。
        logger.warning(
            "优化任务 %s 的 section 没在正文里逐字匹配到（%r），本次不修改整篇", record.id, section
        )
    return patched


async def _finish_optimize(
    job_id: str,
    record: JobRecord,
    jobs: JobService,
    sessions: SessionService,
    run: Any,
    outcome: StreamOutcome,
    state: _DraftState,
    section_text: str,
) -> None:
    """优化成功收尾：拼回整篇 → 落终稿 → 发 `run_summary` / `done` → 同步会话。

    与 `_finish` 的三处不同（都是刻意的）：

    1. **`draft_content` 写拼好的整篇**：任务结束了，草稿的含义就应该是"这份文档现在长什么样"
       （跑到一半时它是节的原文，见 `_consume_optimize` 的说明）；
    2. **不写 `truncated`**：那个标记说的是"**整篇**正文被截断过"，而优化只重写一节 ——
       优化成功不代表末尾补全了（与 `App.tsx` 里旧的优化路径同一条取舍）；
    3. **`result` 里带 `section`**：重连的客户端要靠它把"节的原文"拼回整篇。
    """
    content = _patched_document(record, section_text)
    state.flush(jobs, job_id, extra={"phase": "done"}, force=True)
    summary_payload = _finalize(run)
    section = record.payload().get("section")
    # 优化也要跑：gate 描述的是"眼前这份正文"，优化改了它就等于换了正文（工单 §5 末条）
    gate = _quality_gate_for(record, content)

    optimize_result: dict[str, Any] = {
        "content": content,
        "section": section if isinstance(section, str) else None,
        "truncated": outcome.truncated,
        "finish_reason": outcome.finish_reason,
        "run_summary": summary_payload,
    }
    if gate is not None:
        optimize_result["quality_gate"] = gate

    jobs.update_job(
        job_id,
        status="completed",
        phase="done",
        draft_content=content,
        result_json=json.dumps(optimize_result, ensure_ascii=False),
    )
    _sync_version(sessions.documents, record, content, summary=summary_payload, quality_gate=gate)
    _sync_session(
        sessions,
        lambda: sessions.sync_optimize_completed(
            session_id=record.session_id,
            job_id=job_id,
            artifact=record.artifact,
            content=content,
        ),
        "优化完成",
    )
    await publish(
        job_id,
        {
            "type": "run_summary",
            **summary_payload,
        },
    )
    await publish(
        job_id,
        {
            "type": "done",
            "artifact": record.artifact,
            "content": content,
            "section": section if isinstance(section, str) else None,
            "truncated": outcome.truncated,
            "finish_reason": outcome.finish_reason,
        },
    )
    publish_stream_end(job_id)
    logger.info(
        "优化任务 %s 完成：artifact=%s｜整篇 %d 字符｜本节 %d 字符",
        job_id,
        record.artifact,
        len(content),
        len(section_text),
    )


def _optimize_partial(record: JobRecord, section_text: str) -> str | None:
    """失败时能拿到什么：把**已经收到的这一节**拼回整篇（拿不到就 `None`）。

    为什么不直接退回"什么都不写"：用户等了一分钟，模型可能已经写完了这一节的大半 ——
    把这一节的半成品拼回整篇，是他等待的成果里唯一有价值的部分。
    为什么不退回"把节片段写进 *Content"：那会把整篇文档替换成一段话（数据丢失）。
    """
    if not section_text.strip():
        return None
    payload = record.payload()
    if not isinstance(payload.get("document_content"), str):
        return None
    return _patched_document(record, section_text)


def _sync_version(
    versions: DocumentVersionService,
    record: JobRecord,
    content: str | None,
    *,
    review: Mapping[str, Any] | None = None,
    summary: Mapping[str, Any] | None = None,
    quality_gate: Mapping[str, Any] | None = None,
    derived_from: Mapping[str, Any] | None = None,
    job_status: str | None = None,
) -> None:
    """把这次任务的产物写进**文档版本层**（需求 §3）。**尽力而为**。

    与 `_sync_session` 同一条理由：任务的权威副本在 `generation_jobs` 里，
    版本层是本次新加的对外能力 —— 它出问题不该把一个已经跑完的任务判成失败，
    也不该让用户看到"生成成功"却什么都没有（草稿仍在 job 表与会话镜像里）。

    `content` 为空时**什么都不做**：版本层也不接受空正文，与其让它抛一下再被吞掉，
    不如在这里就说清楚。

    ⚠️ 优化任务的 `content` 必须是**拼好的整篇**（`_patched_document` 的产物），
    **不是那一节的片段** —— 传片段会把整篇文档替换成一段话。

    ⚠️ 失败路径也走它（`job_status="failed"`）：需求 §3 要求"失败但留下了半成品"时
    仍然写版本，metadata 里标明这一版是残的，用户刷新后能看到写到哪了。

    依赖从 `sessions.documents` 取（不是另开一个参数）：`SessionService` 持有版本层，
    而 `start_job` 再穿一个参数下去会让自检脚本落进默认库（见 `session_service.__init__`）。
    """
    if not content or not content.strip():
        return
    try:
        versions.sync_from_job(
            session_id=record.session_id,
            artifact=record.artifact,
            content=content,
            job_id=record.id,
            review=review,
            run_summary=summary,
            quality_gate=quality_gate,
            derived_from=derived_from,
            job_status=job_status,
        )
    except Exception:  # noqa: BLE001 - 版本层失败不该毁掉已经跑完的任务
        logger.exception("任务 %s 写入文档版本层失败（job 表与产物本身不受影响）", record.id)


# ---------------------------------------------------------------- 结构校验（04 篇）

#: 需要跑结构校验的产物。取值就是版本层的 `doc_type` —— 而 `content_artifact_of()`
#: 给出来的正是它（`optimize-api-docs` → `api-docs`），所以**不另写一张映射表**。
_GATE_DOC_TYPES = frozenset({"prd", "api-docs", "prompts"})


#: 有**上游文档**的产物：接口文档来自 PRD，提示词套件来自 PRD（+ 可选的接口文档）。
#: PRD 自己从结构化摘要生成，没有文档级上游，所以不在这里。
_DERIVED_DOC_TYPES = frozenset({"api-docs", "prompts"})


def _derived_from(sessions: SessionService, record: JobRecord) -> dict[str, Any] | None:
    """上游版本快照（05 篇）—— 写进版本 metadata，用来回答"这份产物基于哪一版 PRD"。

    PRD 之后升版了，界面据此提示"接口文档可能已过期"（**只提示，不阻断**）。
    没有它的话，用户只能凭记忆判断"我这份接口文档是不是对着旧 PRD 生成的"。

    ⚠️ 取不到上游版本时返回 `None` 而不是写一个空壳：`derived_from` 存在就意味着
    "有可比的上游版本号"，写个 `prd_version_no=None` 的壳会让过期判断算不出结论，
    还让界面误以为"这份产物有追溯信息"。
    """
    doc_type = content_artifact_of(record.artifact)
    if doc_type not in _DERIVED_DOC_TYPES:
        return None

    snapshot: dict[str, Any] = {}
    prd = sessions.documents.get_current_version(record.session_id, "prd")
    if prd is not None:
        snapshot["prd_version_id"] = prd.id
        snapshot["prd_version_no"] = prd.version_no

    if doc_type == "prompts":
        api_content = record.payload().get("api_docs_content")
        if isinstance(api_content, str) and api_content.strip():
            api_docs = sessions.documents.get_current_version(record.session_id, "api-docs")
            if api_docs is not None:
                snapshot["api_docs_version_id"] = api_docs.id
                snapshot["api_docs_version_no"] = api_docs.version_no
        else:
            # 用户跳过接口文档直接生成提示词：**明确记下来**。导出交付包的 manifest
            # 要说清"这份提示词没有对应的接口文档"，而不是留一个查不出原因的空白。
            snapshot["api_docs_skipped"] = True

    return snapshot or None


def _gate_context(record: JobRecord) -> dict[str, Any]:
    """喂给结构校验的上下文。**只有提示词套件用得上**（要与 PRD 的功能数对齐）。

    ⚠️ 传**整份 PRD**而不是片段：gate 里只做行扫描（数 MVP 表有几行），
    不做 AST 解析、不进 LLM，成本可以忽略；截断反而会让"数不出功能数"退化成 skip，
    白丢一条检查。
    """
    prd_content = record.payload().get("prd_content")
    return {"prd_content": prd_content} if isinstance(prd_content, str) else {}


def _quality_gate_for(record: JobRecord, content: str | None) -> dict[str, Any] | None:
    """对**最终正文**跑一次结构校验，返回可写进 `metadata.quality_gate` 的字典。

    ⚠️ **绝不抛错**（04 篇 §9）：校验器挂了不能把一个已经跑完的任务判成失败，
    所以 catch 之后写一条 `gate.error`（`passed=False` —— 跑挂的检查器不替产物背书）。

    空正文返回 `None`（不写）：`_sync_version` 对空内容本来就什么都不做，
    这里再写一份"空内容不达标"的报告只是噪声。
    """
    doc_type = content_artifact_of(record.artifact)
    if doc_type not in _GATE_DOC_TYPES:
        logger.warning("产物 %s 没有对应的结构校验规则（doc_type=%s），跳过", record.artifact, doc_type)
        return None
    if not content or not content.strip():
        return None
    try:
        return run_quality_gate(doc_type, content, context=_gate_context(record))
    except Exception as exc:  # noqa: BLE001 - 见 docstring
        logger.exception("任务 %s 的结构校验执行失败（记一条 gate.error，产物不受影响）", record.id)
        return gate_error(exc)


def _sync_session(sessions: SessionService, action: Any, label: str) -> None:
    """会话同步是**尽力而为**：失败只记日志，绝不影响任务本身。

    理由：任务是产物的权威来源（草稿与终稿都在 `generation_jobs` 里），会话快照只是
    "用户做到哪"。用户中途删掉会话是完全合法的操作 —— 那时同步失败（`SessionNotFound`）
    不该把一个已经写好的产物判成失败。
    """
    try:
        action()
    except SessionNotFound:
        logger.warning("%s时会话已不存在，跳过会话同步", label)
    except Exception:  # noqa: BLE001 - 会话同步的意外错误同样不该毁掉产物
        logger.exception("%s时同步会话失败（产物本身不受影响）", label)


# ---------------------------------------------------------------- 草稿状态


class _DraftState:
    """一次任务的草稿与节流状态（**内存里的那半份**，权威副本在库里）。

    为什么要有这个类：`draft`（全文）、`pieces`（分片的各片）、`last_persist_at`（节流基准）
    必须一起变，散在 runner 的局部变量里迟早出现"清了这一段忘了落库"这类错位 ——
    而症状是产物少一段，很难查。
    """

    def __init__(self) -> None:
        self.draft: str = ""
        """**原始增量拼接**。PRD 的终稿就是它；分片路径下它是"可读的实时前缀"。"""
        self.pieces: list[str] = []
        """分片路径下**已完成的各片**（收尾时交给 `stitch_parts` 规整）。"""
        self.stitch: bool = False
        """收尾时是否要用 `stitch_parts` 把各片拼起来（只有分片路径才是 True）。"""
        self.review: dict[str, Any] | None = None
        self.rewrites: int = 0
        self.last_persist_at: float = 0.0
        self._current: str = ""

    # ---- 片段与全文

    def begin_piece(self) -> None:
        """开始新的一片（分片路径）。"""
        self._current = ""

    def end_piece(self) -> None:
        """结束一片（分片路径）。"""
        self.pieces.append(self._current)
        self._current = ""

    def add(self, chunk: str) -> None:
        """收一个增量：全文与当前片都要长（全文是给"刷新后看前缀"用的）。"""
        self.draft += chunk
        self._current += chunk

    def reset_draft(self) -> None:
        """改写开始：清空全文与当前片（v1 已由调用方挪进 `previous_draft`）。"""
        self.draft = ""
        self._current = ""
        self.rewrites += 1

    def final_text(self) -> str:
        """终稿。分片路径用 `stitch_parts` 规整（丢空片、去重复一级标题）。"""
        if not self.stitch:
            return self.draft
        pieces = [*self.pieces, self._current] if self._current else list(self.pieces)
        return stitch_parts(pieces)

    # ---- 落库

    def persist_if_due(self, jobs: JobService, job_id: str) -> None:
        self.flush(jobs, job_id)

    def flush(
        self,
        jobs: JobService,
        job_id: str,
        *,
        extra: Mapping[str, Any] | None = None,
        force: bool = False,
    ) -> None:
        """把当前全文写库（可带其它列）。`force=True` 用于阶段变更 / 收尾 / 取消。

        ⚠️ 分片路径下 `draft` 是**原始拼接**，不是 `final_text()`：那样才能在生成中
        便宜地持续落库（`stitch_parts` 每次都要重扫全文）。终稿在 `_finish` 里单独写一次。
        """
        now = time.perf_counter()
        if not force and not should_persist(self.last_persist_at, now):
            return
        jobs.update_job(job_id, draft_content=self.draft, **(dict(extra) if extra else {}))
        self.last_persist_at = now


__all__ = [
    "PHASE_MAP",
    "active_task_count",
    "run_job",
    "shutdown",
    "start_job",
    "task_for",
]
