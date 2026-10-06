"""确定性**结构校验**（quality_gate）：格式层的体检，不调模型、不碰 DB、不读文件。

## 为什么要有它

Review Agent 管**语义**（漏项、矛盾、编造），管不了**格式**（章节齐不齐、MVP 是不是表格、
接口文档分区完不完整）。后者用确定性规则做更稳、更快、可断言，而且**不烧 token**。

| | 谁做 | 看什么 |
| --- | --- | --- |
| Review Agent | LLM（仅 PRD） | 语义：有没有编造、有没有漏项、前后矛盾 |
| **quality_gate** | 本模块（纯代码） | 结构：章节、表格、分区、编号、路径前缀 |

## 规则按**本仓库的真实规范**写（不是照工单字面）

工单给的检查项是按**另一套文档结构**写的（api 的「核心接口 / 边缘接口」分区、
prompts 的「功能开发 / 接口实现 / 前端开发 / 代码审查」五段）。那套在本仓库**从未出现过**：

- 实测留档 `validation_out/split_api.txt`（已通过评审）：11 章 —— 文档信息 / 通用约定 /
  鉴权与授权 / 数据模型 / 错误码表 / **接口清单** / **接口详情** / 外部依赖 / 待确认 / 附 A/B，
  互补分区叫 `### 无接口功能`；
- 实测留档 `validation_out/gen_prompts.txt`：`=== FILE: 01-project-brief.md ===` 这样的
  **文件式**套件（五类 = project-brief / scaffold / data-layer / step-* / verify）。

照字面实现，每一份**已经过评审的好文档**都会报红，面板就失去了信号价值。所以这里保留
工单的**意图、id 风格与严重度分级**，把模式换成真实结构，并在每条上注明它对应哪条规范。

## 纯函数

`run_quality_gate()` 只吃 `(doc_type, content, context)`。于是自检脚本可以直接喂样本字符串
断言，runner 也能对任意正文跑（含失败路径的半成品）。
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, Mapping

#: 产物类型。与 `types/document.ts` 的 `DocumentType` / 版本层的 `doc_type` 一致。
DocType = Literal["prd", "api-docs", "prompts"]

#: 严重度权重：`high` 翻倍。**`passed` 只看 high**（工单 §4）。
SEVERITY_WEIGHT: dict[str, int] = {"high": 2, "medium": 1, "low": 1}

#: `[待确认]` 标记超过这个数就算 warning（工单 §5 的 `prd.pending.count`）。
MAX_PENDING_MARKS = 5

#: 北京时间：`checked_at` 给人看，用带偏移的本地时间比 UTC 好读（与仓库其他时间戳一致）。
_CST = timezone(timedelta(hours=8))

#: `=== FILE: xxx ===` 多文件分隔行（提示词套件的输出契约）。
_FILE_MARKER = re.compile(r"^===\s*FILE:\s*(.+?)\s*===\s*$", re.MULTILINE)

#: 接口条目标题：`### POST /api/v1/...`（允许多余的前缀如 `### 3.1 POST /...`）。
_ENDPOINT_HEADING = re.compile(
    r"^#{3,4}\s+.*?\b(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\b\s+`?/\S*",
    re.IGNORECASE | re.MULTILINE,
)

#: MVP 表的数据行（第一格是 `FR-xx` 编号）。
_MVP_ROW = re.compile(r"^\|\s*(FR-\d+)\s*\|", re.MULTILINE)


def _now() -> str:
    return datetime.now(_CST).isoformat(timespec="seconds")


# ---------------------------------------------------------------- 通用工具


def _sections(content: str, level: int) -> list[tuple[str, str]]:
    """按 `level` 级标题切段：`[(标题, 正文)]`，正文到下一个**同级**标题为止。"""
    pattern = re.compile(rf"^#{{{level}}}\s+(.*)$")
    out: list[tuple[str, str]] = []
    title: str | None = None
    buffer: list[str] = []
    for line in content.splitlines():
        match = pattern.match(line.strip())
        if match:
            if title is not None:
                out.append((title, "\n".join(buffer).strip()))
            title = match.group(1).strip()
            buffer = []
            continue
        if title is not None:
            buffer.append(line)
    if title is not None:
        out.append((title, "\n".join(buffer).strip()))
    return out


def _find_body(sections: list[tuple[str, str]], *names: str) -> str | None:
    """取第一个标题命中 `names` 任一的章节正文；没有返回 `None`。"""
    for title, body in sections:
        if any(name.lower() in title.lower() for name in names):
            return body
    return None


def _has_table(text: str) -> bool:
    """Markdown 表格：一行以 `|` 开头，紧跟着分隔行（`| --- | --- |`）。"""
    lines = [line.strip() for line in text.splitlines()]
    for index in range(len(lines) - 1):
        head, separator = lines[index], lines[index + 1]
        if head.startswith("|") and head.count("|") >= 2 and re.fullmatch(r"\|[\s:|-]+\|", separator):
            return True
    return False


def _check(
    check_id: str, label: str, passed: bool, severity: str, detail: str | None = None
) -> dict[str, Any]:
    return {"id": check_id, "label": label, "passed": passed, "severity": severity, "detail": detail}


def _chapter(
    sections: list[tuple[str, str]], check_id: str, label: str, names: tuple[str, ...], severity: str
) -> dict[str, Any]:
    """章节齐不齐。标题允许 minor 变体（工单 §5：正则匹配，不要求与模板字节级一致）。"""
    found = _find_body(sections, *names) is not None
    return _check(
        check_id,
        label,
        found,
        severity,
        None if found else f"没找到「{names[0]}」章节（也接受：{'、'.join(names[1:])}）",
    )


# ---------------------------------------------------------------- PRD


def _prd_checks(content: str, context: Mapping[str, Any]) -> list[dict[str, Any]]:
    """PRD 的结构检查（工单 §5 的 6 章 + MVP 表格 + 范围两侧 + 待确认计数）。

    6 章与 `skills/prd-generator/references/prd-template.md` 一致 —— 这一路工单写得就是
    本仓库的结构，所以**照做**，只是把标题匹配放宽成变体。
    """
    sections = _sections(content, 2)
    checks = [
        _chapter(sections, "prd.section.overview", "产品概述", ("产品概述", "项目概述", "产品简介", "概述"), "high"),
        _chapter(sections, "prd.section.features", "功能需求", ("功能需求", "功能清单", "核心功能"), "high"),
        _chapter(sections, "prd.section.pages", "页面设计", ("页面设计", "界面设计", "页面结构"), "high"),
        _chapter(sections, "prd.section.tech", "技术规格", ("技术规格", "技术方案", "技术架构"), "high"),
        _chapter(sections, "prd.section.nfr", "非功能需求", ("非功能需求", "非功能性需求", "性能与约束"), "high"),
        _chapter(sections, "prd.section.scope", "项目范围", ("项目范围", "范围边界", "项目边界"), "high"),
    ]

    # MVP 必须是表格：优先看「功能需求」章里的 `### MVP ...` 子节，退而看整章
    features = _find_body(sections, "功能需求", "功能清单", "核心功能") or ""
    mvp = _find_body(_sections(features, 3), "MVP") or features
    checks.append(
        _check(
            "prd.mvp.table",
            "MVP 功能为表格",
            _has_table(mvp),
            "high",
            None if _has_table(mvp) else "「功能需求 > MVP」下没有 Markdown 表格（`| 编号 | 功能 | …`）",
        )
    )

    # 项目范围的两侧都要有内容（互补分区：在范围内 / 不在范围内）
    scope = _find_body(sections, "项目范围", "范围边界", "项目边界") or ""
    scope_sections = _sections(scope, 3)
    for check_id, label, names in (
        ("prd.scope.in", "在范围内有内容", ("在范围内", "范围内")),
        ("prd.scope.out", "不在范围内有内容", ("不在范围内", "范围外")),
    ):
        body = _find_body(scope_sections, *names)
        ok = bool(body and body.strip())
        checks.append(
            _check(
                check_id,
                label,
                ok,
                "medium",
                None if ok else f"「{names[0]}」子节缺失或为空",
            )
        )

    pending = content.count("[待确认]")
    checks.append(
        _check(
            "prd.pending.count",
            f"待确认项 ≤ {MAX_PENDING_MARKS}",
            pending <= MAX_PENDING_MARKS,
            "medium",
            f"全文有 {pending} 处 [待确认]" + ("" if pending <= MAX_PENDING_MARKS else "（偏多：输入可能给得太少，或者模型在拿它顶替缺失信息）"),
        )
    )
    return checks


# ---------------------------------------------------------------- 接口文档


def _api_checks(content: str, context: Mapping[str, Any]) -> list[dict[str, Any]]:
    """接口文档的结构检查。

    对应 `docs/接口文档模板.md` + `core/prompts/gen_api.md` 的 11 章结构。
    ⚠️ 工单原写的 `api.section.core` / `api.section.edge` 指的是「核心接口 / 边缘接口」两个分区，
    本仓库的规范里**没有**这两个分区（实测留档一次都没出现）；等价物是
    「接口清单」（有哪些接口）与「无接口功能」表（哪些功能**不需要**接口，F8.3 的双向闭合）。
    所以 id 换成 `api.section.inventory` / `api.section.edge`，后者保留原名以示同一意图。
    """
    sections = _sections(content, 2)
    checks = [
        _chapter(sections, "api.section.inventory", "接口清单", ("接口清单", "接口列表"), "high"),
        _chapter(sections, "api.section.detail", "接口详情", ("接口详情", "接口定义"), "high"),
        _chapter(sections, "api.section.error_codes", "错误码表", ("错误码",), "high"),
    ]

    # 「无接口功能」：F8.3 要求"每条 P0 要么在接口清单、要么在这里"，是本仓库的互补分区。
    # 它是 `###` 级（在「接口清单」章里），所以按子节找。
    inventory = _find_body(sections, "接口清单", "接口列表") or content
    edge = _find_body(_sections(inventory, 3), "无接口功能", "无需接口")
    checks.append(
        _check(
            "api.section.edge",
            "含「无接口功能」互补分区",
            edge is not None,
            "medium",
            None if edge is not None else "接口清单里没有「无接口功能」表 —— F8.3 要求两条覆盖路径都在",
        )
    )

    # 统一错误体：`{code, message, details, trace_id}`（成功**不套信封**，所以只看错误体）
    has_envelope = bool(re.search(r'"code"\s*:', content)) and bool(re.search(r'"message"\s*:', content))
    checks.append(
        _check(
            "api.response.wrapper",
            "统一错误响应体",
            has_envelope,
            "medium",
            None if has_envelope else '没有找到统一错误体示例（应含 "code" 与 "message"）',
        )
    )

    has_prefix = "/api/v1" in content
    checks.append(
        _check(
            "api.path.prefix",
            "路径带 /api/v1 前缀",
            has_prefix,
            "high",
            None if has_prefix else "全文没有出现 `/api/v1`",
        )
    )

    count = len(_ENDPOINT_HEADING.findall(content))
    checks.append(
        _check(
            "api.endpoint.count",
            "至少一个接口条目",
            count >= 1,
            "high",
            f"识别到 {count} 个 `### <方法> <路径>` 标题" if count else "没有任何 `### GET/POST/... /path` 形式的接口标题",
        )
    )

    fr_ids = sorted(set(re.findall(r"FR-\d+", content)), key=lambda item: int(item[3:]))
    checks.append(
        _check(
            "api.fr_reference",
            "接口标注 PRD 来源（FR-xx）",
            bool(fr_ids),
            "medium",
            f"引用了 {len(fr_ids)} 个 FR 编号：{'、'.join(fr_ids[:6])}" if fr_ids else "全文没有 `FR-xx` —— 接口无法追溯到 PRD",
        )
    )
    return checks


# ---------------------------------------------------------------- 提示词套件


def _prd_mvp_feature_count(prd_content: str) -> int:
    """数 PRD「MVP 功能」表的数据行（第一格是 `FR-xx` 的行）。数不出来返回 0。"""
    sections = _sections(prd_content, 2)
    features = _find_body(sections, "功能需求", "功能清单", "核心功能") or prd_content
    mvp = _find_body(_sections(features, 3), "MVP") or features
    return len(_MVP_ROW.findall(mvp))


def _prompts_checks(content: str, context: Mapping[str, Any]) -> list[dict[str, Any]]:
    """提示词套件的结构检查。

    对应 `docs/提示词套件模板.md` + `core/prompts/gen_prompts.md` 的**五类文件**。
    ⚠️ 工单原写的是「项目总览 / 功能开发 / 接口实现 / 前端开发 / 代码审查」五**段**，
    本仓库是五个**文件**（`=== FILE: 01-project-brief.md ===` 那种）；实测留档里工单那五段
    一次都没出现。所以按文件名判，id 保留 `prompts.section.*` 的命名。
    映射：项目总览↔`01-project-brief`、功能开发↔`04-step-*`、代码审查↔`05-verify`；
    工单的「接口实现 / 前端开发」两类在本仓库的套件里**不存在**（我们按模块切步骤，
    不按前后端切），故不设对应检查。
    """
    files = [name.strip() for name in _FILE_MARKER.findall(content)]
    lowered = [name.lower() for name in files]

    def has(*needles: str) -> bool:
        return any(any(needle in name for needle in needles) for name in lowered)

    steps = [name for name in lowered if re.search(r"\bstep-\d+", name)]

    checks = [
        _check(
            "prompts.section.context",
            "总控提示词（project-brief）",
            has("project-brief", "project_brief"),
            "high",
            None if has("project-brief", "project_brief") else "没有 `=== FILE: 01-project-brief.md ===`",
        ),
        _check(
            "prompts.section.scaffold",
            "脚手架提示词（scaffold）",
            has("scaffold"),
            "high",
            None if has("scaffold") else "没有 `=== FILE: 02-scaffold.md ===`",
        ),
        _check(
            "prompts.section.data_layer",
            "数据层提示词（data-layer）",
            has("data-layer", "data_layer"),
            "high",
            None if has("data-layer", "data_layer") else "没有 `=== FILE: 03-data-layer.md ===`",
        ),
        _check(
            "prompts.section.features",
            "功能实现步骤（step-*）",
            bool(steps),
            "high",
            f"{len(steps)} 个步骤文件" if steps else "没有任何 `=== FILE: 04-step-xx-*.md ===`",
        ),
        _check(
            "prompts.section.verify",
            "验收自检（verify）",
            has("verify"),
            "high",
            None if has("verify") else "没有 `=== FILE: 05-verify.md ===`",
        ),
    ]

    # 与 PRD 的功能数对齐（需要 context.prd_content；没有就 skip，按工单 §5 记为 passed）
    prd_content = str(context.get("prd_content") or "")
    mvp_rows = _prd_mvp_feature_count(prd_content)
    if not prd_content.strip() or mvp_rows == 0:
        checks.append(
            _check(
                "prompts.feature.count",
                "步骤数与 PRD 功能数对齐",
                True,
                "medium",
                "skipped：本次没有可用的 PRD 正文",
            )
        )
    else:
        # ⚠️ 判据是"**不多于**"而不是工单写的"差值 ≤1"：我们的步骤按**模块**分组
        # （实测一次真实生成里 8 条 MVP 功能 → 5 个步骤文件，每个步骤覆盖一组功能），
        # 1:1 的差值判据会把正常产物判红。而"步骤比功能还多"是真正的坏味道
        # （拆得过碎或凭空多出步骤），这条能抓住。
        ok = len(steps) <= mvp_rows
        checks.append(
            _check(
                "prompts.feature.count",
                "步骤数不多于 PRD 功能数",
                ok,
                "medium",
                f"{len(steps)} 个步骤 / PRD 里 {mvp_rows} 条 MVP 功能"
                + ("" if ok else "（步骤比功能还多：拆得过碎，或有凭空多出来的步骤）"),
            )
        )
    return checks


_CHECKS_BY_TYPE = {
    "prd": _prd_checks,
    "api-docs": _api_checks,
    "prompts": _prompts_checks,
}


# ---------------------------------------------------------------- 入口


def run_quality_gate(
    doc_type: str, content: str, context: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """跑一次结构校验，返回可直接写进 `metadata.quality_gate` 的字典。

    Args:
        doc_type: `prd` / `api-docs` / `prompts`（与版本层的 `doc_type` 一致）。
        content: **最终正文**（PRD 的 rewrite 之后那一稿；优化任务是拼好的整篇）。
        context: 可选上下文。目前只用到 `prd_content`（提示词套件要与 PRD 的功能数对齐）。

    Returns:
        `{"passed", "score", "checked_at", "doc_type", "checks"}`。
        - `passed`：**所有 high 检查都通过**（工单 §4）
        - `score`：加权通过率（high 权重 2，medium/low 权重 1），四舍五入到整数

    内容为空 → 单条 `empty_content`（high，失败）。未知 `doc_type` → 单条
    `gate.unknown_doc_type`（high，失败）—— 静默返回"全过"等于把配置错误伪装成合格。
    """
    resolved_context: Mapping[str, Any] = context or {}
    text = content or ""

    if not text.strip():
        checks = [_check("empty_content", "正文非空", False, "high", "正文为空，没有可校验的内容")]
    elif doc_type not in _CHECKS_BY_TYPE:
        checks = [
            _check(
                "gate.unknown_doc_type",
                "产物类型已知",
                False,
                "high",
                f"未知 doc_type={doc_type!r}；支持 {sorted(_CHECKS_BY_TYPE)}",
            )
        ]
    else:
        checks = _CHECKS_BY_TYPE[doc_type](text, resolved_context)

    return {
        "passed": all(item["passed"] for item in checks if item["severity"] == "high"),
        "score": _score(checks),
        "checked_at": _now(),
        "doc_type": doc_type,
        "checks": checks,
    }


def _score(checks: list[Mapping[str, Any]]) -> int:
    """加权通过率（0–100）。空 checks 视为 100（没有可失分的东西）。"""
    total = sum(SEVERITY_WEIGHT.get(str(item.get("severity")), 1) for item in checks)
    if total <= 0:
        return 100
    earned = sum(
        SEVERITY_WEIGHT.get(str(item.get("severity")), 1) for item in checks if item.get("passed")
    )
    return round(100 * earned / total)


def gate_error(exc: BaseException) -> dict[str, Any]:
    """gate 自己抛错时的兜底结果（工单 §9：**不能**让 Job 因此失败）。

    `passed=False` 而不是 `True`：一个跑挂了的检查器不能替产物背书。
    """
    return {
        "passed": False,
        "score": 0,
        "checked_at": _now(),
        "doc_type": "unknown",
        "checks": [
            _check("gate.error", "质量校验执行失败", False, "high", f"{type(exc).__name__}: {exc}")
        ],
    }
