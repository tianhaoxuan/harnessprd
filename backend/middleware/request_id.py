"""请求工号中间件：给每次 HTTP 请求一个 `request_id`，并在响应头里回传。

## 它做三件事

1. **入站**：请求头带 `X-Request-ID` 就用它（便于把前端/网关的链路打穿），没带就生成
   `req_{12 位}`；
2. **写上下文**：塞进 `core.request_context`，之后**任意深度**的 LLM 调用都能读到，
   不需要改任何业务函数签名（这是"业务逻辑不动"的前提）；
3. **出站**：响应头 `X-Request-ID` 原样回传 —— 前端出错时能把这串工号给用户/给我们，
   拿它 `grep` 日志就能捞出这一趟的所有 LLM 调用。

可选：前端可用 `X-Session-ID` 带会话 id，一并写进上下文，日志里就有 `session_id`。

## 一份产物 = 一个 run（分片观测）

接口文档 / 提示词套件是**按分片计划循环发多次请求**生成的，所以"一次 HTTP 请求"
不再等于"一次产物生成"。前端在开始生成一份产物时生成一个 uuid，三次请求都带
`X-Run-ID`，并带 `X-Part-Index` / `X-Part-Total` 说明这是第几片。中间件只负责
**解析 + 写上下文 + 回显**，合并是 `services/llm_metrics.py` 的事（分层：
中间件不 import services 的观测实现）。

这三个头都是**可选**的：不给就按"单片 run"处理（run id 退化成 request_id）。
但"给了却不合法"要留一条 warning —— 静默忽略会让前端带着错的头跑一整轮，
现象是"合并从来没生效"，而日志里一个字都没有。

## 为什么 `call_next` 之前设置就够

`BaseHTTPMiddleware` 用 anyio 把下游跑在一个**子任务**里，而 `contextvars` 在创建子任务时
会被**复制**进去 —— 所以这里 set 的值，路由、service、LLM 包装层都读得到 ✓。
反过来，下游里新 set 的值（例如 RunMetrics 收集器）不会回到这里，这也没关系：
中间件只负责"发工号 + 回传"，不需要读下游的状态。

⚠️ **必须在 `finally` 里 reset**：不 reset 的话，同一个线程后续处理的请求会继承上一个工号
（"串号"），而这正是这套观测最怕的失败模式 —— 日志看起来齐全，其实全串在一起。
"""

from __future__ import annotations

import logging

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from core.request_context import (
    new_request_id,
    normalize_part_number,
    normalize_run_id,
    reset_part,
    reset_request_id,
    reset_run_id,
    reset_session_id,
    set_part,
    set_request_id,
    set_run_id,
    set_session_id,
)

logger = logging.getLogger(__name__)

REQUEST_ID_HEADER = "X-Request-ID"
SESSION_ID_HEADER = "X-Session-ID"
RUN_ID_HEADER = "X-Run-ID"
PART_INDEX_HEADER = "X-Part-Index"
PART_TOTAL_HEADER = "X-Part-Total"


class RequestIdMiddleware(BaseHTTPMiddleware):
    """给请求发工号（本片 + 整份 run），并把它放进上下文与响应头。"""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        incoming = (request.headers.get(REQUEST_ID_HEADER) or "").strip()
        request_id = incoming or new_request_id()
        session_id = (request.headers.get(SESSION_ID_HEADER) or "").strip() or None

        raw_run_id = request.headers.get(RUN_ID_HEADER)
        run_id = normalize_run_id(raw_run_id)
        if run_id is None and (raw_run_id or "").strip():
            # 给了但不合法：当没给（不影响请求），但**必须留痕** ——
            # 否则"合并从来没生效"会被当成后端 bug 查很久。
            logger.warning(
                "忽略非法的 %s（只接受 [A-Za-z0-9_-]{1,64}）：%r → 该请求按单片 run 处理",
                RUN_ID_HEADER,
                raw_run_id.strip()[:64],
            )
        # 分片序号只在**同一个 run** 里才有意义：没有（合法的）X-Run-ID 时，
        # 这次请求就是一份独立产物。保留 `X-Part-*` 会让日志出现"part=1/3 但
        # run_summary 只有 1/1"这种自相矛盾的两行，排查时比没有分片信息更误导。
        part_index = normalize_part_number(request.headers.get(PART_INDEX_HEADER)) if run_id else None
        part_total = normalize_part_number(request.headers.get(PART_TOTAL_HEADER)) if run_id else None
        if (
            run_id is None
            and (
                (request.headers.get(PART_INDEX_HEADER) or "").strip()
                or (request.headers.get(PART_TOTAL_HEADER) or "").strip()
            )
        ):
            logger.warning(
                "%s / %s 在没有 %s 时无意义（该请求按单片 run 处理），已忽略",
                PART_INDEX_HEADER,
                PART_TOTAL_HEADER,
                RUN_ID_HEADER,
            )

        token_request = set_request_id(request_id)
        token_session = set_session_id(session_id)
        token_run = set_run_id(run_id)
        token_part = set_part(part_index, part_total)
        try:
            response = await call_next(request)
        finally:
            # 顺序与 set 相反，避免上下文栈错位（contextvars 的 reset 是后进先出）
            reset_part(*token_part)
            reset_run_id(token_run)
            reset_session_id(token_session)
            reset_request_id(token_request)

        response.headers[REQUEST_ID_HEADER] = request_id
        if run_id is not None:
            # 只回显**合法**的：非法值已被当成"没给"，回显它等于替前端确认了一个
            # 后端并未采用的 run 身份（前端会拿它去查日志，然后什么都查不到）。
            response.headers[RUN_ID_HEADER] = run_id
        return response
