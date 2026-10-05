"""Generation Job 路由（3 条）：创建 / 查快照 / 订阅进度。

## 路径为什么是 `/api/jobs/*` 而不是 `/api/v1/jobs/*`

与 `api/session.py` 同一条理由：**需求把路径定成这样**，本模块照办。
router 自带 `/api/jobs` 前缀，`main.py` 里**不再加** `/api/v1`。

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| POST | `/api/jobs` | 创建任务（**立刻返回 job_id**，不等生成） |
| GET | `/api/jobs/{id}` | 查快照（刷新时先调它，页面不空白） |
| GET | `/api/jobs/{id}/stream` | 订阅进度（SSE，GET 无 body） |

## 本层只做三件事

1. **校验入参**（Pydantic 形状 + 业务存在性）：形状在 `api/schemas.py`，
   "会话必须已存在"在这里判（→ 404），"同产物已有在跑的任务"由服务层抛
   `DuplicateRunningJob`（→ 409，由 `main.py` 注册的处理器统一转换）。
2. **把生成丢进后台**：`job_runner.start_job(...)` —— `POST` **不等生成结束**。
   这是整个功能的核心：请求返回后任务仍在跑，浏览器断开也不影响它。
3. **把队列里的事件翻译成 SSE 帧**（见下面 `_job_stream`）。

## ⚠️ 为什么订阅是 GET，而创建是 POST

需求点名 `GET /jobs/{id}/stream`。GET 的好处是**可以刷新页面直接重连**（同一个 URL），
坏处是不能带请求体 —— 但订阅本来就不需要参数：任务的输入早在创建时落库了。
（这也是它与 `/conversation/*-stream` 那套 POST + SSE 的根本区别：那套必须带上下文，
因为它无状态；任务是有状态的，上下文在库里。）

## SSE 帧的复用

帧组装（`_sse_event`）与响应头直接**复用 `api/conversation.py` 的实现**，
不在这里再写一份：SSE 的转义规则（`ensure_ascii=False` 让 data 段永远只有一行）
写错一次就是"中文变 `\\uXXXX`"或"帧被拆断"，而 `smoke_check.py` 直接对那一处做源码计数断言。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Mapping
from typing import Any

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import StreamingResponse

from api.conversation import (
    SSE_HEADERS,
    SSE_MEDIA_TYPE,
    _ensure_llm_ready,
    _sse_event,
)
from api.deps import DocumentServiceDep, JobServiceDep, SessionServiceDep
from api.schemas import CreateJobRequest, CreateJobResponse, JobSnapshotView
from services.job_bus import STREAM_END_TYPE, subscription
from services.job_models import TERMINAL_STATUSES, JobSnapshot
from services.job_runner import start_job
from services.session_service import SessionNotFound

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/jobs", tags=["jobs"])


@router.post("", response_model=CreateJobResponse, summary="创建生成任务（立刻返回）")
async def create_job(
    payload: CreateJobRequest,
    jobs: JobServiceDep,
    sessions: SessionServiceDep,
    documents: DocumentServiceDep,
) -> CreateJobResponse:
    """登记一个后台生成任务，**立刻**返回 `job_id`。

    四道前置检查，全部在创建**之前**完成：

    1. **会话必须存在** → 否则任务无从同步进度，且 `GET /api/jobs/{id}` 拿到的
       任务会指向一个不存在的会话（`SessionNotFound` → 404）；
    2. **缺 Key 直接 503**（`_ensure_llm_ready`）：与 `/conversation/*-stream` 同一约定
       （F6.5）。不这么做的后果是"创建成功、任务立刻失败"，
       用户拿到一个 job_id 和一条 error，比当场被拒更难查；
    3. **同会话 + 同产物已有在跑的任务 → 409**（`DuplicateRunningJob`，服务层判定）；
    4. payload 的必需键（`CreateJobRequest` 的校验器 → 422）。

    ⚠️ 这里**不创建 SSE 流**：创建与订阅是两条独立的请求。这样刷新页面（丢掉订阅）
    不会影响任务，重连只需要再发一次 `GET .../stream`。
    """
    try:
        sessions.get_session(payload.session_id)
    except SessionNotFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    _ensure_llm_ready(documents)

    job_id = jobs.create_job(payload.session_id, payload.artifact, payload.payload)
    # 丢进后台：**本请求不等它**。任务在库里的状态才是权威（前端靠 GET /stream 追）。
    start_job(job_id, jobs=jobs, sessions=sessions, documents=documents)
    logger.info(
        "已受理生成任务 job=%s session=%s artifact=%s",
        job_id,
        payload.session_id,
        payload.artifact,
    )
    return CreateJobResponse(job_id=job_id)


@router.get("/{job_id}", response_model=JobSnapshotView, summary="查任务快照")
async def get_job(job_id: str, jobs: JobServiceDep) -> JobSnapshotView:
    """任务的当前快照。**不存在 → 404**（`JobNotFound` 由 `main.py` 注册的处理器转换）。

    刷新页面时的正确顺序是：先 `GET /api/session/{id}` 拿到 `activeJobId`，
    再 `GET /api/jobs/{activeJobId}` 把草稿填上，最后订阅 `stream` ——
    所以这个接口必须在**订阅之前**就能给出全文草稿，页面才不会空白一下。
    """
    return JobSnapshotView.from_snapshot(jobs.get_snapshot(job_id))


@router.get("/{job_id}/stream", summary="订阅任务进度（SSE）")
async def stream_job(job_id: str, jobs: JobServiceDep) -> StreamingResponse:
    """订阅这个任务的进度事件，直到它收尾（或客户端自己断开）。

    **断开连接不会取消任务**：本函数只 `unsubscribe`，后台那个 `asyncio.Task`
    与这条 HTTP 请求没有任何关系（`services/job_runner.py` 里创建的）。

    三条路径：

    - 任务**已收尾**（completed / failed / cancelled）：直接把库里存下的结论重放一遍
      （`run_summary` 也从 `result_json` 里取），然后 `[DONE]`。
      这样"生成完了再打开页面"与"从头看着它生成"拿到的是同一套事件；
    - 任务**在跑 / 刚创建**：先发 `snapshot`（把已生成的全文交给它，页面立刻有内容），
      再转发 `job_bus` 上的后续事件；
    - 任务**不存在**：404（在开流之前，不会变成"200 + 流里一个 error"）。
    """
    # 先取一次只为了**开流前的资源校验**（不存在 → 404，符合"入参/资源校验必须在开流前"的纪律）。
    # ⚠️ 首帧用的那一份快照在 `_job_stream` 内部、**订阅之后**再读一次 ——
    # 顺序反了会有竞态（任务恰好在"读状态"与"注册订阅"之间结束 → 终态事件推给没人，
    # 客户端于是永远停在"生成中"）。这里这次读只为 404，不参与帧的内容。
    jobs.get_snapshot(job_id)
    return StreamingResponse(
        _job_stream(job_id, jobs),
        media_type=SSE_MEDIA_TYPE,
        headers=SSE_HEADERS,
    )


async def _job_stream(job_id: str, jobs: JobService) -> AsyncIterator[str]:
    """把「首帧 snapshot + 后续事件」拼成 SSE 帧流。

    ## ⚠️ 顺序是**先订阅、再读状态**，不能反

    这条流有一个真实的竞态（实测踩到过，症状是界面永远停在"生成中"）：

        读状态 → 还在跑 → （runner 恰好在这一瞬间跑完并广播 done + 哨兵）→ 才注册队列
                                                                    ↑ 终态事件推给谁？没人

    于是订阅者等一个永远不会再来的 `done`，连接一直挂着，客户端无从知道任务已经结束。

    subscribe 在前就没有这个窗口：早于订阅的事件由 `snapshot` + `_replay_terminal`
    覆盖，晚于订阅的事件都进了队列 —— 两者接起来正好是整个任务的全过程。

    ⚠️ `finally: unsubscribe` 由 `with subscription(...)` 保证：客户端断开时 Starlette 会
    关闭这个生成器，退订必须发生 —— 不退订的后果不是报错，而是**队列泄漏**
    （每条断掉的连接留一个队列，runner 每次 publish 都要往里塞），
    表现为长时间运行后内存缓慢上涨、且 publish 越来越慢。
    """
    with subscription(job_id) as sub:
        try:
            # 订阅之后才读：这一刻的状态是"订阅点"的状态，与队列里的事件无缝衔接
            snapshot = jobs.get_snapshot(job_id)
        except Exception:  # noqa: BLE001 - 校验那一刻还在、开流这一刻没了（理论上删不掉）
            logger.exception("订阅时任务 %s 已读不到", job_id)
            yield _sse_event("error", {"message": "生成任务不存在", "request_id": None})
            yield "data: [DONE]\n\n"
            return

        yield _sse_event("snapshot", _snapshot_payload(snapshot))

        if snapshot.status in TERMINAL_STATUSES:
            # 已经结束的任务：把结论重放一遍就走。队列里可能有订阅点之后又被推的事件
            # （极少见：刚读完就结束），重放与它们等价，丢掉不影响正确性。
            for frame in _replay_terminal(snapshot):
                yield frame
            yield "data: [DONE]\n\n"
            return

        while True:
            event = await sub.queue.get()
            if event.get("type") == STREAM_END_TYPE:
                break
            event_type = str(event.get("type") or "message")
            data = {key: value for key, value in event.items() if key != "type"}
            if event_type == "text_delta":
                # 内部事件名 `text_delta` 与对外帧名**刻意一致**（需求 §十 定的名字）。
                # 不叫 `chunk` 是因为 `chunk` 属于 `/conversation/*` 那套协议 ——
                # 两套混用会让客户端写两遍解析，而失败时很难判断是哪一套在推。
                yield _sse_event("text_delta", {"content": data.get("content", "")})
                continue
            if event_type in ("phase", "review", "done", "error", "run_summary"):
                yield _sse_event(event_type, data)
                continue
            # 不认识的内部事件：原样透出（客户端可忽略）。**不静默丢弃** ——
            # 丢掉的话"界面少了一段"会被误判成模型的问题。
            logger.warning("任务 %s 推了一个客户端未知的事件类型：%s", job_id, event_type)
            yield _sse_event(event_type, data)

    yield "data: [DONE]\n\n"


def _snapshot_payload(snapshot: JobSnapshot) -> dict[str, Any]:
    """首帧 `snapshot` 的载荷：需求 §十 点名的五个字段 + 几个有用的身份字段。

    刻意**不含 `result`**（终稿）—— 终稿很大，而它的去处是 `done` 事件或
    `GET /api/jobs/{id}`；塞进首帧会让每次重连都白传一遍全文。

    ⚠️ `section` / `doc_type` 是**优化任务**必需的：优化时 `draft_content` 是
    "模型这一节写了多少"（**不是整篇**），客户端要拿 `section` 把这一节拼回整篇才能正确显示。
    少了它，界面上只会显示一节片段 —— 用户会以为整篇文档被替换掉了
    （实测踩到：本帧曾漏掉这两个键，优化中的正文只有那一节、前后章节全不见了）。
    """
    return {
        "type": "snapshot",
        "job_id": snapshot.id,
        "artifact": snapshot.artifact,
        "status": snapshot.status,
        "phase": snapshot.phase,
        "draft_content": snapshot.draft_content,
        "review": snapshot.review,
        "section": snapshot.section,
        "doc_type": snapshot.doc_type,
    }


def _replay_terminal(snapshot: JobSnapshot) -> list[str]:
    """已收尾任务的"重放"：`run_summary` →（`done` 或 `error`）。

    顺序与实时路径**刻意一致**（`run_summary` 在 `done` / `error` 之前）——
    两条路径给出不同顺序的话，前端得写两套状态机，而其中一套几乎测不到。

    `run_summary` 从 `result_json.run_summary` 取（runner 收尾时存进去的）：
    任务结束后进程内那份合并账早被清理了（TTL / 容量上限），
    不落库的话重连就只能少一份账。
    """
    frames: list[str] = []
    result: Mapping[str, Any] = snapshot.result or {}
    summary = result.get("run_summary")
    if isinstance(summary, Mapping):
        frames.append(_sse_event("run_summary", dict(summary)))
    if snapshot.status == "completed":
        payload: dict[str, Any] = {
            "artifact": snapshot.artifact,
            "content": result.get("content", snapshot.draft_content),
            "review": result.get("review", snapshot.review),
            "revision_applied": result.get("revision_applied"),
            "truncated": result.get("truncated"),
            "finish_reason": result.get("finish_reason"),
        }
        if snapshot.artifact == "prd":
            payload["final_prd"] = payload["content"]
        frames.append(_sse_event("done", payload))
    else:
        frames.append(
            _sse_event(
                "error",
                {
                    "message": snapshot.error or "任务已结束但没有结果",
                    "request_id": None,
                },
            )
        )
    return frames
