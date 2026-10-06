"""LLM 观测的三样东西：步骤枚举、单次调用的账、整趟流程的总账 + 收集器。

## 三个概念

| | 是什么 |
| --- | --- |
| `LlmStep` | **这一步在干什么**。全文统一用枚举：各写各的字符串时，`grep '"step":"prd_review"'` 会漏掉 `"PRD_REVIEW"` 那种拼法，而漏掉的正是要排查的那次调用 |
| `StepMetrics` | **一次** LLM 调用的一笔账：模型名、输入/输出 token、耗时、成功与否 |
| `RunMetrics` | **一份产物**的总账 + 逐笔详单（单片时就是这一片的账，分片时是各片合并后的整份账） |

## 一份产物 = 一个 run（跨分片合并）

接口文档与提示词套件**装不下单次输出上限**，前端按 `document-plan` 循环发 3 次
SSE 请求。而在合并之前，`run_summary` 是"每请求一份"：界面上显示的是**最后一片**
的统计，`request_id` 也只是最后一片的 —— 维护者按它检索日志只能查到 1/3 的步骤。

所以观测层多了一层：**分片（一次 HTTP 请求）→ run（一份产物）**。

- 前端开始生成一份产物时生成一个 uuid，三次请求都带 `X-Run-ID`
  （另带 `X-Part-Index` / `X-Part-Total`），中间件把它写进上下文；
- 每片结束时 `finalize_run()` 把这一片并进 `RunMetricsStore`，返回**整份口径**的
  `RunMetrics` —— `run_summary` 帧与日志用的都是它；
- 合并按 `request_id` **幂等**（重试/重放是替换不是累加，token 不会翻倍）；
  墙钟耗时取 run 的**水位跨度**（见过的最早开始 → 最晚结束，只增不减；不是相加：
  相加等于把前端在两次请求之间的等待重复计一遍，而"整份生成花了多久"问的是墙钟，
  用 `perf_counter()` 记 —— Windows 的 `monotonic()` 粒度 15.6ms，会把窗口压成 0）；
- 没有 `X-Run-ID` 时**一个字节都不写 store**：澄清聊天每轮一个请求，塞进去只会
  把内存撑爆，而且永远等不到 `complete`。

## 日志形态：stdout 单行 JSON

- 每次调用一行 `event=llm_step`（带 `run_id` 与 `part`）
- 每片结束一行 `event=run_summary`（最后一片那行的 `complete=true`）

两类行都带 `run_id`，所以既能按整份查、也能按片查：

    grep '"run_id":"<id>"' app.log

## 没有 active run 时怎么办

**照打 `llm_step` 日志，跳过累计，绝不抛异常。** 观测是旁路：某个脚本直接调 service
（不经过 API 层、没人 `start()`）时，业务必须照常跑完 —— 因为观测没准备好就把业务打挂，
是把"少一份账"升级成"用不了"。

## token 取不到是常态，不是失败

DeepSeek 等厂商在**流式**场景通常不返回 usage，此时 `input/output_tokens` 记 0，
但 `duration_ms` 一定拿得到（自己掐表）—— 所以"哪一步最慢"永远能回答，
"哪一步 input 最大"在流式场景可能只有非流式那几步有数。这一点写在字段说明里，
免得看日志的人以为是 bug。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from contextvars import ContextVar, Token
from enum import Enum
from typing import Any, Mapping, Sequence

from pydantic import BaseModel, Field

from core.request_context import (
    current_part_label,
    current_run_id,
    get_part_index,
    get_part_total,
    get_request_id,
    get_run_id,
    get_session_id,
)

logger = logging.getLogger("harnessprd.llm")


class LlmStep(str, Enum):
    """步骤标签。**新增调用点必须往这里加**，不要在业务里写字面量。"""

    CHAT_START = "chat_start"
    CHAT_CONTINUE = "chat_continue"
    SUMMARY_SYNC = "summary_sync"
    JSON_REPAIR = "json_repair"
    JSON_REGENERATE = "json_regenerate"
    PRD_WRITER = "prd_writer"
    PRD_REVIEW = "prd_review"
    PRD_REWRITE = "prd_rewrite"
    API_DOCS_GENERATE = "api_docs_generate"
    PROMPTS_GENERATE = "prompts_generate"
    DOCUMENT_OPTIMIZE = "document_optimize"


class StepMetrics(BaseModel):
    """一次 LLM 调用的一笔账。"""

    step: str
    model: str
    input_tokens: int = 0
    """流式无 usage 时为 0（不是失败）。"""
    output_tokens: int = 0
    duration_ms: int
    """wall time。**一定有值** —— 自己掐表，不依赖厂商返回。"""
    status: str = "ok"
    error: str = ""
    part_index: int | None = None
    """这一笔属于第几片（单片 / 离线脚本为 `None`）。

    合并后的 `steps` 是各片按序拼接的，没有这个字段就看不出"第 3 片失败了"，
    只能靠耗时去猜 —— 而排查分片问题时第一句话就是"是哪一片"。"""
    part_total: int | None = None


class RunMetrics(BaseModel):
    """一份产物的总账。由 `finalize_run()` 产出（单片时就是这一片）。"""

    request_id: str
    """**本片**的请求工号。"""
    run_type: str
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_duration_ms: int = 0
    llm_call_count: int = 0
    steps: list[StepMetrics] = Field(default_factory=list)
    revision_applied: bool | None = None
    budget: dict[str, Any] | None = None
    """最后一次预算检查的完整结果（含 budget_source / policy_cap / hardware_cap）。"""

    injected_skills: list[dict[str, Any]] | None = None
    """这趟**注入进提示词**的技能包清单，形如
    `[{"skill_id": "api-docs-generator", "version": "1.0.0", "artifacts": ["instructions.md", …]}]`。

    用途：事后回答"这份产物是按哪一版规范生成的"。记录点只有一处 ——
    `document_service` 取 bundle 的那几个函数（见 `skill_loader.bundle_to_injected_meta`）。

    ⚠️ 跨分片取**并集**（见 `RunTotals.summary`）：每片都会注入同一批文件，
    只留最后一片的值在正常情况下也对，但并集能容忍"某片少注入了一份"这种真出问题的情况。
    `None` = 这趟没有技能包（澄清聊天等纯对话 run 本来就没有），与"空列表"区分开。
    """

    # ---------- 跨分片（一份产物 = 一个 run）----------
    # 全部可选：单片请求（澄清聊天、PRD 生成、修订）没有这些信息也不该多出账。
    run_id: str | None = None
    """整份生成的工号。没有 `X-Run-ID` 时退化成 `request_id`（单片 run）。"""
    request_ids: list[str] = Field(default_factory=list)
    """该 run 已合并的各片 `request_id`，按分片序号。"""
    part_index: int | None = None
    """**本片**是第几片（1 基）。"""
    part_total: int | None = None
    """总片数。各片上报不一致时取最大值（宁可显示"未完成"，也不谎报完成）。"""
    parts_seen: int = 0
    """已合并到几片（按 `request_id` 去重后的数量）。"""
    complete: bool = False
    """整份是否收齐（`parts_seen >= part_total`）。"""
    part_duration_ms: int | None = None
    """**本片**的耗时；`total_duration_ms` 是整份墙钟 —— 两者刻意分开，
    否则"最后一片很快"会被读成"整份很快"。"""
    started_at: float | None = Field(default=None, exclude=True)
    ended_at: float | None = Field(default=None, exclude=True)
    """内部字段（`exclude=True`：**不进日志、不进 SSE**）。

    ⚠️ 用 `time.perf_counter()` 记，**不是 `time.monotonic()`**：Windows 上
    `monotonic()` 是 `GetTickCount64`，粒度 15.6ms —— 一个跑几毫秒的请求，起止会落在
    **同一个刻度**上，窗口退化成 0，整份墙钟于是算成 0ms（实测踩到：三片请求的
    `started_at == ended_at`）。`perf_counter()` 同样是单调的，而且是全系统最高精度，
    量区间本来就该用它。"""

    def to_dict(self, *, brief: bool = False) -> dict[str, Any]:
        """`brief=True` 时 steps 只留摘要字段（给 SSE 用）——完整细节走日志。"""
        payload = self.model_dump()
        if brief:
            # 白名单要跟着 `StepMetrics` 走：漏一个字段，SSE 里就少一项，
            # 而前端拿不到时不会报错，只会显示成 0/未定义。
            keys = (
                "step",
                "model",
                "input_tokens",
                "output_tokens",
                "duration_ms",
                "status",
                "part_index",
                "part_total",
            )
            payload["steps"] = [{key: item[key] for key in keys} for item in payload["steps"]]
        return payload


class RunMetricsRun:
    """一趟流程的**本片**可变累加器（`RunMetrics` 是它的不可变产物）。

    "一趟"到这里还是"一次 HTTP 请求"（= 一个分片）；跨分片合并发生在 `finalize_run()`
    之后的 `RunMetricsStore` 里 —— 累加器只管自己这一片，不关心整份。
    """

    def __init__(self, run_type: str, request_id: str) -> None:
        self.run_type = run_type
        self.request_id = request_id
        self._started = time.perf_counter()
        # 本片耗时与跨片窗口**用同一个时钟**：两个单调时钟混用只会制造"两个数对不上"
        # 的疑问，而 `perf_counter()` 精度更高（见 `RunMetrics.started_at` 的说明）。
        self._started_at = self._started
        self._ended_at: float | None = None
        self._steps: list[StepMetrics] = []
        self._revision_applied: bool | None = None
        self._budget: dict[str, Any] | None = None
        # 按 skill_id 去重：分片生成时每片都要注入一次，直接 append 会让清单变成 3 份
        self._injected_skills: dict[str, dict[str, Any]] = {}
        self.context_usage: dict[str, Any] | None = None
        self._token: Token[RunMetricsRun | None] | None = None

    def add_step(self, record: StepMetrics) -> None:
        self._steps.append(record)

    def record_injected_skills(self, items: Sequence[Mapping[str, Any]]) -> None:
        """记下这趟注入的技能包（**幂等**：同一 skill 重复上报只并文件清单）。

        与 `add_step` 不同，这里按 `skill_id` 合并而不是追加 —— 分片生成时每片都会
        注入同一批规范，汇总要回答的是"这趟用了哪些规范"，不是"注入了几次"。
        """
        for item in items or ():
            skill_id = str(item.get("skill_id") or "")
            if not skill_id:
                continue
            row = self._injected_skills.setdefault(
                skill_id,
                {
                    "skill_id": skill_id,
                    "version": str(item.get("version") or ""),
                    "artifacts": [],
                },
            )
            for path in item.get("artifacts") or ():
                text = str(path)
                if text not in row["artifacts"]:
                    row["artifacts"].append(text)

    def set_budget(self, check: Any) -> None:
        """记下最后一次预算检查：全量给日志，四字段给前端。"""
        self._budget = check.to_log()
        self.context_usage = check.to_context_usage()

    def mark_revision(self, applied: bool) -> None:
        """PRD 是否触发了自动改写（业务侧知道，观测层不猜）。"""
        self._revision_applied = applied

    @property
    def step_names(self) -> list[str]:
        return [item.step for item in self._steps]

    def finish(self) -> RunMetrics:
        """收尾：算出本片的总账，并把上下文里的收集器**摘掉**（不清会让下一个请求继承上一趟）。

        ⚠️ 只摘收集器，**不清 `context_usage`** —— `done` 帧要在 `finish()` 之后
        把上下文占用塞进去（`api/conversation.py`），清了那份注入就没了。
        """
        if self._token is not None:
            RunMetricsCollector._current.reset(self._token)
            self._token = None
        self._ended_at = time.perf_counter()
        return RunMetrics(
            request_id=self.request_id,
            run_type=self.run_type,
            total_input_tokens=sum(item.input_tokens for item in self._steps),
            total_output_tokens=sum(item.output_tokens for item in self._steps),
            total_duration_ms=int((time.perf_counter() - self._started) * 1000),
            llm_call_count=len(self._steps),
            steps=list(self._steps),
            revision_applied=self._revision_applied,
            budget=self._budget,
            # 空 → None：与"有技能包但清单为空"区分开（后者不该出现，出现了就是 bug）
            injected_skills=list(self._injected_skills.values()) or None,
            started_at=self._started_at,
            ended_at=self._ended_at,
        )


class RunMetricsCollector:
    """当前流程的收集器（`contextvars` 持有，LLM 包装层直接 `current()` 拿）。"""

    _current: ContextVar[RunMetricsRun | None] = ContextVar("harnessprd_run_metrics", default=None)

    @classmethod
    def current(cls) -> RunMetricsRun | None:
        return cls._current.get()

    @classmethod
    def start(cls, run_type: str, *, request_id: str | None = None) -> RunMetricsRun:
        """开一趟。`request_id` 缺省取上下文里的（中间件已注入）。"""
        run = RunMetricsRun(run_type=run_type, request_id=request_id or get_request_id() or "req_local")
        run._token = cls._current.set(run)
        return run


# ---------------------------------------------------------------- 跨分片：一片 → 一个 run


class PartRecord(BaseModel):
    """一个分片（一次 HTTP 请求）的观测记录。

    `request_id` 是它在 run 里的**键**：同一个 `request_id` 重复上报是**替换**。
    重试/断线重连会让同一片被报两次，而累加会让 tokens 直接翻倍 ——
    那种数字比没有数字更糟（看起来像"模型多花了钱"）。
    """

    request_id: str
    part_index: int
    part_total: int
    run_type: str
    input_tokens: int = 0
    output_tokens: int = 0
    llm_call_count: int = 0
    duration_ms: int = 0
    steps: list[StepMetrics] = Field(default_factory=list)
    revision_applied: bool | None = None
    budget: dict[str, Any] | None = None
    injected_skills: list[dict[str, Any]] | None = None
    started_at: float | None = None
    ended_at: float | None = None
    last_at: float = 0.0
    """这一片**最后一次**上报的时刻（单调时钟）。只用于 TTL 与淘汰排序。"""

    @classmethod
    def from_summary(
        cls, part: RunMetrics, *, part_index: int, part_total: int, now: float
    ) -> PartRecord:
        """由**本片**的 `RunMetrics`（`finish()` 的产物）派生。

        字段名与 `RunMetrics` 一一对应：分片与整份是同一套语义的两层，
        换名字只会让人以为它们不是一回事。
        """
        return cls(
            request_id=part.request_id,
            part_index=part_index,
            part_total=part_total,
            run_type=part.run_type,
            input_tokens=part.total_input_tokens,
            output_tokens=part.total_output_tokens,
            llm_call_count=part.llm_call_count,
            duration_ms=part.total_duration_ms,
            steps=list(part.steps),
            revision_applied=part.revision_applied,
            budget=part.budget,
            injected_skills=list(part.injected_skills) if part.injected_skills else None,
            # 拿不到区间就退化成"就是此刻"，至少不会把并集算成 0
            started_at=part.started_at if part.started_at is not None else now,
            ended_at=part.ended_at if part.ended_at is not None else now,
            last_at=now,
        )


class RunTotals(BaseModel):
    """一个 run 下已合并的各片（`parts` 的键 = `request_id`）。

    **它不是第二套字段定义**：对外形态永远是 `RunMetrics`，这里只负责"攒片"，
    汇总口径只有 `summary()` 一处 —— 两处求和必然漂移。
    """

    run_id: str
    parts: dict[str, PartRecord] = Field(default_factory=dict)
    last_at: float = 0.0
    first_started_at: float | None = None
    """**水位**：这个 run 见过的**最早**开始时刻。"""
    last_ended_at: float | None = None
    """**水位**：这个 run 见过的**最晚**结束时刻。

    为什么用水位，而不是"从现存各片现算"：同一片重放（重试 / 断线重连）会**替换**
    掉那条记录，现算的话跨度会**缩短** —— 可整份生成明明经历过那段时间。
    水位只增不减，才与"整份从第一片开始、到最后一片结束"对上（实测踩到：
    三片序列重放第一片后，跨度从 300ms 缩到 240ms）。"""

    def summary(self, part: PartRecord) -> RunMetrics:
        """整份口径的汇总；`part` 是**本次上报的那片**（本片语义的字段取它）。"""
        # 按分片序号排列：`request_ids` / `steps` 的顺序要对得上界面上的"第 N 片"，
        # 而到达顺序由前端循环决定（并发时可能与序号无关）。序号相同的极端情况
        # （同一片换了 request_id 重报）保持插入顺序。
        ordered = sorted(self.parts.values(), key=lambda item: item.part_index)
        # 墙钟 = 水位跨度（最早开始 → 最晚结束）。**不是相加**：相加会把片与片之间的
        # 等待重复计一遍，而"整份生成了多久"问的是墙钟。
        span = 0
        if self.first_started_at is not None and self.last_ended_at is not None:
            span = int((self.last_ended_at - self.first_started_at) * 1000)
        # 下界兜底：任何时钟都可能取不到值（`started_at` 缺失时退化成"就是此刻"），
        # 而"整份至少花了最慢那一片那么久"永远成立 —— 数字宁可保守，不可偏小。
        span = max(span, max((item.duration_ms for item in ordered), default=0))
        total_parts = max((item.part_total for item in ordered), default=1)
        budget = part.budget
        if budget is None:
            # 本片没走到预算检查（例如在检查之前就失败了）→ 退到最近一片的结论
            for item in reversed(ordered):
                if item.budget is not None:
                    budget = item.budget
                    break
        revisions = [item.revision_applied for item in ordered if item.revision_applied is not None]
        # 注入清单取**并集**（按 skill_id 合并、文件清单去重）。正常情况下各片一模一样，
        # 并集与"取最后一片"等价；差别只体现在真出问题时（某片漏注入了规范）。
        injected: dict[str, dict[str, Any]] = {}
        for item in ordered:
            for row in item.injected_skills or ():
                skill_id = str(row.get("skill_id") or "")
                if not skill_id:
                    continue
                merged = injected.setdefault(
                    skill_id,
                    {
                        "skill_id": skill_id,
                        "version": str(row.get("version") or ""),
                        "artifacts": [],
                    },
                )
                for path in row.get("artifacts") or ():
                    text = str(path)
                    if text not in merged["artifacts"]:
                        merged["artifacts"].append(text)
        return RunMetrics(
            request_id=part.request_id,
            run_type=part.run_type,
            total_input_tokens=sum(item.input_tokens for item in ordered),
            total_output_tokens=sum(item.output_tokens for item in ordered),
            total_duration_ms=span,
            llm_call_count=sum(item.llm_call_count for item in ordered),
            steps=[step for item in ordered for step in item.steps],
            # 任一触发过自动改写，就算整份改写过了（前端据此提示"已自动改写"）
            revision_applied=any(revisions) if revisions else None,
            budget=budget,
            injected_skills=list(injected.values()) or None,
            run_id=self.run_id,
            request_ids=[item.request_id for item in ordered],
            part_index=part.part_index,
            part_total=total_parts,
            parts_seen=len(ordered),
            complete=len(ordered) >= total_parts,
            part_duration_ms=part.duration_ms,
            started_at=self.first_started_at,
            ended_at=self.last_ended_at,
        )


def _store_limits() -> tuple[float, int]:
    """从配置读 TTL / 容量上限；**读不到就给安全默认值**。

    观测存储不该因为配置模块读不到而拒绝服务（那是把"少一份汇总"升级成"用不了"）。
    """
    try:
        from core.config import get_settings

        settings = get_settings()
        return (
            float(getattr(settings, "run_metrics_ttl_seconds", 1800.0)),
            int(getattr(settings, "run_metrics_max_runs", 256)),
        )
    except Exception:  # noqa: BLE001 - 配置问题不该影响观测本身
        return 1800.0, 256


class RunMetricsStore:
    """按 `run_id` 合并各片的进程内存储。

    **为什么放进程内存而不是 Redis/SQLite**：这份账的寿命只有"一次产物生成"那么长
    （默认 30 分钟 TTL），丢了只是少一份合并汇总（每片的 `llm_step` 日志仍在），
    而且单机进程内合并没有任何跨进程一致性问题。真要多实例部署时再换后端 ——
    那时改的也只是这个类。

    ⚠️ 同一进程里 FastAPI 的同步/异步执行体可能落在**不同线程**，所以进出都要加锁：
    地图本身不是线程安全的，而"偶发地把两片的账算错"是最难查的一类 bug。

    清理是**惰性**的（在下一次 `merge()` 时顺手做）：不为此起后台线程 ——
    进程空闲时留着几条过期记录只是几十字节，而起一个定时器的代价与出错面都更大。
    """

    def __init__(self, *, ttl_seconds: float | None = None, max_runs: int | None = None) -> None:
        default_ttl, default_max = _store_limits()
        self.ttl_seconds = default_ttl if ttl_seconds is None else ttl_seconds
        self.max_runs = default_max if max_runs is None else max_runs
        self._runs: dict[str, RunTotals] = {}
        self._lock = threading.Lock()

    def merge(
        self, part: RunMetrics, *, run_id: str, part_index: int, part_total: int
    ) -> RunMetrics:
        """并入一片，返回**整份口径**的汇总（本片字段取自 `part`）。"""
        now = time.perf_counter()
        record = PartRecord.from_summary(
            part, part_index=part_index, part_total=part_total, now=now
        )
        with self._lock:
            self._evict_expired(now)
            totals = self._runs.get(run_id)
            if totals is None:
                totals = RunTotals(run_id=run_id)
                self._runs[run_id] = totals
            # 幂等：同一 request_id 覆盖而不是追加（同一片重试/重放不该被算两次）
            totals.parts[record.request_id] = record
            # 水位只增不减：重放那片的窗口比原来早还是晚，都不改变"整份经历过的时间"
            if record.started_at is not None and (
                totals.first_started_at is None or record.started_at < totals.first_started_at
            ):
                totals.first_started_at = record.started_at
            if record.ended_at is not None and (
                totals.last_ended_at is None or record.ended_at > totals.last_ended_at
            ):
                totals.last_ended_at = record.ended_at
            totals.last_at = now
            self._evict_overflow()
            return totals.summary(record)

    def run_count(self) -> int:
        """当前存了几个 run（自检用；先清一遍过期项，报的是**有效**数量）。"""
        with self._lock:
            self._evict_expired(time.perf_counter())
            return len(self._runs)

    def clear(self) -> None:
        """清空（自检用：让断言不受上一个用例残留的影响）。"""
        with self._lock:
            self._runs.clear()

    def _evict_expired(self, now: float) -> None:
        """TTL 过期清理。调用方必须已持锁。"""
        if self.ttl_seconds <= 0:
            return
        stale = [
            key for key, totals in self._runs.items() if now - totals.last_at > self.ttl_seconds
        ]
        for key in stale:
            self._runs.pop(key, None)

    def _evict_overflow(self) -> None:
        """超出上限时按 `last_at` 淘汰最旧的。调用方必须已持锁。

        淘汰最旧而不是最不完整：一个卡住的 run（前端崩了、只发了 1/3 片）
        会一直占着位置，而它的 `last_at` 最老 —— 正好先被清掉。
        """
        if self.max_runs <= 0:
            return
        while len(self._runs) > self.max_runs:
            oldest = min(self._runs.items(), key=lambda item: item[1].last_at)[0]
            self._runs.pop(oldest, None)


run_store = RunMetricsStore()
"""进程级单例：两个 SSE 生成器都通过 `finalize_run()` 用它，不各自持有一个。"""


def finalize_run(run: RunMetricsRun) -> RunMetrics:
    """收尾一趟流程，并合并成整份口径 —— `run_summary` 的**唯一出口**。

    - 带了 `X-Run-ID`：并进 `run_store`，返回整份（含各片）的汇总；
    - 没带：**不碰 store**，run id 退化成 `request_id`、按 `1/1` 单片处理。
      澄清聊天每轮都是独立请求，往 store 里塞记录只会把它撑爆，
      而且那些 run 永远等不到 `complete`。
    """
    summary = run.finish()
    run_id = get_run_id()
    if not run_id:
        return summary.model_copy(
            update={
                "run_id": summary.request_id,
                "request_ids": [summary.request_id],
                "part_index": 1,
                "part_total": 1,
                "parts_seen": 1,
                "complete": True,
                "part_duration_ms": summary.total_duration_ms,
            }
        )
    return run_store.merge(
        summary,
        run_id=run_id,
        # 头缺失时按单片算：`X-Part-Index` 单独出现（没有总数）没有意义，
        # 与其猜一个总数，不如按 `1/1` 如实报。
        part_index=get_part_index() or 1,
        part_total=get_part_total() or 1,
    )


def extract_tokens(payload: Any) -> tuple[int, int]:
    """从响应/分片里取 `(input, output)`；取不到返回 `(0, 0)`。

    两种形态都要认（实测两套都出现过）：
    - LangChain 标准的 `usage_metadata`：`{"input_tokens": n, "output_tokens": m}`
    - OpenAI 兼容的 `response_metadata["token_usage"]`：`prompt_tokens` / `completion_tokens`
    """
    if payload is None:
        return 0, 0
    usage = getattr(payload, "usage_metadata", None)
    if not isinstance(usage, dict) or not usage:
        metadata = getattr(payload, "response_metadata", None)
        if isinstance(metadata, dict):
            usage = metadata.get("token_usage") or metadata.get("usage") or {}
    if not isinstance(usage, dict):
        return 0, 0
    inputs = usage.get("input_tokens", usage.get("prompt_tokens", 0)) or 0
    outputs = usage.get("output_tokens", usage.get("completion_tokens", 0)) or 0
    try:
        return int(inputs), int(outputs)
    except (TypeError, ValueError):
        return 0, 0


def _metrics_log_enabled() -> bool:
    """逐步日志开关（`LOG_LLM_METRICS`）。读不到配置时按**打开**处理：宁可多打，不要静默丢账。"""
    try:
        from core.config import get_settings

        return bool(getattr(get_settings(), "log_llm_metrics", True))
    except Exception:  # noqa: BLE001 - 配置问题不该影响观测本身
        return True


def record_step(
    *,
    step: LlmStep,
    model: str,
    usage: Any,
    duration_ms: int,
    status: str = "ok",
    error: str = "",
) -> StepMetrics:
    """记一笔：进总账 + 打一行 JSON 日志。观测的唯一入口。"""
    input_tokens, output_tokens = extract_tokens(usage)
    record = StepMetrics(
        step=step.value,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        duration_ms=duration_ms,
        status=status,
        error=error,
        # 分片归属在**记录时**定下来：合并后的 steps 里要能看出这一步属于哪一片
        part_index=get_part_index(),
        part_total=get_part_total(),
    )
    run = RunMetricsCollector.current()
    if run is not None:
        run.add_step(record)
    else:
        # 没有 active run：只打日志，**不**为了记账去造一个 run（那会让"这趟跑了几步"失真）
        logger.debug("没有 active run，本次不再累计（step=%s）", record.step)
    if status == "error":
        # 失败**永远**打日志（即使逐步日志关着）：出错的账不能因为开关而丢
        _emit(record, level=logging.WARNING)
    elif _metrics_log_enabled():
        _emit(record, level=logging.INFO)
    else:
        logger.debug("LOG_LLM_METRICS=false，跳过 llm_step 日志：%s", record.step)
    return record


def record_injected_skills(items: Sequence[Mapping[str, Any]]) -> None:
    """把"这趟注入了哪些 skill 文件"记进当前 run 的汇总（`run_summary.injected_skills`）。

    与 `record_step` 走同一条路子：调用方（`document_service` 取 bundle 的那几处）
    不必持有收集器。没有 active run 时静默跳过 —— **不为记账去造一个 run**，
    那会让"这趟跑了几步、花了多少"失真（同 `record_step` 的处理）。
    """
    run = RunMetricsCollector.current()
    if run is None:
        logger.debug("没有 active run，技能包注入不记账（%d 项）", len(items or ()))
        return
    run.record_injected_skills(items)


def _emit(record: StepMetrics, *, level: int) -> None:
    """打一行单行 JSON（`ensure_ascii=False`：中文模型名/错误信息保持可读）。

    `run_id` 与 `part` 是**跨分片排查的入口**：一份产物由 3 次请求生成时，
    按 `request_id` 只能查到 1/3，而 `grep '"run_id":"<id>"'` 一次拿到全部步骤。
    没有 `X-Run-ID` 时 `run_id` 退化成 `request_id`（单片 run 也能这么捞）。
    """
    payload = {
        "event": "llm_step",
        "request_id": get_request_id(),
        "run_id": current_run_id(),
        "part": current_part_label(),
        "session_id": get_session_id(),
        **record.model_dump(),
    }
    logger.log(level, json.dumps(payload, ensure_ascii=False))


def log_run_summary(run: RunMetrics) -> None:
    """整份结束打一行 `event=run_summary`（**不受逐步开关影响**：一趟一行，成本可忽略）。

    字段就是 `RunMetrics` 全体：`run_id` / `parts_seen` / `complete` / `request_ids`
    都在里面，所以日志与 SSE 是**同一份口径**，不会出现"界面显示 3 片、日志只有 1 片"。
    """
    payload = {
        "event": "run_summary",
        "session_id": get_session_id(),
        **run.to_dict(brief=True),
    }
    logger.info(json.dumps(payload, ensure_ascii=False))
