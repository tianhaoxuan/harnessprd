"""请求级上下文：把「这次请求是谁」放进 `contextvars`，供任意深度的调用读取。

## 为什么用 contextvars 而不是 threading.local

FastAPI 的同步路由跑在线程池、异步路由跑在事件循环，同一个线程会先后服务不同请求 ——
`threading.local` 在这种模型下**必然串号**。`contextvars` 是按**执行上下文**隔离的，
并且会随 `asyncio` 任务创建自动复制，正好匹配"一次请求一条链"的语义。

## 为什么要有它

LLM 调用散落在多个 service 里，如果每个调用点都靠参数把 request_id 一层层传下去，
那就要改一堆业务函数签名（而这个改造的前提是**不动业务逻辑**）。放进上下文之后，
底层包装层直接 `get_request_id()` 就能拿到，业务代码一行不用改。

写入口只有 `middleware/request_id.py` 一处；这里只提供读写原语。

## request_id 之外还有「run」与「分片」

一份产物（接口文档 / 提示词套件）装不下单次输出上限，前端按 `document-plan` 循环
发 **3 次** SSE 请求 —— 于是「一次 HTTP 请求」不再等于「一次产物生成」。所以除了
`request_id`（本片工号），还要有：

- `run_id`：整份生成的工号（前端带 `X-Run-ID`，三次请求同一个值）。**它才是
  "这次生成"的身份**，`grep '"run_id":"..."'` 能一次拿到三片的全部步骤；
- `part_index` / `part_total`：本片是第几片、共几片（`X-Part-Index` / `X-Part-Total`）。

三者都**可选**：缺席时按"单片 run"处理（run id 退化成 request_id、分片为 `1/1`）——
澄清聊天每轮就是一个请求，不该被当成"没写完的 run"。

校验放在写入口（`middleware/request_id.py`）而不是这里：非法值要记一条 warning，
而 logger 在那边；这里只留**纯粹的取值规则**，便于离线自检直接调用。
"""

from __future__ import annotations

import re
import uuid
from contextvars import ContextVar, Token

_request_id: ContextVar[str | None] = ContextVar("harnessprd_request_id", default=None)
_session_id: ContextVar[str | None] = ContextVar("harnessprd_session_id", default=None)
_run_id: ContextVar[str | None] = ContextVar("harnessprd_run_id", default=None)
_part_index: ContextVar[int | None] = ContextVar("harnessprd_part_index", default=None)
_part_total: ContextVar[int | None] = ContextVar("harnessprd_part_total", default=None)

RUN_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
"""`X-Run-ID` 的合法形态。

**必须收窄**：这个值会进 JSON 日志、会被拿去 `grep`，还会当 `RunMetricsStore` 的键
（跨请求共享的进程内字典）。放任任意字符串进来，等于让请求头决定日志里出现什么。
64 位足够放 uuid（32 位十六进制）与前端自己拼的前缀。"""


def new_request_id() -> str:
    """生成一个请求工号：`req_` + 12 位十六进制（够用且短，日志里好读）。"""
    return f"req_{uuid.uuid4().hex[:12]}"


def set_request_id(value: str | None) -> Token[str | None]:
    return _request_id.set(value)


def reset_request_id(token: Token[str | None]) -> None:
    _request_id.reset(token)


def get_request_id() -> str | None:
    """当前请求工号；不在请求里（脚本、后台任务）时为 `None`。

    **不返回占位字符串**：日志里出现 `req_unknown` 会让人以为真有这么个请求，
    而 `null` 明确表示"这条日志不在一次 HTTP 请求里"（例如离线自检脚本）。
    """
    return _request_id.get()


def set_session_id(value: str | None) -> Token[str | None]:
    return _session_id.set(value)


def reset_session_id(token: Token[str | None]) -> None:
    _session_id.reset(token)


def get_session_id() -> str | None:
    """当前会话 id（前端可选通过 `X-Session-ID` 头带上来）。"""
    return _session_id.get()


def normalize_run_id(value: str | None) -> str | None:
    """把 `X-Run-ID` 的原文收成合法值；不合法返回 `None`（= 当没给）。

    **不抛异常**：观测头是旁路，写错了最多是"合并没发生"，绝不能让请求失败。
    调用方（中间件）负责对"给了但不合法"记一条 warning —— 静默忽略会让前端
    带着错的头跑一整轮而没人知道。
    """
    candidate = (value or "").strip()
    if not candidate:
        return None
    return candidate if RUN_ID_PATTERN.fullmatch(candidate) else None


def normalize_part_number(value: str | None) -> int | None:
    """把分片序号/总数收成正整数；非法返回 `None`（= 当没给 → 按 `1/1`）。"""
    candidate = (value or "").strip()
    if not candidate.isdigit():
        return None
    number = int(candidate)
    return number if number >= 1 else None


def set_run_id(value: str | None) -> Token[str | None]:
    return _run_id.set(value)


def reset_run_id(token: Token[str | None]) -> None:
    _run_id.reset(token)


def get_run_id() -> str | None:
    """本次请求带的 run id（**原样**，不带兜底）。

    需要"兜底成单片 run"的地方用 `current_run_id()`；需要判断"到底带没带"的地方
    （例如决定要不要写合并存储）必须用这个 —— 两者混用会让单片请求也往存储里塞记录。
    """
    return _run_id.get()


def set_part(index: int | None, total: int | None) -> tuple[Token[int | None], Token[int | None]]:
    """写入分片信息（两个 contextvar 一次性设好，避免只设了一半的中间态）。"""
    return _part_index.set(index), _part_total.set(total)


def reset_part(index_token: Token[int | None], total_token: Token[int | None]) -> None:
    _part_total.reset(total_token)
    _part_index.reset(index_token)


def get_part_index() -> int | None:
    return _part_index.get()


def get_part_total() -> int | None:
    return _part_total.get()


def current_run_id() -> str | None:
    """观测口径的 run id：没有 `X-Run-ID` 时**退化成 `request_id`**（单片 run）。

    这样日志里每一行 `llm_step` 都至少有一个 run id 可用 —— 否则单片生成
    （PRD、修订、澄清聊天）的日志就没法按批次捞，而那正是最常见的形态。
    """
    return _run_id.get() or _request_id.get()


def current_part_label() -> str:
    """`"3/3"` 形态的分片标签；没有分片信息时是单片 `"1/1"`。"""
    return f"{_part_index.get() or 1}/{_part_total.get() or 1}"
