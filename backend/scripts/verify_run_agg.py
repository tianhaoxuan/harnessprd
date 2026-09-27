"""独立验证：跨分片 run 合并（走**真实 SSE 生成器** + 假模型，不调真实模型、不花钱）。

为什么单独一份、不进 `smoke_check.py`：那份是"仓库自检"，这份是**验收**——
它按用户能感知的口径验（界面上的数字对不对、日志能不能一次查全），
并且要能和自检相互独立地证伪。

验三件事：
1. 同一 `X-Run-ID` 的两次分片请求：第二片的 `run_summary` 必须是**整份合计**；
2. 同一 `X-Request-ID` 重放（重试/重连）：**幂等**，tokens 不翻倍、片数不虚增；
3. 日志：`llm_step` 与 `run_summary` 每行都带 `run_id`，能一条 grep 查全整份。

用法（在 `backend/` 下）：`.venv\\Scripts\\python.exe scripts\\verify_run_agg.py`
"""

from __future__ import annotations

import asyncio
import json
import logging
import pathlib
import sys
from typing import Any

BACKEND_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

from fastapi.testclient import TestClient  # noqa: E402
from langchain_core.messages import AIMessage, AIMessageChunk  # noqa: E402

from core.config import Settings  # noqa: E402
from main import create_app  # noqa: E402
import services.document_service as document_service  # noqa: E402

RUN_ID = "verify_run_agg_0001"
REQ_1 = "verify_req_part1"
REQ_2 = "verify_req_part2"

INPUT_TOKENS = 120
OUTPUT_TOKENS = 340

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"[{'✓' if ok else 'X'}] {label}" + (f" —— {detail}" if detail else ""))
    if not ok:
        failures.append(label)


class _FakeChat:
    """假模型：两种调用形态都给，因为生成路径既可能 ainvoke 也可能 astream。"""

    model_name = "verify-fake"

    async def ainvoke(self, messages: Any, **kwargs: Any) -> AIMessage:
        return AIMessage(
            content="## 第 1 章 验证\n\n正文。",
            usage_metadata={
                "input_tokens": INPUT_TOKENS,
                "output_tokens": OUTPUT_TOKENS,
                "total_tokens": INPUT_TOKENS + OUTPUT_TOKENS,
            },
        )

    def astream(self, messages: Any, **kwargs: Any):
        async def gen():
            yield AIMessageChunk(content="## 第 1 章 验证\n\n")
            yield AIMessageChunk(
                content="正文。",
                usage_metadata={
                    "input_tokens": INPUT_TOKENS,
                    "output_tokens": OUTPUT_TOKENS,
                    "total_tokens": INPUT_TOKENS + OUTPUT_TOKENS,
                },
            )

        return gen()


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.lines: list[dict[str, Any]] = []

    def emit(self, record: logging.LogRecord) -> None:
        text = record.getMessage()
        if not text.startswith("{"):
            return
        try:
            self.lines.append(json.loads(text))
        except json.JSONDecodeError:
            pass


def _frames(raw: str) -> list[tuple[str, dict[str, Any]]]:
    """把 SSE 文本拆成 (事件名, payload)。只看 `event:`/`data:` 两行。"""
    out: list[tuple[str, dict[str, Any]]] = []
    event = ""
    for line in raw.splitlines():
        if line.startswith("event:"):
            event = line.split(":", 1)[1].strip()
        elif line.startswith("data:"):
            body = line.split(":", 1)[1].strip()
            if body and body != "[DONE]":
                try:
                    out.append((event, json.loads(body)))
                except json.JSONDecodeError:
                    pass
    return out


def _summary_of(raw: str) -> dict[str, Any]:
    got = [payload for name, payload in _frames(raw) if name == "run_summary"]
    return got[-1] if got else {}


