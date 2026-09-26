"""HTTP 冒烟：对着**已启动**的服务打真实请求。

与 smoke_check.py 的分工：
- smoke_check.py  不启动服务、不联网，验证配置 / 提示词 / 模型构造 / 路由注册
- http_check.py   打真实 HTTP，验证状态码、响应结构、路由是否与设计一致

用标准库 urllib，不引入额外依赖。

前置：先启动服务
    .venv\\Scripts\\python -m uvicorn main:app --port 8000

运行：
    .venv\\Scripts\\python scripts\\http_check.py
    .venv\\Scripts\\python scripts\\http_check.py http://127.0.0.1:8010

注：本脚本**不做真实 LLM 调用**。旧骨架的 /prd/draft 是唯一入口，已删除；
提示词层的真实 LLM 验证由 scripts/validate_prompts.py 承担。
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from typing import Any

DEFAULT_BASE = "http://127.0.0.1:8000"
TIMEOUT = 30.0

EXPECTED_PATHS = {
    "/",
    "/health",
    "/api/v1/health",
    "/api/v1/conversation/questions",
    "/api/v1/conversation/start-stream",
    "/api/v1/conversation/continue-stream",
    "/api/v1/conversation/generate-prd-stream",
    "/api/v1/conversation/generate-api-docs-stream",
    "/api/v1/conversation/generate-prompts-stream",
    "/api/v1/conversation/optimize-document-stream",
    "/api/v1/sessions",
    "/api/v1/sessions/{session_id}",
    "/api/v1/sessions/{session_id}/form",
    "/api/v1/sessions/{session_id}/events",
    "/api/v1/sessions/{session_id}/documents/{kind}",
    "/api/v1/sessions/{session_id}/messages",
    "/api/v1/config",
}

# 已删除的旧形状：留着就是两套并存的接口面
REMOVED_ROUTES = (
    ("POST", "/api/v1/prd/draft"),              # 旧骨架，HANDOFF.md §7
    ("POST", "/api/v1/prd/clarify"),
    ("POST", "/api/v1/conversation/messages"),  # 以"对话消息"为中心 → 改为以"会话"为中心
    ("GET", "/api/v1/conversation/sessions"),
    ("GET", "/api/v1/form/questions"),          # 已并入 /conversation/questions
    ("GET", "/api/v1/conversation/stream"),     # 被 start-stream / continue-stream 取代
)
REMOVED_PATHS = {path for _, path in REMOVED_ROUTES}

# (方法, 路径, 请求体)。probe-id 只是占位串，占位实现不会真的去查库。
# 带 body 的两条必须给合法 body —— FastAPI **先校验入参再进 handler**，
# 不给 body 会得到 422 而不是 501，那是另一条断言该管的事。
# stream 的 session_id 是必填查询参数，所以写进路径的 query 里。
PLACEHOLDERS = (
    ("POST", "/api/v1/sessions", None),
    ("GET", "/api/v1/sessions", None),
    ("GET", "/api/v1/sessions/probe-id", None),
    ("PUT", "/api/v1/sessions/probe-id/form", {}),
    ("POST", "/api/v1/sessions/probe-id/events", {"event": "probe"}),
    ("GET", "/api/v1/sessions/probe-id/documents/prd", None),
    ("GET", "/api/v1/sessions/probe-id/messages", None),
    ("GET", "/api/v1/config", None),
)

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    print(f"[{'PASS' if condition else 'FAIL'}] {name}" + (f" —— {detail}" if detail else ""))
    if not condition:
        FAILURES.append(name)


def request(
    base: str,
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
) -> tuple[int | None, str]:
    """返回 (status_code, body_text)；连不上时 status 为 None。"""
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"

    req = urllib.request.Request(f"{base}{path}", data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")
    except urllib.error.URLError as exc:
        return None, str(exc.reason)


def main() -> int:
    base = (sys.argv[1] if len(sys.argv) > 1 else DEFAULT_BASE).rstrip("/")
    print(f"目标：{base}\n")

    # 1) 服务自述
    status, body = request(base, "GET", "/")
    check("GET / 返回 200", status == 200, f"status={status}")
    if status == 200:
        root = json.loads(body)
        check("自述指向根路径 /health", root.get("health") == "/health", str(root.get("health")))

    # 2) 健康检查
    status, body = request(base, "GET", "/api/v1/health")
    check("GET /api/v1/health 返回 200", status == 200, f"status={status}")
    health: dict[str, Any] = {}
    if status == 200:
        health = json.loads(body)
        print(f"     {json.dumps(health, ensure_ascii=False)}")
        check("健康检查含提示词资源", bool(health.get("prompts")))
        check("健康检查不泄露密钥值", "api_key" not in body.lower() or "configured" in body.lower())
        check("健康检查回显 llm_configured", isinstance(health.get("llm_configured"), bool))

    # 3) 路由注册（以 OpenAPI 为准）
    status, body = request(base, "GET", "/openapi.json")
    if status == 200:
        paths = set(json.loads(body).get("paths", {}))
        missing = EXPECTED_PATHS - paths
        check("OpenAPI 路由齐全", not missing, f"缺失 {missing}" if missing else f"共 {len(paths)} 条")
        stale = REMOVED_PATHS & paths
        check("旧 /prd 路由已移除", not stale, f"仍存在 {sorted(stale)}" if stale else "")
    else:
        check("OpenAPI 可访问", False, f"status={status}")

    # 4) 根路径健康检查：探针用的稳定别名，应与版本化路径同源
    status, body = request(base, "GET", "/health")
    check("GET /health 返回 200", status == 200, f"status={status}")
    if status == 200 and health:
        root_health = json.loads(body)
        check(
            "/health 与 /api/v1/health 内容一致",
            root_health == health,
            f"provider {root_health.get('llm_provider')} / {health.get('llm_provider')}",
        )

    # 5) 已实现的接口：表单题目下发
    status, body = request(base, "GET", "/api/v1/conversation/questions")
    check("GET /conversation/questions 返回 200", status == 200, f"status={status}")
    if status == 200:
        form = json.loads(body)
        check("返回表单版本", form.get("version") == "1.2", str(form.get("version")))
        # 注意别把题目列表赋给 `base` —— 那是上面存服务地址的变量，遮蔽了会让后续请求打到畸形 URL
        base_questions = form.get("base_questions", [])
        advanced_questions = form.get("advanced_questions", [])
        check(
            "返回 20 题",
            len(base_questions) + len(advanced_questions) == 20,
            f"{len(base_questions)}+{len(advanced_questions)}",
        )
        check(
            "题目字段齐全（前端渲染所需）",
            all(
                {"id", "label", "question", "type", "options", "required", "maxLength"} <= set(q)
                for q in base_questions
            ),
        )

    # 6) 占位接口必须是 501：返回 200 假数据会被误认为已实现
    for method, path, payload in PLACEHOLDERS:
        status, detail = request(base, method, path, payload)
        # status=None 表示压根没连上 —— 必须把原因打出来，否则只看到 None 无从下手
        note = f"status={status}" if status is not None else f"连不上：{detail[:120]}"
        check(f"{method} {path} 占位返回 501", status == 501, note)

    # 7) 入参校验真的接上了（不是所有请求都无脑 501）
    status, _ = request(base, "GET", "/api/v1/sessions/probe-id/documents/not-a-kind")
    check("文档 kind 取非法值被拦成 422", status == 422, f"status={status}")
    status, _ = request(base, "POST", "/api/v1/sessions/probe-id/events", {})
    check("事件缺 event 字段被拦成 422", status == 422, f"status={status}")
    status, _ = request(base, "POST", "/api/v1/conversation/start-stream", {})
    check("start-stream 缺 form 被拦成 422", status == 422, f"status={status}")

    # 分片计划：**不调模型**，所以这里可以查合法的 kind（上面那些只能发非法 body）
    status, body = request(base, "GET", "/api/v1/conversation/document-plan?kind=api")
    check("GET /conversation/document-plan 返回 200", status == 200, f"status={status}")
    if status == 200:
        plan = json.loads(body) if body else {}
        parts = plan.get("parts") or []
        check(
            "接口文档的计划是多片的（单次生成必被截断）",
            plan.get("multi_part") is True and len(parts) >= 2,
            f"multi_part={plan.get('multi_part')}｜{len(parts)} 片",
        )
        check(
            "计划里每片都带了 scope/spec/outline（否则占位符会留字面量）",
            all(p.get("scope") and p.get("spec") and p.get("outline") for p in parts),
            f"{len(parts)} 片",
        )
    status, _ = request(base, "GET", "/api/v1/conversation/document-plan?kind=not-a-kind")
    check("document-plan 的 kind 取非法值被拦成 422", status == 422, f"status={status}")
    status, _ = request(
        base, "POST", "/api/v1/conversation/continue-stream", {"form": {}, "user_input": ""}
    )
    check("continue-stream 空 user_input 被拦成 422", status == 422, f"status={status}")

    # 4 个生成 / 修订接口的入参校验。
    # ⚠️ **只发非法 body** —— 合法 body 会真的调用 LLM（本脚本明确不做真实 LLM 调用），
    # 所以这里全部断言"被拦成 422"，绝不构造能过校验的请求。
    status, _ = request(
        base, "POST", "/api/v1/conversation/generate-api-docs-stream", {"prd_content": "   "}
    )
    check("generate-api-docs 空白 prd_content 被拦成 422", status == 422, f"status={status}")
    status, _ = request(
        base, "POST", "/api/v1/conversation/generate-prd-stream", {"scope": {"outline": "只有一半"}}
    )
    check("generate-prd 残缺 scope 被拦成 422", status == 422, f"status={status}")
    status, _ = request(
        base, "POST", "/api/v1/conversation/generate-prompts-stream", {"prd_content": ""}
    )
    check("generate-prompts 空 prd_content 被拦成 422", status == 422, f"status={status}")
    status, _ = request(
        base,
        "POST",
        "/api/v1/conversation/optimize-document-stream",
        {"kind": "api", "section": "### x", "current_content": "y"},
    )
    check("optimize 修订 api 类产物缺 prd_content 被拦成 422", status == 422, f"status={status}")
    status, _ = request(
        base,
        "POST",
        "/api/v1/conversation/optimize-document-stream",
        {"kind": "prd", "section": "   ", "current_content": "y"},
    )
    check("optimize 空白 section 被拦成 422", status == 422, f"status={status}")

    # 8) 旧形状必须 404（而不是 500）——确保是真删干净，不是删了实现留了壳
    for method, path in REMOVED_ROUTES:
        payload = {"requirement": "x"} if method == "POST" else None
        status, _ = request(base, method, path, payload)
        check(f"{method} {path} 已 404", status == 404, f"status={status}")

    print()
    if FAILURES:
        print(f"{len(FAILURES)} 项未通过：" + "、".join(FAILURES))
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
