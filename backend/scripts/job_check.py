r"""Generation Job 验收：任务化到底有没有真的"脱离 SSE 连接"。

## 两段，费用不同

| 段 | 命令 | 覆盖 | 费用 |
| --- | --- | --- | --- |
| 离线 | `python scripts/job_check.py` | 仓储 / 服务 / 广播 / runner / 会话同步 / 降级（**假模型**，不联网） | 免费 |
| 在线 | `python scripts/job_check.py --live` | 真 HTTP + 真模型：三种产物各跑一个任务，**断开订阅后任务仍然跑完** | 3 次生成调用 |

默认只跑离线段：需求 §十三 的 6 条里，前 5 条的**机制**都能用假模型验（而且更快、更稳、
不花额度），第 3 条"断开后仍然跑完"在离线段用"退订 + 断言任务照跑到 completed"验同一件事。
在线段额外回答一个离线段回答不了的问题：**真模型下 `done` 的正文与 `draft_content` 一致**。

## 为什么不用 `smoke_check.py`

`smoke_check.py` 是**全局离线自检**（配置、提示词、路由形状、模型构造），
它的断言都是"不会变的东西"。任务化的行为验证需要假模型驱动真实 runner、
还要真起一个 SSE 订阅再断开 —— 那是一整套场景，与 `verify_run_agg.py`（跨分片观测）
和 `split_check.py`（分片生成）同类，所以单独一个脚本。

接口地址默认 `http://127.0.0.1:8000`，用 `HARNESS_API_BASE` 覆盖（同 `split_check.py`：
机器上常有一个**改动之前**就起着的后端占着 8000，硬编码只能让人去停别人的服务）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import httpx

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

API_BASE = os.environ.get("HARNESS_API_BASE", "http://127.0.0.1:8000")

_PASS = 0
_FAIL = 0


def check(label: str, ok: bool, detail: str = "") -> bool:
    """一条断言。**失败不中断**：最后统一报，才能一次看到所有问题（与 smoke_check 同风格）。"""
    global _PASS, _FAIL
    if ok:
        _PASS += 1
        print(f"[PASS] {label}" + (f" —— {detail}" if detail else ""))
    else:
        _FAIL += 1
        print(f"[FAIL] {label}" + (f" —— {detail}" if detail else ""))
    return ok


# ---------------------------------------------------------------- 假模型


class _Chunk:
    """`message_text()` 只读 `.content`，所以假片段有这个属性就够了。"""

    def __init__(self, content: str) -> None:
        self.content = content


class _ScriptedModel:
    """按角色分工的假模型：**写手走 `astream`、审核员走 `ainvoke`**。

    ⚠️ 两个方法都要实现，不能只实现一个：
    `document_service.generate_prd_with_review_events` 里写手是流式的（`astream`），
    而审核员是**非流式**的一次调用（`_review_prd` 用 `ainvoke`，输出是一小段 JSON）。
    只实现 `astream` 的话，`ainvoke` 会走 `TrackedChatModel.__getattr__` 转发到
    `None`/AttributeError —— 报错点离原因很远（看起来像"审核模型配错了"）。
    """

    def __init__(self, drafts: list[list[str]], reviews: list[str]) -> None:
        self._drafts = drafts
        self._reviews = reviews
        self.writer_calls = 0
        self.review_calls = 0

    async def astream(self, messages: Any) -> Any:
        index = min(self.writer_calls, len(self._drafts) - 1)
        self.writer_calls += 1
        for piece in self._drafts[index]:
            yield _Chunk(piece)

    async def ainvoke(self, messages: Any) -> Any:
        from langchain_core.messages import AIMessage

        index = min(self.review_calls, len(self._reviews) - 1)
        self.review_calls += 1
        # 用真的 `AIMessage`：`message_text()` 读的是 `.content`，
        # 而审核那段还会顺带看 `response_metadata`（截断判定），自己造一个 dict 容易漏。
        return AIMessage(content=self._reviews[index])


# ---------------------------------------------------------------- 离线段


def offline() -> None:
    """假模型驱动的机制验证：仓储 / 服务 / 广播 / runner / 会话同步 / 降级。"""
    import shutil

    from core.config import Settings
    from services import job_bus
    from services.document_service import DocumentService
    from services.job_models import (
        ARTIFACT_CONFLICT_GROUP,
        ARTIFACT_DOC_KIND,
        ARTIFACT_INITIAL_PHASE,
        ARTIFACT_REVIEW_VIEW,
        ARTIFACT_RUN_TYPE,
        JOB_ARTIFACTS,
        JOB_PHASES,
        JOB_STATUSES,
        OPTIMIZE_ARTIFACTS,
        PAYLOAD_ALLOWED_KEYS,
        PAYLOAD_REQUIRED_KEYS,
        content_artifact_of,
        is_optimize_artifact,
    )
    from services.job_runner import PHASE_MAP, _GATE_DOC_TYPES, run_job, start_job
    from services.quality_gate import run_quality_gate
    from services.section_edit import replace_section
    from services.job_service import (
        DRAFT_PERSIST_INTERVAL_SECONDS,
        DuplicateRunningJob,
        JobNotFound,
        JobService,
        should_persist,
    )
    from services.session_service import SessionService, downgrade_session_data
    from services.stitch import stitch_parts
    # 临时库放在 **backend/ 下**（`.gitignore` 已忽略 `_tmp_jobs_check/`），
    # 不用系统临时目录：SQLite 还要在同目录建 `-wal` / `-shm`，
    # 而受限环境里 `%TEMP%` 往往写不了（实测：`unable to open database file`）。
    tmp = BACKEND_DIR / "_tmp_jobs_check"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True, exist_ok=True)
    try:
        settings = Settings(sqlite_path=tmp / "jobs.db")
        jobs = JobService(settings)
        sessions = SessionService(settings, jobs=jobs)
        # 只调会话层的 ensure_ready：它必须把**三组表**都建上（本层的降级判断要读任务表，
        # 镜像同步与任务收尾的版本写入要读 documents / document_versions）。
        # 这条断言本身就是需求 §十三.6 的前置条件 —— 少建一张表会在第 N 次保存时才炸。
        sessions.ensure_ready()

        # ---------- 表结构 ----------
        table_sql = jobs.repository.table_sql()
        check("建表成功且列齐全", "generation_jobs" in table_sql and "draft_content" in table_sql)
        check(
            "CHECK 里的枚举与 job_models 的元组一致（artifact）",
            all(f"'{item}'" in table_sql for item in JOB_ARTIFACTS),
            "、".join(JOB_ARTIFACTS),
        )
        check(
            "CHECK 里的枚举与 job_models 的元组一致（status / phase）",
            all(f"'{item}'" in table_sql for item in JOB_STATUSES + JOB_PHASES),
        )
        check(
            "建表**不动** PRAGMA user_version（那是 plans 表的版本，两个写者会互相覆盖）",
            job_bus is not None and "user_version" not in table_sql,
        )
        check(
            "每一种 artifact 都被几张映射表完整接线（加新产物时不会再漏一处）",
            all(
                a in ARTIFACT_DOC_KIND
                and a in ARTIFACT_REVIEW_VIEW
                and a in ARTIFACT_INITIAL_PHASE
                and a in ARTIFACT_RUN_TYPE
                and a in ARTIFACT_CONFLICT_GROUP
                and a in PAYLOAD_ALLOWED_KEYS
                for a in JOB_ARTIFACTS
            ),
            "、".join(JOB_ARTIFACTS),
        )
        check(
            "每种 artifact 的必填键都在白名单里（漏了会被路由层 422 挡住，根本跑不到 runner）",
            all(set(PAYLOAD_REQUIRED_KEYS[a]) <= set(PAYLOAD_ALLOWED_KEYS[a]) for a in JOB_ARTIFACTS),
        )
        # 03 篇：澄清"收口没收口"**不进后端契约** —— PRD 任务的必填键只有结构化摘要。
        # 钉住它是因为：任何"必须澄清完成"之类的必填键，都会让"带着警告生成"变成 422，
        # 而那正是那一篇刻意允许的动作（前端 warning + confirm，服务端不设第二道硬门）。
        check(
            "PRD 任务的必填键只有结构化摘要（澄清是否收口不进后端契约）",
            tuple(PAYLOAD_REQUIRED_KEYS["prd"]) == ("requirements_summary",),
            f"prd 必填 = {list(PAYLOAD_REQUIRED_KEYS['prd'])}",
        )
        check(
            "冲突组：优化 artifact 与同文档的生成 artifact 互斥（含它自己）",
            all(
                {a, content_artifact_of(a)} <= set(ARTIFACT_CONFLICT_GROUP[a])
                for a in OPTIMIZE_ARTIFACTS
            ),
        )
        check(
            "冲突组：**跨文档不冲突**（PRD 在跑时接口文档照样能生成）",
            all(
                other not in ARTIFACT_CONFLICT_GROUP[content]
                for content in ("prd", "api-docs", "prompts")
                for other in JOB_ARTIFACTS
                if content_artifact_of(other) != content
            ),
        )

        # ---------- 会话（供任务挂靠） ----------
        made = sessions.save_session(
            {
                "sessionId": "s1",
                "formVersion": "1.2",
                "form": {"productName": "群聊周报助手"},
                "messages": [{"role": "user", "content": "你好"}],
                "roundIndex": 1,
                "documents": {"api": {"content": "旧接口文档", "approved": True}},
                "viewState": "review-prd",
                "updatedAt": "2026-09-30T00:00:00.000+00:00",
            }
        )
        session_id = made.id
        check("会话已建（任务要挂在它上面）", bool(session_id))

        # ---------- job_service：创建 / 查 / 重名 ----------
        job_id = jobs.create_job(session_id, "prd", {"requirements_summary": {"product_name": "x"}})
        record = jobs.get_job(job_id)
        check(
            "create_job 落库为 pending + 该产物的初始 phase",
            record.status == "pending" and record.phase == "writing",
            f"{record.status}/{record.phase}",
        )
        try:
            jobs.create_job(session_id, "prd", {"requirements_summary": {"product_name": "x"}})
            check("同会话 + 同产物重复创建被拒（409 的来源）", False, "居然创建成功了")
        except DuplicateRunningJob as exc:
            check("同会话 + 同产物重复创建被拒（409 的来源）", exc.running_id == job_id, str(exc))
        other = jobs.create_job(session_id, "api-docs", {"prd_content": "# PRD"})
        check("不同产物可以并行（只锁同产物）", bool(other))
        try:
            jobs.get_job("不存在")
            check("查不到的任务抛 JobNotFound（404 的来源）", False)
        except JobNotFound:
            check("查不到的任务抛 JobNotFound（404 的来源）", True)

        payload_dropped = jobs.get_job(jobs.create_job(
            session_id, "prompts", {"prd_content": "# PRD", "不认识的键": 1}
        )).payload()
        check(
            "payload 丢弃白名单之外的键（不静默存进去）",
            "不认识的键" not in payload_dropped and payload_dropped.get("prd_content") == "# PRD",
            json.dumps(payload_dropped, ensure_ascii=False),
        )

        # ---------- 节流 ----------
        check(
            f"节流：{DRAFT_PERSIST_INTERVAL_SECONDS}s 内不重复写",
            should_persist(0.0, DRAFT_PERSIST_INTERVAL_SECONDS)
            and not should_persist(10.0, 10.0 + DRAFT_PERSIST_INTERVAL_SECONDS / 2),
        )

        # ---------- 广播：订阅 / 退订 ----------
        async def bus_roundtrip() -> tuple[int, int]:
            with job_bus.subscription("job-x") as sub:
                during = job_bus.subscriber_count("job-x")
                await job_bus.publish("job-x", {"type": "text_delta", "content": "a"})
                got = sub.queue.get_nowait()
            return during, (1 if got.get("content") == "a" else 0)

        during, ok_content = asyncio.run(bus_roundtrip())
        check("订阅后 publish 能收到事件", during == 1 and ok_content == 1)
        check("退订后订阅者归零（断开 SSE 只退订）", job_bus.subscriber_count("job-x") == 0)
        asyncio.run(job_bus.publish("job-x", {"type": "text_delta", "content": "b"}))
        check("没有订阅者时 publish 是空操作（没人看也要继续跑）", True)

        # ---------- PHASE_MAP 与生成器实际会吐的阶段一致 ----------
        check(
            "PHASE_MAP 覆盖生成器会吐的四个阶段（写 / 审 / 改写 / 完成）",
            set(PHASE_MAP) == {"writing", "reviewing", "rewriting", "done"},
            "、".join(sorted(PHASE_MAP)),
        )

        # ---------- stitch ----------
        check(
            "stitch_parts：丢空片 + 去掉重复的一级标题",
            stitch_parts(["# 标题\n\n## 1 章", "", "# 标题\n\n## 2 章"]) == "# 标题\n\n## 1 章\n\n## 2 章",
        )

        # ---------- 降级：有 running job 时不许降 ----------
        snapshot_with_job = json.dumps(
            {"viewState": "generating-prd", "activeJobId": job_id, "documents": {}},
            ensure_ascii=False,
        )
        kept, _, kind = downgrade_session_data(snapshot_with_job, running_job_ids={job_id})
        check(
            "有 running job → 保持 generating-*（否则前端不会去重连，需求 §十一）",
            kept == snapshot_with_job and kind is None,
        )
        downgraded, original, kind2 = downgrade_session_data(snapshot_with_job)
        check(
            "没有 running job → 照旧降级成 review-*（孤儿态那条规则没被破坏）",
            "generating-prd" not in downgraded and original == "generating-prd" and kind2 == "prd",
        )

        # ---------- runner：假模型跑完整任务 ----------
        # PRD 双智能体的调用序（`PRD_MAX_REWRITES = 1`）：
        #   1) 写手初稿（astream）
        #   2) 审核（ainvoke）→ 有问题
        #   3) 改写稿（astream，round=2）
        #   4) round_no(2) > 上限(1) → 直接以"仍带审核意见"的 done 收尾，**不再审第二遍**
        #      —— 这是 `document_service` 的既有行为（"达到上限仍不合格 → 照样输出终稿"），
        #      验收按事实断言，不按愿望断言。
        review_bad = json.dumps({"ok": False, "issues": [{"detail": "第 2 章太薄"}]})
        review_ok = json.dumps({"ok": True, "issues": []})
        model = _ScriptedModel(
            drafts=[["# 群聊周报助手", "\n\n", "初稿正文"], ["# 群聊周报助手", "\n\n改写后的正文"]],
            reviews=[review_bad, review_ok],
        )
        doc_service = DocumentService(model=model)
        # 审核模型默认复用同一个实例（没配 PRD_REVIEW_LLM_*），所以假模型同时扮演两个角色
        #
        # 先把上面那条**故意没跑**的 pending 任务收掉：它一直占着"同会话 + prd 只有一个在跑"的
        # 名额（这正是 409 那条规则的用途 —— 连测试自己都会被它拦住）。
        jobs.update_job(job_id, status="cancelled")
        prd_job = jobs.create_job(session_id, "prd", {"requirements_summary": {"product_name": "群聊周报助手"}})

        async def pipe(job: str, documents: Any) -> list[dict[str, Any]]:
            """起一个订阅、把事件收进列表，直到结束哨兵（模拟一个 SSE 订阅者）。"""
            events: list[dict[str, Any]] = []
            with job_bus.subscription(job) as sub:
                task = start_job(job, jobs=jobs, sessions=sessions, documents=documents)
                while True:
                    item = await sub.queue.get()
                    if item.get("type") == job_bus.STREAM_END_TYPE:
                        break
                    events.append(item)
                await task
            return events

        events = asyncio.run(pipe(prd_job, doc_service))
        kinds = [item["type"] for item in events]
        record = jobs.get_job(prd_job)
        check(
            "runner 走完：status=completed 且 phase=done",
            record.status == "completed" and record.phase == "done",
            f"{record.status}/{record.phase} error={record.error}",
        )
        check(
            "PRD 终稿是**改写后**的那一稿（v1 不残留在正文里）",
            "改写后的正文" in record.draft_content and "初稿正文" not in record.draft_content,
            record.draft_content[:60].replace("\n", "\\n"),
        )
        check(
            "previous_draft 存下了 v1 初稿（改写前的证据）",
            "初稿正文" in record.previous_draft,
            record.previous_draft[:40].replace("\n", "\\n"),
        )
        check(
            "事件序列含 phase + review + text_delta",
            "phase" in kinds and "review" in kinds and "text_delta" in kinds,
            "、".join(kinds[:8]),
        )
        check(
            "phase 事件用对外名字（writer_started / review_started / rewrite_started / done）",
            [item["phase"] for item in events if item["type"] == "phase"]
            == ["writer_started", "review_started", "rewrite_started", "done"],
            "、".join(item["phase"] for item in events if item["type"] == "phase"),
        )
        summary = next((item for item in events if item["type"] == "run_summary"), None)
        done_event = next((item for item in events if item["type"] == "done"), None)
        check(
            "run_summary 在 done **之前**（与 conversation.py 同顺序）",
            kinds.index("run_summary") < kinds.index("done"),
        )
        check(
            "done 载荷带 final_prd / review / revision_applied（需求 §十）",
            isinstance(done_event, dict)
            and done_event.get("final_prd") == record.draft_content
            and isinstance(done_event.get("review"), dict)
            and done_event["revision_applied"] is True,
            json.dumps(
                {k: v for k, v in (done_event or {}).items() if k in ("revision_applied", "truncated")},
                ensure_ascii=False,
            ),
        )
        check(
            "run_summary 里 revision_applied=True（改写过的账）",
            isinstance(summary, dict) and summary.get("revision_applied") is True,
        )
        # RAG 下线那一篇：注入的技能包要**跟着 Job 的 run_summary 一起出来**
        # （记录发生在 document_service 取 bundle 时，与 run 的开启是同一套 contextvar）
        check(
            "run_summary.injected_skills 记下 PRD 技能包（Job 路径也带上）",
            isinstance(summary, dict)
            and isinstance(summary.get("injected_skills"), list)
            and summary["injected_skills"][0]["skill_id"] == "prd-generator"
            and len(summary["injected_skills"][0]["artifacts"]) >= 4,
            json.dumps((summary or {}).get("injected_skills"), ensure_ascii=False)[:140],
        )
        check(
            "review_json 落库且内容来自审核员（issues 原样带出来）",
            isinstance(record.review(), dict)
            and record.review()["issues"][0]["detail"] == "第 2 章太薄"
            and record.review()["passed"] is False,
            json.dumps(record.review(), ensure_ascii=False),
        )

        # ---------- 会话同步 ----------
        loaded = sessions.get_session(session_id)
        snap = json.loads(loaded.session_data)
        check(
            "任务完成后会话同步：activeJobId 清空 + viewState=review-prd",
            snap.get("activeJobId") is None and snap.get("viewState") == "review-prd",
            f"activeJobId={snap.get('activeJobId')} viewState={snap.get('viewState')}",
        )
        check(
            "终稿写进 documents.prd.content",
            snap["documents"]["prd"]["content"] == record.draft_content,
        )
        check(
            "审查结论写进 prdReviewResult（需求点名的键）",
            isinstance(snap.get("prdReviewResult"), dict)
            and snap["prdReviewResult"].get("issues") == [{"detail": "第 2 章太薄"}],
            json.dumps(snap.get("prdReviewResult"), ensure_ascii=False),
        )
        check(
            "同步是**合并**不是覆盖（原有的 messages / form 都还在）",
            snap.get("messages") and snap.get("form", {}).get("productName") == "群聊周报助手",
        )
        check(
            "documents 一层深合并（别的产物的 key 没被顶掉）",
            snap["documents"].get("api", {}).get("content") == "旧接口文档"
            and snap["documents"]["api"].get("approved") is True,
        )

        # ---------- 版本层：生成 Job 完成后写入 v1（02 篇验收标准 1 / 3 / 4） ----------
        prd_cur = sessions.documents.get_current_version(session_id, "prd")
        check(
            "版本层：PRD Job 完成 → v1，且正文与 session_data 镜像**一致**（双写不偏差）",
            prd_cur is not None
            and prd_cur.version_no == 1
            and prd_cur.content == record.draft_content
            and prd_cur.content == snap["documents"]["prd"]["content"],
            f"v{prd_cur.version_no if prd_cur else None}",
        )
        check(
            "版本层：source_kind=generate，source_job_id 指向这个任务",
            prd_cur.source_kind.value == "generate" and prd_cur.source_job_id == prd_job,
            f"{prd_cur.source_kind.value} / {prd_cur.source_job_id}",
        )
        check(
            "版本层：run_summary 与 review 都写进 metadata（PRD 才有审查环节）",
            isinstance(prd_cur.metadata().get("run_summary"), dict)
            and prd_cur.metadata().get("review", {}).get("issues") == [{"detail": "第 2 章太薄"}],
            "、".join(sorted(prd_cur.metadata())),
        )

        # ---------- 结构校验：PRD Job 完成 → metadata.quality_gate（04 篇验收 2 / 5） ----------
        _prd_gate = prd_cur.metadata().get("quality_gate") or {}
        check(
            "版本层：PRD Job 完成后 metadata.quality_gate 有值且形状完整（04 篇验收 2）",
            isinstance(_prd_gate.get("passed"), bool)
            and _prd_gate.get("doc_type") == "prd"
            and isinstance(_prd_gate.get("score"), int)
            and 0 <= _prd_gate["score"] <= 100
            and bool(_prd_gate.get("checks")),
            f"passed={_prd_gate.get('passed')} score={_prd_gate.get('score')}"
            f" checks={len(_prd_gate.get('checks') or [])}",
        )
        # 这条是**验收 5 的离线版**：PRD 这一趟走的是「初稿 → 审核 → 改写」，
        # 落库的正文是**改写后**那一稿。如果 gate 是在改写**之前**算的、或者把初稿的结论
        # 带了过来，那么"重算一遍这一版正文"就对不上。
        _prd_recomputed = run_quality_gate("prd", prd_cur.content)
        check(
            "结构校验针对的是**最终稿**（与按下班正文重算的结果逐项一致，04 篇验收 5）",
            _prd_gate.get("passed") == _prd_recomputed["passed"]
            and _prd_gate.get("score") == _prd_recomputed["score"]
            and [item["id"] for item in _prd_gate.get("checks") or []]
            == [item["id"] for item in _prd_recomputed["checks"]],
            f"落库 {_prd_gate.get('passed')}/{_prd_gate.get('score')}"
            f" vs 重算 {_prd_recomputed['passed']}/{_prd_recomputed['score']}",
        )
        check(
            "结构校验覆盖三个产物（runner 的 doc_type 白名单 + content_artifact_of 的反查）",
            _GATE_DOC_TYPES == {"prd", "api-docs", "prompts"}
            and {content_artifact_of(item) for item in JOB_ARTIFACTS}
            == {"prd", "api-docs", "prompts"},
            "、".join(sorted({content_artifact_of(item) for item in JOB_ARTIFACTS})),
        )
        _api_cur = sessions.documents.get_current_version(session_id, "api-docs")
        check(
            "版本层：会话里先种下的接口文档正文被**迁移**成 import 的 v1（老数据那条路）",
            _api_cur is not None
            and _api_cur.version_no == 1
            and _api_cur.source_kind.value == "import"
            and _api_cur.content == "旧接口文档",
            f"v{_api_cur.version_no if _api_cur else None}/{_api_cur.source_kind.value if _api_cur else None}",
        )

        # ---------- 跳过订阅：没人订阅也要跑完（需求 §十三.3） ----------
        solo_job = jobs.create_job(session_id, "prd", {"requirements_summary": {"product_name": "x"}})
        solo_model = _ScriptedModel(drafts=[["# 无人订阅也要跑完"]], reviews=[review_ok])
        asyncio.run(
            run_job(
                solo_job,
                jobs=jobs,
                sessions=sessions,
                documents=DocumentService(model=solo_model),
            )
        )
        solo = jobs.get_job(solo_job)
        check(
            "**没有任何订阅者**时任务照样跑完并落库（断开 SSE 不 cancel 的机制保证）",
            solo.status == "completed" and "无人订阅也要跑完" in solo.draft_content,
            f"{solo.status}/{len(solo.draft_content)} 字符",
        )
        check(
            "审核通过的那条路径：review.passed=True 且 revision_applied=False（没改写）",
            (solo.review() or {}).get("passed") is True
            and json.loads(solo.result_json or "{}").get("revision_applied") is False,
            json.dumps(solo.review(), ensure_ascii=False),
        )

        # ---------- 运行中退订：任务不受影响 ----------
        slow_model = _ScriptedModel(drafts=[["# 慢稿", " 第二段"]], reviews=[review_ok])
        watch_job = jobs.create_job(session_id, "prd", {"requirements_summary": {"product_name": "x"}})

        async def subscribe_then_leave() -> str:
            with job_bus.subscription(watch_job) as sub:
                task = start_job(watch_job, jobs=jobs, sessions=sessions, documents=DocumentService(model=slow_model))
                await sub.queue.get()  # 只等第一个事件，然后**退出 with**（= 客户端断开）
                leaving = job_bus.subscriber_count(watch_job)
            await task
            return f"中途退订时订阅者={leaving}"

        detail = asyncio.run(subscribe_then_leave())
        watched = jobs.get_job(watch_job)
        check(
            "订阅者中途断开后任务仍然跑到 completed（需求 §十三.3 的核心）",
            watched.status == "completed",
            f"{detail}｜{watched.status}",
        )
        check("断开后订阅者归零（队列被回收，不是泄漏）", job_bus.subscriber_count(watch_job) == 0)

        # ---------- 失败路径 ----------
        class _Boom:
            async def astream(self, messages: Any) -> Any:
                yield _Chunk("# 半截")
                raise RuntimeError("模拟上游失败")

            async def ainvoke(self, messages: Any) -> Any:  # pragma: no cover - 走不到
                raise RuntimeError("模拟上游失败")

        bad_job = jobs.create_job(session_id, "prd", {"requirements_summary": {"product_name": "x"}})
        boom_events: list[dict[str, Any]] = []

        async def run_boom() -> None:
            with job_bus.subscription(bad_job) as sub:
                task = start_job(bad_job, jobs=jobs, sessions=sessions, documents=DocumentService(model=_Boom()))
                while True:
                    item = await sub.queue.get()
                    if item.get("type") == job_bus.STREAM_END_TYPE:
                        break
                    boom_events.append(item)
                await task

        asyncio.run(run_boom())
        boom = jobs.get_job(bad_job)
        check(
            "上游异常 → status=failed + error 落库（不静默死掉）",
            boom.status == "failed" and "模拟上游失败" in (boom.error or ""),
            boom.error or "（没有 error）",
        )
        check(
            "失败也发 error + run_summary（失败那趟的账不能丢）",
            any(item["type"] == "error" for item in boom_events)
            and any(item["type"] == "run_summary" for item in boom_events),
            "、".join(item["type"] for item in boom_events),
        )
        boom_snap = json.loads(sessions.get_session(session_id).session_data)
        check(
            "失败时半截草稿留在会话里（用户的东西不被清掉）",
            boom_snap["documents"]["prd"]["content"].startswith("# 半截"),
            boom_snap["documents"]["prd"]["content"][:20],
        )
        check(
            "失败也清 activeJobId（否则界面永远停在生成中）",
            boom_snap.get("activeJobId") is None and boom_snap.get("viewState") == "review-prd",
        )
        # ---------- 版本层：失败但有半成品也要写（02 篇验收标准 5） ----------
        boom_cur = sessions.documents.get_current_version(session_id, "prd")
        boom_versions = sessions.documents.list_versions(session_id, "prd")
        check(
            "版本层：失败但留下半成品 → 仍然**升新版**（generate），metadata 标 job_status=failed",
            boom_cur is not None
            and boom_cur.version_no == len(boom_versions)
            and boom_cur.version_no > 1
            and boom_cur.content.startswith("# 半截")
            and boom_cur.source_kind.value == "generate"
            and boom_cur.metadata().get("job_status") == "failed",
            f"v{boom_cur.version_no if boom_cur else None}｜{boom_cur.metadata().get('job_status') if boom_cur else None}",
        )
        check(
            "版本层：失败路径是**追加**而不是就地覆盖 —— 版本号连续、v1 正文一字未动",
            [item.version_no for item in boom_versions]
            == list(range(len(boom_versions), 0, -1))
            and boom_versions[-1].content == record.draft_content,
            str([item.version_no for item in boom_versions]),
        )
        check(
            "版本层：失败时版本正文与会话镜像**一致**（两条链路用的是同一份草稿）",
            boom_cur.content == boom_snap["documents"]["prd"]["content"],
            "一致",
        )

        # ---------- 服务重启：遗留 running → failed ----------
        stale_job = jobs.create_job(session_id, "prd", {"requirements_summary": {"product_name": "x"}})
        jobs.update_job(stale_job, status="running")
        swept = jobs.fail_stale_jobs()
        stale = jobs.get_job(stale_job)
        check(
            "启动扫描：遗留 running 任务被标记为 failed（需求 §十二）",
            stale_job in swept and stale.status == "failed" and "服务重启" in (stale.error or ""),
            stale.error or "",
        )

        # ---------- repo 层的 running 查询 ----------
        fresh = jobs.create_job(session_id, "prd", {"requirements_summary": {"product_name": "x"}})
        check(
            "running_ids_for_sessions 只回在跑的任务（给降级判断用）",
            jobs.running_ids_for_sessions([session_id]) == {fresh},
            str(jobs.running_ids_for_sessions([session_id])),
        )

        # ---------- SSE 订阅的**竞态**（实测踩到过的真 bug）----------
        #
        # 场景：SSE 先读状态（"还在跑"），runner 恰好在"读状态"与"注册订阅"之间跑完并
        # 广播终态事件 —— 那些事件推给**没人**，订阅者于是等一个永远不会再来的 done，
        # 界面上永远停在"生成中"（实测在审查阶段刷新之后复现）。
        #
        # 下面的假 service 就模拟那一瞬间：`get_snapshot()` 在返回**之前**把任务收尾并广播
        # 终态。**先订阅、再读状态**的实现能拿到这些事件；反过来写就会在这里挂住
        # （所以用 `wait_for` 超时把"挂住"变成一条失败断言，而不是让脚本永远卡着）。
        from api.jobs import _job_stream  # noqa: PLC0415 - 只在这一组断言里用

        # 上面那条 `fresh` 还占着"同会话同产物只跑一个"的名额（这正是 409 规则的用途）
        jobs.update_job(fresh, status="cancelled")
        race_job = jobs.create_job(session_id, "prd", {"requirements_summary": {"product_name": "x"}})
        jobs.update_job(race_job, status="running", phase="writing")

        class _RacyJobs:
            """读状态时"顺手"把任务结束掉并广播终态 —— 即那个竞态窗口。"""

            def __init__(self, inner: JobService) -> None:
                self._inner = inner
                self.fired = False

            def get_snapshot(self, job_id: str) -> Any:
                snapshot = self._inner.get_snapshot(job_id)
                if not self.fired:
                    self.fired = True
                    self._inner.update_job(
                        job_id,
                        status="completed",
                        phase="done",
                        draft_content="# 终稿",
                        result_json=json.dumps({"content": "# 终稿"}, ensure_ascii=False),
                    )
                    # 顺序与 runner 一致：run_summary → done → 哨兵（同步投递，不 await）
                    job_bus.publish_nowait(job_id, {"type": "run_summary", "run_id": "race"})
                    job_bus.publish_nowait(job_id, {"type": "done", "artifact": "prd", "content": "# 终稿"})
                    job_bus.publish_stream_end(job_id)
                return snapshot

        async def race_frames() -> list[str]:
            frames: list[str] = []
            async for frame in _job_stream(race_job, _RacyJobs(jobs)):  # type: ignore[arg-type]
                frames.append(frame)
            return frames

        try:
            frames = asyncio.run(asyncio.wait_for(race_frames(), timeout=5))
        except asyncio.TimeoutError:
            check(
                "竞态：任务在「读状态」与「注册订阅」之间结束时，流能正常收尾",
                False,
                "流挂住了 —— 订阅必须先于读状态（api/jobs.py `_job_stream`）",
            )
        else:
            joined = "".join(frames)
            check(
                "竞态：任务在「读状态」与「注册订阅」之间结束时，流能正常收尾",
                "event: snapshot" in joined and "event: done" in joined and "[DONE]" in joined,
                f"{len(frames)} 帧：{''.join(f.splitlines()[0] + '|' for f in frames)[:110]}",
            )

        # ---------- 优化任务（F8.6）：artifact / 冲突 / runner / 会话同步 ----------

        # (1) `replace_section` —— TS 那份的**跨语言镜像**，逐条对齐它的已知边界
        doc = (
            "# 群聊周报助手\n\n"
            "## 1. 产品概述\n\n旧概述\n\n"
            "## 2. 功能需求\n\n旧需求\n\n"
            "### 2.1 汇总\n\n子节\n\n"
            "## 3. 技术规格\n\n```bash\n# 安装依赖\nnpm install\n```\n"
        )
        replaced = replace_section(doc, "## 2. 功能需求", "## 2. 功能需求\n\n新需求")
        check(
            "replace_section：同级替换换掉整节（**含它的更深子节**），前后章节不动",
            "新需求" in replaced
            and "旧需求" not in replaced
            and "子节" not in replaced  # §2.1 属于 §2 这棵子树，随它一起被替换
            and "## 1. 产品概述" in replaced
            and replaced.endswith("## 3. 技术规格\n\n```bash\n# 安装依赖\nnpm install\n```\n"),
            replaced.replace("\n", "\\n")[:80],
        )
        check(
            "replace_section：标题逐字匹配不上 → **原样返回**（绝不追加到末尾）",
            replace_section(doc, "## 9. 不存在的章", "新内容") == doc,
        )
        check(
            "replace_section：模型只给子节标题（低一级）时补回原标题",
            replace_section(doc, "## 2. 功能需求", "### 2.1 汇总\n\n改写过的子节")
            .count("## 2. 功能需求")
            == 1
            and "改写过的子节" in replace_section(doc, "## 2. 功能需求", "### 2.1 汇总\n\n改写过的子节"),
        )
        check(
            "replace_section：代码块里的 `# 注释` 不算标题（否则会切出假小节）",
            replace_section(doc, "## 3. 技术规格", "## 3. 技术规格\n\n```bash\n# 装依赖\npnpm i\n```")
            .count("## 3. 技术规格")
            == 1,
        )
        check(
            "replace_section：整篇里的其它章节一个字都没动",
            replace_section(doc, "## 1. 产品概述", "## 1. 产品概述\n\n新概述").endswith(
                "## 3. 技术规格\n\n```bash\n# 安装依赖\nnpm install\n```\n"
            ),
        )

        # (2) 冲突组：同一份文档的「生成」与「优化」互斥，不同文档互不影响
        jobs.update_job(fresh, status="completed")  # 收掉前面那条占位的 prd 任务
        holding = jobs.create_job(session_id, "prd", {"requirements_summary": {"p": 1}})
        try:
            jobs.create_job(
                session_id,
                "optimize-prd",
                {
                    "doc_type": "prd",
                    "section": "## 1. 产品概述",
                    "current_content": "## 1. 产品概述\n\n旧",
                    "document_content": "# 文档\n\n## 1. 产品概述\n\n旧\n",
                    "instruction": "更详细",
                },
            )
            check("冲突组：PRD 生成在跑时不能同时开 PRD 优化（409 的来源）", False, "居然放进来了")
        except DuplicateRunningJob as exc:
            check(
                "冲突组：PRD 生成在跑时不能同时开 PRD 优化（409 的来源）",
                exc.running_artifact == "prd",
                str(exc),
            )
        api_job = jobs.create_job(
            session_id,
            "optimize-api-docs",
            {
                "doc_type": "api",
                "section": "## 1. 概览",
                "current_content": "## 1. 概览\n\n旧",
                "document_content": "# 接口文档\n\n## 1. 概览\n\n旧\n",
                "instruction": "补错误码",
            },
        )
        check("冲突组：跨文档不冲突（PRD 在跑，接口文档优化照样能开）", bool(api_job))
        jobs.update_job(holding, status="cancelled")
        jobs.update_job(api_job, status="cancelled")

        # (3) runner：假模型跑一遍优化，正文要**拼回整篇**、视图要**留在 review**
        #     会话先种一份"带审查结论 + truncated 标记"的 PRD，用来验证优化**不碰**这两样
        doc_session = sessions.save_session(
            {
                "sessionId": "opt",
                "formVersion": "1.2",
                "form": {"productName": "群聊周报助手"},
                "messages": [{"role": "user", "content": "你好"}],
                "roundIndex": 1,
                "documents": {
                    "prd": {
                        "content": "# 群聊周报助手\n\n## 1. 产品概述\n\n旧概述\n\n## 2. 功能需求\n\n旧需求\n",
                        "approved": True,
                        "truncated": True,
                    }
                },
                "prdReviewResult": {"passed": True, "issues": [], "review_model": "fake"},
                "viewState": "review-prd",
                "updatedAt": "2026-09-30T00:00:00.000+00:00",
            }
        ).id
        sessions.sync_job_started(session_id=doc_session, job_id="job-opt-1", artifact="optimize-prd")
        during = json.loads(sessions.get_session(doc_session).session_data)
        check(
            "优化开始时 viewState **保持 review-prd**（不像生成那样切 generating-*）",
            during.get("viewState") == "review-prd" and during.get("activeJobId") == "job-opt-1",
            f"viewState={during.get('viewState')} activeJobId={during.get('activeJobId')}",
        )

        opt_artifact = "optimize-prd"
        optimize_payload = {
            "doc_type": "prd",
            "section": "## 2. 功能需求",
            "current_content": "## 2. 功能需求\n\n旧需求",
            "document_content": during["documents"]["prd"]["content"],
            "instruction": "把功能需求写细一点",
        }
        opt_job = jobs.create_job(doc_session, opt_artifact, optimize_payload)
        opt_model = _ScriptedModel(drafts=[["## 2. 功能需求", "\n\n", "新需求（更细）"]], reviews=[])
        asyncio.run(
            run_job(
                opt_job,
                jobs=jobs,
                sessions=sessions,
                documents=DocumentService(model=opt_model),
            )
        )
        opt_record = jobs.get_job(opt_job)
        opt_snapshot = jobs.get_snapshot(opt_job)
        after = json.loads(sessions.get_session(doc_session).session_data)
        check(
            "优化 runner 收尾：completed + phase=done",
            opt_record.status == "completed" and opt_record.phase == "done",
            f"{opt_record.status}/{opt_record.phase} err={opt_record.error}",
        )
        check(
            "优化终稿是**整篇**（新的一节换进去、其它章节原样）",
            "新需求（更细）" in opt_record.draft_content
            and "旧需求" not in opt_record.draft_content
            and "## 1. 产品概述" in opt_record.draft_content,
            opt_record.draft_content.replace("\n", "\\n")[:70],
        )
        check(
            "会话同步：写入整篇 + viewState 仍是 review-prd + activeJobId 清空",
            after["documents"]["prd"]["content"] == opt_record.draft_content
            and after.get("viewState") == "review-prd"
            and after.get("activeJobId") is None,
            f"viewState={after.get('viewState')} activeJobId={after.get('activeJobId')}",
        )
        # ---------- 版本层：优化**不升号**（02 篇验收标准 2） ----------
        opt_cur = sessions.documents.get_current_version(doc_session, "prd")
        check(
            "版本层：优化 Job 完成 → current **就地更新**，version_no 不变、不新增版本",
            opt_cur is not None
            and opt_cur.version_no == 1
            and opt_cur.content == opt_record.draft_content
            and len(sessions.documents.list_versions(doc_session, "prd")) == 1,
            f"v{opt_cur.version_no if opt_cur else None}｜共 {len(sessions.documents.list_versions(doc_session, 'prd'))} 版",
        )
        check(
            "版本层：优化**不改** source_kind（仍是首版出生时的 import）",
            opt_cur.source_kind.value == "import",
            opt_cur.source_kind.value,
        )
        check(
            "版本层：优化把本趟 run_summary 写进 metadata（对照 Job id 能看出是哪一趟）",
            isinstance(opt_cur.metadata().get("run_summary"), dict)
            and opt_cur.metadata().get("run_summary") == opt_record.result().get("run_summary"),
            "、".join(sorted(opt_cur.metadata())),
        )
        # 04 篇：优化 Job 也要跑结构校验。这里断言"落库的结论与**优化后的整篇**一致"。
        # ⚠️ "覆盖而不是沿用旧值"这一条在**这里验不出来**（本 fixture 改前改后的结论恰好相同），
        # 所以它放在 smoke_check 里用两次 `sync_from_job` 直接验（见那边）。
        _opt_gate = opt_cur.metadata().get("quality_gate") or {}
        _opt_recomputed = run_quality_gate("prd", opt_cur.content)
        check(
            "结构校验：优化 Job 完成后 metadata.quality_gate 描述优化后的整篇（04 篇 §5 末条）",
            bool(_opt_gate)
            and _opt_gate.get("doc_type") == "prd"
            and _opt_gate.get("passed") == _opt_recomputed["passed"]
            and _opt_gate.get("score") == _opt_recomputed["score"],
            f"落库 {_opt_gate.get('passed')}/{_opt_gate.get('score')}"
            f" vs 按优化后正文重算 {_opt_recomputed['passed']}/{_opt_recomputed['score']}",
        )
        # 同一 PRD **连续第二次**优化：仍不升号，metadata 的 run_summary 换成最后一次
        second_payload = dict(optimize_payload)
        second_payload["document_content"] = opt_record.draft_content
        second_payload["current_content"] = "## 2. 功能需求\n\n新需求（更细）"
        second_job = jobs.create_job(doc_session, opt_artifact, second_payload)
        second_model = _ScriptedModel(
            drafts=[["## 2. 功能需求", "\n\n", "再细一点"]], reviews=[]
        )
        asyncio.run(
            run_job(
                second_job,
                jobs=jobs,
                sessions=sessions,
                documents=DocumentService(model=second_model),
            )
        )
        second_record = jobs.get_job(second_job)
        second_cur = sessions.documents.get_current_version(doc_session, "prd")
        check(
            "版本层：**连续两次优化**后 version_no 仍为 1、正文是第二次的结果（验收标准 2）",
            second_record.status == "completed"
            and second_cur.version_no == 1
            and second_cur.content == second_record.draft_content
            and "再细一点" in second_cur.content
            and len(sessions.documents.list_versions(doc_session, "prd")) == 1,
            f"v{second_cur.version_no}｜{second_cur.content[-12:].replace(chr(10), ' ')}｜共 {len(sessions.documents.list_versions(doc_session, 'prd'))} 版",
        )
        check(
            "版本层：metadata.run_summary 是**最后一次**那趟的账（浅合并覆盖同名字段）",
            second_cur.metadata().get("run_summary") == second_record.result().get("run_summary"),
            "最后一次",
        )
        check(
            "版本层：两次优化后会话镜像也跟着更新（镜像与 current 一致）",
            json.loads(sessions.get_session(doc_session).session_data)["documents"]["prd"][
                "content"
            ]
            == second_cur.content,
            "一致",
        )
        check(
            "优化**不动** `truncated`（那个标记说的是整篇被截断过，与改一节无关）",
            after["documents"]["prd"].get("truncated") is True,
            str(after["documents"]["prd"].get("truncated")),
        )
        check(
            "优化**保留** prdReviewResult（没重新审稿，就不能把审查结论抹掉）",
            (after.get("prdReviewResult") or {}).get("passed") is True,
            json.dumps(after.get("prdReviewResult"), ensure_ascii=False),
        )
        check(
            "snapshot 带 section / doc_type（前端靠它把节片段拼回整篇显示）",
            opt_snapshot.section == "## 2. 功能需求" and opt_snapshot.doc_type == "prd",
            f"section={opt_snapshot.section!r} doc_type={opt_snapshot.doc_type!r}",
        )
        check(
            "run_summary 的 run_type 与前台优化接口逐字一致（optimize_document）",
            ARTIFACT_RUN_TYPE[opt_artifact] == "optimize_document",
        )

        # ---------- 结构校验：api-docs / prompts 的 Job 也要写 gate（04 篇验收 4） ----------
        # 用假模型**真跑**这两条链路，而不是只查"白名单里有它"：gate 是 runner 收尾时算的，
        # 只验表验不出"某个收尾分支忘了调用它"。
        gate_session = sessions.save_session(
            {
                "sessionId": "gate",
                "formVersion": "1.2",
                "form": {"productName": "群聊周报助手"},
                "messages": [],
                "roundIndex": 0,
                "documents": {},
                "viewState": "review-prd",
                "updatedAt": "2026-09-30T00:00:00.000+00:00",
            }
        ).id
        # 给 prompts 的 gate 准备一份"数得出功能数"的 PRD：这条路径要顺便验
        # `_gate_context` 真的把 payload 里的 prd_content 递进去了（04 篇 §5）
        gate_prd = (
            "# 群聊周报助手\n\n## 2. 功能需求\n\n### MVP 功能\n\n"
            "| 编号 | 功能 |\n| --- | --- |\n| FR-01 | 登录 |\n"
        )
        for gate_artifact, gate_doc_type in (("api-docs", "api-docs"), ("prompts", "prompts")):
            gate_job = jobs.create_job(gate_session, gate_artifact, {"prd_content": gate_prd})
            # 分片计划不同（api=3 片 / prompts=3 片），多给几份同样的草稿：
            # `_ScriptedModel` 用完之后会重复最后一份
            #
            # 05 篇：`reviews` 现在**不再为空** —— 生成完会再调一次模型做审查，
            # 那次 `ainvoke` 消费的就是这里的第一条。给一份**带 issues 的**结论，
            # 才能验到"审查结论真的流进了 metadata.review"（给空列表的话
            # `_ScriptedModel` 会 IndexError → 被降级成 review_skipped，反而验不到）。
            gate_review = json.dumps(
                {
                    "passed": False,
                    "summary": "覆盖不全：导出类接口缺失",
                    "issues": [
                        {
                            "severity": "HIGH",  # 大小写不该影响归一化
                            "section": "## 第 6 章 接口清单",
                            "problem": "PRD 的 FR-04 导出没有对应接口",
                            "suggestion": "补一条 POST /api/v1/exports",
                        }
                    ],
                },
                ensure_ascii=False,
            )
            gate_model = _ScriptedModel(
                drafts=[[f"# {gate_doc_type} 正文", "\n\n## 第 1 章 文档信息\n\n占位"]] * 4,
                reviews=[gate_review],
            )
            # 订阅一次：既要验 metadata，也要验**事件序列**（05 篇验收 4 的"有 Review phase"）
            # ⚠️ 这段跑在 `offline()` 这个**同步**函数里，所以起一个局部 async 帮手再用
            # `asyncio.run` 驱动（与上面 `pipe` 同一个理由）；直接在这里 `await` 是语法错误。
            async def _drive(job: str, documents: Any) -> list[dict[str, Any]]:
                collected: list[dict[str, Any]] = []
                with job_bus.subscription(job) as sub:
                    task = start_job(job, jobs=jobs, sessions=sessions, documents=documents)
                    while True:
                        item = await sub.queue.get()
                        if item.get("type") == job_bus.STREAM_END_TYPE:
                            break
                        collected.append(item)
                    await task
                return collected

            gate_events = asyncio.run(_drive(gate_job, DocumentService(model=gate_model)))
            gate_names = [str(item.get("type")) for item in gate_events]
            gate_phases = [
                str(item.get("phase")) for item in gate_events if item.get("type") == "phase"
            ]
            gate_record = jobs.get_job(gate_job)
            gate_cur = sessions.documents.get_current_version(gate_session, gate_doc_type)
            gate_meta = ((gate_cur.metadata().get("quality_gate") if gate_cur else None) or {})
            check(
                f"[{gate_artifact}] Job 完成后 metadata.quality_gate 有值（04 篇验收 4）",
                gate_record.status == "completed"
                and gate_cur is not None
                and gate_meta.get("doc_type") == gate_doc_type
                and isinstance(gate_meta.get("score"), int)
                and bool(gate_meta.get("checks")),
                f"{gate_record.status}｜gate={gate_meta.get('passed')}/{gate_meta.get('score')}"
                f" checks={len(gate_meta.get('checks') or [])}",
            )

            # ---------- 05 篇：生成后审查（一次，不 Rewrite） ----------
            gate_review_meta = ((gate_cur.metadata().get("review") if gate_cur else None) or {})
            check(
                f"[{gate_artifact}] 有 Review phase（SSE 发 review_started）且 review 事件在 done 之前",
                "review_started" in gate_phases
                and "review" in gate_names
                and gate_names.index("review") < gate_names.index("done"),
                "、".join(gate_names[-6:]),
            )
            check(
                f"[{gate_artifact}] 审查结论写进 metadata.review（severity 归一化成小写 high）",
                gate_review_meta.get("passed") is False
                and gate_review_meta.get("review_skipped") is False
                and gate_review_meta.get("summary") == "覆盖不全：导出类接口缺失"
                and [
                    (item.get("severity"), item.get("section"), item.get("suggestion"))
                    for item in gate_review_meta.get("issues") or []
                ]
                == [("high", "## 第 6 章 接口清单", "补一条 POST /api/v1/exports")],
                json.dumps(gate_review_meta, ensure_ascii=False)[:160],
            )
            check(
                f"[{gate_artifact}] 审查不通过**仍然交付正文**（不 Rewrite，正文照写在 done 里）",
                "正文" in gate_record.draft_content
                and any(
                    item.get("type") == "done" and item.get("content") == gate_record.draft_content
                    for item in gate_events
                ),
                f"正文 {len(gate_record.draft_content)} 字符",
            )
        # prompts 的 gate 应当拿到了 PRD 上下文：那一条不再是 skipped
        _prompts_cur = sessions.documents.get_current_version(gate_session, "prompts")
        _count_check = next(
            (
                item
                for item in (_prompts_cur.metadata().get("quality_gate") or {}).get("checks", [])
                if item["id"] == "prompts.feature.count"
            ),
            None,
        )
        _count_detail = (_count_check or {}).get("detail") or ""
        check(
            "结构校验：prompts 的 gate 拿到了 PRD 上下文（功能数那条不是 skipped）",
            _count_check is not None and "skipped" not in _count_detail,
            _count_detail or "没有 prompts.feature.count 这条检查",
        )

        # (4) 优化失败：只把**已收到的那一节**拼回整篇，不能把整篇替换成片段
        boom_job = jobs.create_job(
            doc_session,
            opt_artifact,
            {**optimize_payload, "section": "## 1. 产品概述", "current_content": "## 1. 产品概述\n\n旧概述"},
        )

        class _BoomOptimize:
            async def astream(self, messages: Any) -> Any:
                yield _Chunk("## 1. 产品概述\n\n写到一半")
                raise RuntimeError("模拟优化失败")

            async def ainvoke(self, messages: Any) -> Any:  # pragma: no cover
                raise RuntimeError("模拟优化失败")

        boom_events: list[dict[str, Any]] = []

        async def run_boom_optimize() -> None:
            with job_bus.subscription(boom_job) as sub:
                task = start_job(
                    boom_job,
                    jobs=jobs,
                    sessions=sessions,
                    documents=DocumentService(model=_BoomOptimize()),
                )
                while True:
                    item = await sub.queue.get()
                    if item.get("type") == job_bus.STREAM_END_TYPE:
                        break
                    boom_events.append(item)
                await task

        asyncio.run(run_boom_optimize())
        boom_record = jobs.get_job(boom_job)
        boom_session = json.loads(sessions.get_session(doc_session).session_data)
        check(
            "优化失败：status=failed + error 落库",
            boom_record.status == "failed" and "模拟优化失败" in (boom_record.error or ""),
            boom_record.error or "",
        )
        check(
            "优化失败：**整篇结构还在**（半成品那一节拼回去，不是把整篇换成片段）",
            boom_session["documents"]["prd"]["content"].startswith("# 群聊周报助手")
            and "写到一半" in boom_session["documents"]["prd"]["content"]
            and "## 2. 功能需求" in boom_session["documents"]["prd"]["content"],
            boom_session["documents"]["prd"]["content"].replace("\n", "\\n")[:70],
        )
        check(
            "优化失败：viewState 仍在 review-prd、activeJobId 清空",
            boom_session.get("viewState") == "review-prd"
            and boom_session.get("activeJobId") is None,
            f"viewState={boom_session.get('viewState')}",
        )
        check(
            "优化失败：也发 error + run_summary",
            any(item["type"] == "error" for item in boom_events)
            and any(item["type"] == "run_summary" for item in boom_events),
            "、".join(item["type"] for item in boom_events),
        )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- 在线段


def _read_sse(response: httpx.Response, *, stop_after: int | None = None) -> list[tuple[str, Any]]:
    """按帧读 SSE，直到 `[DONE]`（或最多 `stop_after` 帧后主动断开 —— 模拟客户端离开）。"""
    events: list[tuple[str, Any]] = []
    event = ""
    for line in response.iter_lines():
        if line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            payload = line[5:].strip()
            if payload == "[DONE]":
                events.append(("DONE", None))
                break
            events.append((event, json.loads(payload)))
            if stop_after is not None and len(events) >= stop_after:
                break
    return events


def live(only: list[str] | None = None) -> None:
    """真 HTTP + 真模型：三种产物各一个任务，其中接口文档那条**中途断开订阅**。

    `only` 用来只跑其中几种产物（改一处实现后重跑时不必把三种都重新生成一遍 ——
    每次生成都要真花钱）。
    """
    with httpx.Client(base_url=API_BASE, timeout=600.0) as client:
        # 造一个会话（任务必须挂在存在的会话上）
        created = client.post(
            "/api/session/save",
            json={
                "session_data": {
                    "sessionId": "live",
                    "formVersion": "1.2",
                    "form": {"productName": "群聊周报助手"},
                    "messages": [{"role": "user", "content": "给 20 人以下创业团队做群聊周报助手"}],
                    "roundIndex": 2,
                    "documents": {},
                    "viewState": "chatting",
                    "updatedAt": "2026-09-30T00:00:00.000+00:00",
                }
            },
        )
        created.raise_for_status()
        session_id = created.json()["id"]
        print(f"  会话：{session_id}")

        prd_fixture = (BACKEND_DIR / "validation_out" / "prd_skill.txt").read_text(encoding="utf-8")
        summary = {
            "product_name": "群聊周报助手",
            "product_goal": "把周报整理从两小时降到十分钟",
            "target_users": ["20 人以下创业团队的技术负责人"],
            "platform": "Web 网站",
            "mvp_features": [
                {"name": "绑定群聊并汇总", "description": "选定范围后汇总一周消息"},
                {"name": "按人生成周报草稿", "description": "每人一份草稿"},
            ],
            "core_flows": ["选择群聊 → 选择周期 → 生成草稿 → 编辑 → 导出"],
            "tech_constraints": ["Web 前端 + 后端服务", "不接第三方 IM 的私有协议"],
            "non_functional": ["单次汇总 20 人群聊一周消息 < 60 秒"],
        }

        for artifact, payload in (
            ("prd", {"requirements_summary": summary}),
            ("api-docs", {"prd_content": prd_fixture}),
            ("prompts", {"prd_content": prd_fixture}),
        ):
            if only and artifact not in only:
                continue
            print(f"\n=== {artifact} ===")
            bad = client.post(
                "/api/jobs",
                json={"session_id": session_id, "artifact": artifact, "payload": {"prd_content": "   "}},
            )
            if artifact != "prd":
                check(f"[{artifact}] payload 缺必需内容 → 422（开流前就拦下）", bad.status_code == 422, str(bad.status_code))

            made = client.post(
                "/api/jobs", json={"session_id": session_id, "artifact": artifact, "payload": payload}
            )
            if made.status_code == 409:
                check(f"[{artifact}] 409 只在真有任务在跑时出现", False, made.text[:120])
                continue
            made.raise_for_status()
            job_id = made.json()["job_id"]
            print(f"  job_id={job_id}")

            duplicate = client.post(
                "/api/jobs", json={"session_id": session_id, "artifact": artifact, "payload": payload}
            )
            check(
                f"[{artifact}] 同会话 + 同产物重复创建 → 409（防连点）",
                duplicate.status_code == 409,
                str(duplicate.status_code),
            )

            # 快照：刷新页面时先调它，页面不空白
            snap = client.get(f"/api/jobs/{job_id}")
            snap.raise_for_status()
            check(
                f"[{artifact}] GET /api/jobs/{{id}} 给出 status/phase",
                snap.json()["status"] in ("pending", "running", "completed"),
                f"{snap.json()['status']}/{snap.json()['phase']}",
            )

            # 订阅：**故意在收到几帧后断开**（api-docs 那条），验证任务不受影响
            stop_after = 5 if artifact == "api-docs" else None
            started = time.monotonic()
            frames: list[tuple[str, Any]] = []
            with client.stream("GET", f"/api/jobs/{job_id}/stream") as response:
                response.raise_for_status()
                frames = _read_sse(response, stop_after=stop_after)
            elapsed = time.monotonic() - started
            names = [name for name, _ in frames]
            check(
                f"[{artifact}] 首帧是 snapshot（带已有草稿）",
                bool(frames) and names[0] == "snapshot",
                "、".join(names[:6]),
            )

            if stop_after is not None:
                print(f"  已在收到 {len(frames)} 帧后主动断开（模拟关页面/刷新）")
                # 断开之后等任务自己跑完 —— 这一步就是需求 §十三.3 的验收
                deadline = time.monotonic() + 480
                status = "running"
                while time.monotonic() < deadline:
                    status = client.get(f"/api/jobs/{job_id}").json()["status"]
                    if status in ("completed", "failed"):
                        break
                    time.sleep(2)
                check(
                    "[api-docs] 订阅断开后任务仍然跑完（**断开不 cancel**）",
                    status == "completed",
                    f"最终 status={status}（断开时已过 {elapsed:.1f}s）",
                )
                # 收尾后重连：应当重放结论而不是空等
                with client.stream("GET", f"/api/jobs/{job_id}/stream") as response:
                    replayed = _read_sse(response)
                replayed_names = [name for name, _ in replayed]
                check(
                    "[api-docs] 收尾后重连：重放 run_summary + done + [DONE]",
                    replayed_names[0] == "snapshot"
                    and "done" in replayed_names
                    and replayed_names[-1] == "DONE",
                    "、".join(replayed_names),
                )
                done_payload = next(data for name, data in replayed if name == "done")
                final_api = client.get(f"/api/jobs/{job_id}").json()
                check(
                    "[api-docs] 重放出来的 done 与落库的终稿一致",
                    # `result` 已经由接口解析成对象（`JobSnapshotView.result`），
                    # 这里**不要再 `json.loads` 一次** —— 那会 TypeError（实测踩到）。
                    done_payload.get("content") == (final_api.get("result") or {}).get("content")
                    == final_api["draft_content"],
                    f"{len(str(done_payload.get('content') or ''))} 字符",
                )
            else:
                check(
                    f"[{artifact}] 结束有 done + run_summary + [DONE]",
                    "done" in names and "run_summary" in names and names[-1] == "DONE",
                    "、".join(names[-4:]),
                )
                check(
                    f"[{artifact}] run_summary 在 done 之前",
                    names.index("run_summary") < names.index("done"),
                )
                payload_done = next(data for name, data in frames if name == "done")
                check(
                    f"[{artifact}] done 带正文与截断标记",
                    bool((payload_done.get("content") or payload_done.get("final_prd")))
                    and "truncated" in payload_done,
                    f"{len(payload_done.get('content') or payload_done.get('final_prd') or '')} 字符"
                    f"｜truncated={payload_done.get('truncated')}",
                )
                if artifact == "prd":
                    check(
                        "[prd] 运行中有 phase 事件（写 / 审 / 改）",
                        any(name == "phase" for name in names),
                        "、".join(data.get("phase", "") for name, data in frames if name == "phase"),
                    )
                    check(
                        "[prd] 运行中有 review 事件（审查结论）",
                        any(name == "review" for name in names),
                    )

            final = client.get(f"/api/jobs/{job_id}").json()
            check(
                f"[{artifact}] 终态是 completed（error={final.get('error')}）",
                final["status"] == "completed",
                f"{final['status']}/{final['phase']}",
            )

            # 会话同步
            session_snap = json.loads(client.get(f"/api/session/{session_id}").json()["session_data"])
            doc_key = {"prd": "prd", "api-docs": "api", "prompts": "prompts"}[artifact]
            check(
                f"[{artifact}] 会话已同步：activeJobId 清空 + 落回 review-*",
                session_snap.get("activeJobId") is None
                and session_snap.get("viewState") == f"review-{doc_key if doc_key != 'api' else 'api-docs'}",
                f"viewState={session_snap.get('viewState')}",
            )
            check(
                f"[{artifact}] 终稿写进会话的 documents.{doc_key}.content",
                bool((session_snap.get("documents", {}).get(doc_key, {}) or {}).get("content")),
            )

        # 任务不存在 → 404
        missing = client.get("/api/jobs/does-not-exist")
        check("不存在的任务 → 404", missing.status_code == 404, str(missing.status_code))
        missing_stream = client.get("/api/jobs/does-not-exist/stream")
        check("不存在的任务订阅 → 404（不是 200 + 流里报错）", missing_stream.status_code == 404)


# ---------------------------------------------------------------- 入口


def main() -> int:
    global API_BASE

    parser = argparse.ArgumentParser(description="Generation Job 验收")
    parser.add_argument("--live", action="store_true", help="跑在线段（真模型，会花钱）")
    parser.add_argument("--api-base", default=None, help=f"后端地址（默认 {API_BASE}）")
    parser.add_argument(
        "--only",
        default=None,
        help="在线段只跑这些产物（逗号分隔：prd,api-docs,prompts）。不传 = 三种都跑",
    )
    args = parser.parse_args()

    if args.api_base:
        API_BASE = args.api_base
    only = [item.strip() for item in args.only.split(",")] if args.only else None

    print("=" * 70)
    print("离线段：仓储 / 服务 / 广播 / runner / 会话同步 / 降级（假模型，不花钱）")
    print("=" * 70)
    offline()

    if args.live:
        print()
        print("=" * 70)
        print(f"在线段：真 HTTP + 真模型（{API_BASE}）—— 会花钱，三种产物各一次生成")
        print("=" * 70)
        live(only)
    else:
        print("\n（在线段未跑：加 --live 才会真调模型）")

    print()
    print(f"通过 {_PASS} 项，失败 {_FAIL} 项")
    if _FAIL:
        print("FAILED")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