def main() -> int:
    # 只替换模型构造：业务逻辑、分片计划、SSE 生成器全部走真实代码。
    # 三处都要打：`get_llm` 既可能从本模块取，也可能从工厂模块取（谁先 import 谁持有引用）。
    import services.llm_factory as llm_factory

    llm_factory.get_llm = lambda **kwargs: _FakeChat()  # type: ignore[assignment]
    llm_factory.get_tracked_llm = lambda step, **kwargs: llm_factory.TrackedChatModel(  # type: ignore[assignment]
        _FakeChat(), step
    )
    document_service.get_llm = lambda **kwargs: _FakeChat()  # type: ignore[assignment]

    capture = _Capture()
    logging.getLogger("harnessprd.llm").addHandler(capture)

    body = {"prd_content": "## 第 1 章 验证\n\n这是给假模型看的 PRD 正文。"}

    with TestClient(create_app(Settings(_env_file=None))) as client:
        # 浏览器预检：白名单里的 origin 必须放行这几个自定义头。
        #
        # ⚠️ origin 取**配置里的**，不要硬编码：开发期前端走 Vite 代理（`vite.config.ts`
        # 把 `/api` 转到 8000），浏览器看到的是同源请求，CORS 压根不参与；白名单只对
        # "直连后端"的部署形态有意义。所以这里验的是**配置承诺**，随便挑个地址（比如
        # `127.0.0.1:5173`）去验只会测出一个与产品无关的 400。
        origin = Settings(_env_file=None).cors_origins[0]
        preflight = client.options(
            "/api/v1/conversation/generate-api-docs-stream",
            headers={
                "Origin": origin,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type,x-run-id,x-part-index,x-part-total",
            },
        )
        allowed = preflight.headers.get("access-control-allow-headers", "").lower()
        check(
            "CORS 预检放行 X-Run-ID / X-Part-*（直连后端的部署形态下，浏览器才发得出这几个头）",
            preflight.status_code in (200, 204) and ("*" in allowed or "x-run-id" in allowed),
            f"origin={origin} status={preflight.status_code} allow-headers={allowed!r}",
        )
        rejected = client.options(
            "/api/v1/conversation/generate-api-docs-stream",
            headers={
                "Origin": "http://evil.example.com",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type,x-run-id",
            },
        )
        check(
            "白名单外的 origin 仍被拒（放宽头不能顺手把 origin 也放开）",
            rejected.status_code >= 400
            and not rejected.headers.get("access-control-allow-origin"),
            f"status={rejected.status_code}",
        )

        def post(req_id: str, index: int, total: int) -> dict[str, Any]:
            resp = client.post(
                "/api/v1/conversation/generate-api-docs-stream",
                json=body,
                headers={
                    "X-Run-ID": RUN_ID,
                    "X-Request-ID": req_id,
                    "X-Part-Index": str(index),
                    "X-Part-Total": str(total),
                },
            )
            check(f"分片 {index}/{total} 返回 200 且是 SSE", resp.status_code == 200, f"status={resp.status_code}")
            check(
                f"分片 {index}/{total} 响应回显 X-Run-ID",
                resp.headers.get("X-Run-ID") == RUN_ID,
                str(resp.headers.get("X-Run-ID")),
            )
            return _summary_of(resp.text)

        first = post(REQ_1, 1, 2)
        one_call = INPUT_TOKENS + OUTPUT_TOKENS
        check(
            "第 1 片：run_id / 分片信息如实（1/2，未完成）",
            first.get("run_id") == RUN_ID
            and first.get("part_index") == 1
            and first.get("part_total") == 2
            and first.get("parts_seen") == 1
            and first.get("complete") is False,
            f"run_id={first.get('run_id')} part={first.get('part_index')}/{first.get('part_total')} "
            f"seen={first.get('parts_seen')} complete={first.get('complete')}",
        )
        check(
            "第 1 片：tokens/调用次数是这一片的",
            first.get("total_input_tokens") == INPUT_TOKENS
            and first.get("total_output_tokens") == OUTPUT_TOKENS
            and first.get("llm_call_count") == 1,
            f"in={first.get('total_input_tokens')} out={first.get('total_output_tokens')} "
            f"calls={first.get('llm_call_count')}",
        )

        second = post(REQ_2, 2, 2)
        check(
            "第 2 片：汇总变成**整份合计**（这是本次修复的核心）",
            second.get("total_input_tokens") == INPUT_TOKENS * 2
            and second.get("total_output_tokens") == OUTPUT_TOKENS * 2
            and second.get("llm_call_count") == 2,
            f"in={second.get('total_input_tokens')} out={second.get('total_output_tokens')} "
            f"calls={second.get('llm_call_count')}（单片是 {one_call}）",
        )
        check(
            "第 2 片：片数与请求 id 清单齐全、标记完成",
            second.get("parts_seen") == 2
            and second.get("complete") is True
            and second.get("request_ids") == [REQ_1, REQ_2],
            f"seen={second.get('parts_seen')} complete={second.get('complete')} ids={second.get('request_ids')}",
        )
        check(
            "耗时是**整份墙钟**（≥ 本片，而不是各片相加）",
            isinstance(second.get("total_duration_ms"), int)
            and isinstance(second.get("part_duration_ms"), int)
            and second["total_duration_ms"] >= second["part_duration_ms"],
            f"total={second.get('total_duration_ms')}ms part={second.get('part_duration_ms')}ms",
        )
        check(
            "步骤明细是各片拼接（两片的账都在）",
            isinstance(second.get("steps"), list) and len(second["steps"]) == 2,
            f"steps={len(second.get('steps') or [])}",
        )

        replay = post(REQ_1, 1, 2)
        check(
            "重放同一 request_id：**幂等**（tokens 不翻倍、片数不虚增）",
            replay.get("total_input_tokens") == INPUT_TOKENS * 2
            and replay.get("llm_call_count") == 2
            and replay.get("parts_seen") == 2,
            f"in={replay.get('total_input_tokens')} calls={replay.get('llm_call_count')} "
            f"seen={replay.get('parts_seen')}",
        )

    steps_logged = [item for item in capture.lines if item.get("event") == "llm_step"]
    run_steps = [item for item in steps_logged if item.get("run_id") == RUN_ID]
    check(
        "日志：每个 llm_step 都带 run_id 与 part（一条 grep 能查全整份）",
        len(run_steps) == 3 and all(item.get("part") for item in run_steps),
        f"带该 run_id 的 llm_step 共 {len(run_steps)} 行（3 次请求）",
    )
    summaries = [item for item in capture.lines if item.get("event") == "run_summary" and item.get("run_id") == RUN_ID]
    check(
        "日志：run_summary 带 run_id/parts_seen/complete",
        bool(summaries) and "parts_seen" in summaries[-1] and "complete" in summaries[-1],
        f"共 {len(summaries)} 行",
    )

    print()
    if failures:
        print(f"{len(failures)} 项未通过：" + "、".join(failures))
        return 1
    print("全部通过：跨分片合并、幂等重放、日志贯通")
    return 0


if __name__ == "__main__":
    asyncio.set_event_loop_policy(asyncio.DefaultEventLoopPolicy())
    raise SystemExit(main())
