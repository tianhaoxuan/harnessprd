"""审查结论的**归一化**（05 篇）。

## 为什么要有它

三份产物的审查输出**形状不一样**：

| 谁 | 原始输出 |
| --- | --- |
| PRD（`prd_review.md`，既有） | `{ok, issues: [{section, problem, suggestion}]}` |
| 接口文档（`api_docs_review.md`，05 篇） | `{passed, summary, issues: [{severity, section, problem, suggestion}]}` |
| 提示词套件（`prompts_review.md`，05 篇） | 同上 |

而**存进 `metadata.review` 与 SSE `review` 事件的必须是同一个形状** —— 否则前端的
`ReviewResultPanel` 要为每份产物写一套渲染，`manifest.json` 也要判"这份 review 有没有 summary"。

## 两条固定规则

1. **`passed` 由 `issues` 推出来**（`passed = 没有 issues`），不信模型自己那个布尔字段。
   理由：模型完全可能给出 `passed: true` 却列了三条问题（或反过来）。既有的 PRD 路径
   （`job_runner._review_from_stage`）就是 `passed = not issues` —— 这里保持一致，
   而不是让"通过与否"取决于模型的心情。
2. **`severity` 只保留三档之一**，缺失/非法一律按 `medium`。界面的配色与排序靠它，
   一个拼错的 `"High"` 不该让那一条掉进"其它"。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

__all__ = ["REVIEW_SEVERITIES", "DEFAULT_SEVERITY", "normalize_review_verdict"]

#: 允许的严重度（前端 `QualityReportPanel` 用的是同一套三档）。
REVIEW_SEVERITIES = ("high", "medium", "low")

DEFAULT_SEVERITY = "medium"

#: `summary` 的截断长度：它会进 manifest 与界面一行提示，长了自己也会被界面截掉。
MAX_SUMMARY_CHARS = 200


def _clean_issue(raw: Mapping[str, Any]) -> dict[str, Any] | None:
    """把一条意见洗成固定字段。没有任何可读内容（既无 problem 也无 section）→ 丢掉。"""
    problem = raw.get("problem") or raw.get("detail") or raw.get("issue")
    section = raw.get("section")
    problem_text = str(problem).strip() if isinstance(problem, (str, int, float)) else ""
    section_text = str(section).strip() if isinstance(section, (str, int, float)) else ""
    if not problem_text and not section_text:
        return None
    severity = raw.get("severity")
    severity_text = str(severity).strip().lower() if isinstance(severity, str) else ""
    suggestion = raw.get("suggestion")
    return {
        "severity": severity_text if severity_text in REVIEW_SEVERITIES else DEFAULT_SEVERITY,
        "section": section_text or None,
        "problem": problem_text or section_text,
        "suggestion": str(suggestion).strip() if isinstance(suggestion, str) and suggestion.strip() else None,
    }


def normalize_review_verdict(verdict: Mapping[str, Any]) -> dict[str, Any]:
    """归一化一份审查结论（不含 `review_model` / `review_skipped`，那两个由调用方补）。

    Args:
        verdict: 模型解析出来的 JSON 对象（`_extract_json_object` 的产物）。

    Returns:
        `{"passed": bool, "issues": [...], "summary": str | None}`
    """
    raw_issues = verdict.get("issues")
    issues: list[dict[str, Any]] = []
    if isinstance(raw_issues, Sequence) and not isinstance(raw_issues, (str, bytes)):
        for raw in raw_issues:
            if isinstance(raw, Mapping):
                cleaned = _clean_issue(raw)
                if cleaned is not None:
                    issues.append(cleaned)

    summary = verdict.get("summary")
    summary_text = str(summary).strip() if isinstance(summary, str) else ""
    if len(summary_text) > MAX_SUMMARY_CHARS:
        summary_text = summary_text[: MAX_SUMMARY_CHARS - 1] + "…"

    return {
        # ⚠️ 只看 issues，不看模型自报的 passed/ok（见模块 docstring 第 1 条）
        "passed": not issues,
        "issues": issues,
        "summary": summary_text or None,
    }
