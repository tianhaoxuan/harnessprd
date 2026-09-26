"""端到端验证：用真实 LLM 跑一遍「表单 → 对话 → 生成」链路。

**这个脚本验证的是提示词的设计假设，不是代码正确性。**
它要回答的问题（见 `docs/` 各模板的设计意图）：

1. 澄清阶段的 JSON 输出能不能解析？模型会不会加 ``` 包裹或前言？
2. 每轮是否 ≤ 3 个问题？每个问题是否都带 `suggested_answer`？
3. S2 是从 12 类边界里**挑**，还是把 12 类全列一遍？
4. S4 在字段留空时，是否给出**具体数值**而不是反问？
5. gen_prd 是否守住章节范围、字数上限？有没有前言、有没有整篇套代码块？
6. gen_api 是否给每个接口标 `PRD 来源`？是否输出「无接口功能」表？
7. gen_prompts 的 `=== FILE: ===` 分隔契约能否被稳定输出并解析？

⚠️ **本脚本的 `gen_prd` 步骤验的是 `gen_prd.md`（15 章 + 2 附录），那是回退路径** ——
产品默认走 `skills/prd-generator/` 的 6 章技能包，那条路由 `scripts/skill_prd_check.py`
验证（它会真跑一次并把产物留档）。`gen_api` 与 `gen_prompts` 两步传进去的 `prd_content`
是这里的 `gen_prd.txt`，所以它们验的是"下游提示词能不能按章节名从一份 PRD 里取到数"，
技能包路径下取数依据是 6 章的章名与 `FR-xx`/`AC-xx`，两份提示词都已按后者改写。

运行（在 backend/ 目录下，Key 走环境变量 `DEEPSEEK_API_KEY`，不落盘）：

    .venv\\Scripts\\python scripts\\validate_prompts.py
    .venv\\Scripts\\python scripts\\validate_prompts.py --only s0,gen_prd
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from core.config import get_settings  # noqa: E402
from core.prompts import (  # noqa: E402
    available_prompts,
    build_system_prompt,
    declared_placeholders,
)
from services.conversation_service import format_form_data  # noqa: E402
from services.llm import build_chat_model  # noqa: E402

CORE_DIR = BACKEND_DIR / "core"
OUT_DIR = BACKEND_DIR / "validation_out"

FAILURES: list[str] = []
OBSERVATIONS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" —— {detail}" if detail else ""), flush=True)
    if not ok:
        FAILURES.append(name)


def observe(text: str) -> None:
    print(f"  [观察] {text}", flush=True)
    OBSERVATIONS.append(text)


# ---------------------------------------------------------------- 输入构造


def load_questions() -> tuple[list[dict[str, Any]], str]:
    cfg = json.loads((CORE_DIR / "questions_config.json").read_text(encoding="utf-8"))
    return [*cfg["base_questions"], *cfg["advanced_questions"]], cfg["version"]


def build_form_summary(answers: dict[str, str], questions: list[dict[str, Any]]) -> str:
    """**已改为转调 `services.conversation_service.format_form_data`。**

    保留这个薄壳只是为了少改调用点；**不要再在这里实现格式化逻辑** ——
    提示词输入有两处实现就必然漂移，那样"验证通过的提示词"与"运行时的提示词"会是两份
    （`HANDOFF.md` §4 坑 #10）。`questions` 参数已不需要，留着是为了兼容既有调用。
    """
    del questions  # 配置由 core.questions 统一读，不再由调用方传
    return format_form_data(answers)


def report_missing_inputs(prompt_name: str, values: dict[str, str]) -> None:
    """提示词声明了、但本次没注入的占位符。

    存在的意义：``str.replace`` 对缺失键**静默跳过**，缺注入不会报错，只会让
    ``{generate_scope}`` 之类的字面量留在 system prompt 里 —— 模型照样能输出像样的
    结果，于是「验证通过」掩盖了「输入根本没送到」。

    只作观察、不判失败：``clarify_common.md`` 的清单表把各阶段的占位符列在一起，
    它声明的集合是各阶段的并集，单阶段跑起来必然有几项是"本阶段不需要"的。
    真正要盯的是 **gen 族** —— ``gen_common.md`` 把 ``{doc_outline}``、
    ``{generate_scope}``、``{scope_spec}`` 标为「全部」适用，而目前没有任何
    调用方注入它们。
    """
    family = prompt_name.split("_", 1)[0]
    available = set(available_prompts())
    declared: set[str] = set()
    for candidate in (f"{family}_common", prompt_name):
        if candidate in available:
            declared |= declared_placeholders(candidate)
    missing = sorted(declared - set(values))
    if missing:
        observe(f"{prompt_name} 未注入已声明的占位符：{missing}")
    else:
        observe(f"{prompt_name} 声明的占位符已全部注入")


# ---------------------------------------------------------------- 调用与解析


async def call(model: Any, system: str, human: str) -> tuple[str, dict[str, Any]]:
    from langchain_core.messages import HumanMessage, SystemMessage

    started = time.monotonic()
    resp = await model.ainvoke([SystemMessage(content=system), HumanMessage(content=human)])
    elapsed = time.monotonic() - started

    content = resp.content
    if isinstance(content, list):
        content = "".join(
            b if isinstance(b, str) else str(b.get("text", ""))
            for b in content
            if isinstance(b, (str, dict))
        )
    usage = getattr(resp, "usage_metadata", None) or {}
    meta = {
        "seconds": round(elapsed, 1),
        "chars": len(content),
        "in_tokens": usage.get("input_tokens"),
        "out_tokens": usage.get("output_tokens"),
    }
    return content.strip(), meta


def parse_json_reply(raw: str) -> tuple[dict[str, Any] | None, list[str]]:
    """解析澄清阶段的 JSON 输出，同时记录模型违反了哪些格式约定。"""
    notes: list[str] = []
    text = raw

    if text.startswith("```"):
        notes.append("输出被 ``` 代码块包裹（提示词明确禁止）")
        text = re.sub(r"^```[a-zA-Z]*\n", "", text)
        text = re.sub(r"\n```$", "", text)

    if not text.lstrip().startswith("{"):
        notes.append("JSON 前面有额外文字（提示词明确禁止）")

    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < 0:
        return None, notes + ["找不到 JSON 对象"]
    try:
        return json.loads(text[start : end + 1]), notes
    except json.JSONDecodeError as exc:
        return None, notes + [f"JSON 解析失败：{exc}"]


BOUNDARY_CATEGORIES = [
    "空数据", "失败", "超量", "权限", "重复", "撤销",
    "时间与时区", "并发", "一致性", "授权撤销", "精度", "版本",
]


def check_clarify(stage: str, data: dict[str, Any] | None, notes: list[str], meta: dict[str, Any]) -> None:
    print(f"  耗时 {meta['seconds']}s / 输出 {meta['chars']} 字 / "
          f"token {meta['in_tokens']}→{meta['out_tokens']}", flush=True)
    for n in notes:
        observe(n)
    if data is None:
        check(f"{stage} JSON 可解析", False)
        return

    check(f"{stage} JSON 可解析", True)
    missing = [k for k in ("message", "questions", "conflicts", "stage_status", "open_questions") if k not in data]
    check(f"{stage} 响应字段齐全", not missing, f"缺 {missing}" if missing else "")

    qs = data.get("questions") or []
    check(f"{stage} 问题数 1–3", 1 <= len(qs) <= 3, f"实际 {len(qs)} 个")
    no_suggestion = [q.get("id", "?") for q in qs if not (q.get("suggested_answer") or "").strip()]
    check(f"{stage} 每问都带 suggested_answer", not no_suggestion, f"缺建议的是 {no_suggestion}" if no_suggestion else "")
    check(f"{stage} stage_status 取值合法", data.get("stage_status") in ("asking", "done"),
          f"实际 {data.get('stage_status')!r}")

    # S2 专项：是从 12 类里挑，还是全列？
    if stage == "S2":
        blob = json.dumps(data, ensure_ascii=False)
        hit = [c for c in BOUNDARY_CATEGORIES if c in blob]
        observe(f"S2 触及的边界类别 {len(hit)}/12：{hit}")
        check("S2 没有把 12 类边界全列一遍", len(hit) < 12, f"触及 {len(hit)} 类")

    # S4 专项：是否给出具体数值
    if stage == "S4":
        blob = json.dumps(data, ensure_ascii=False)
        has_number = bool(re.search(r"\d+\s*(个|天|分钟|小时|%|次|并发)", blob))
        check("S4 给出了具体数值而非反问", has_number)


# ---------------------------------------------------------------- 各步骤


async def run_clarify(model: Any, stage: str, base: dict[str, str]) -> tuple[str, dict[str, Any] | None]:
    report_missing_inputs(f"clarify_{stage}", base)
    # 组装与渲染都走 core/prompts —— 与运行时同一份代码，不在脚本里另存一份实现
    system = build_system_prompt(f"clarify_{stage}", base)
    human = "请按上述要求开始。（这是本轮用户输入）"
    raw, meta = await call(model, system, human)
    (OUT_DIR / f"clarify_{stage}.txt").write_text(raw, encoding="utf-8")
    data, notes = parse_json_reply(raw)
    check_clarify(stage, data, notes, meta)
    return raw, data


async def run_gen(model: Any, which: str, base: dict[str, str], scope: str, spec: str) -> str:
    report_missing_inputs(f"gen_{which}", base)
    system = build_system_prompt(f"gen_{which}", base)
    human = f"本次只生成：{scope}\n\n详细规格：\n{spec}"
    raw, meta = await call(model, system, human)
    (OUT_DIR / f"gen_{which}.txt").write_text(raw, encoding="utf-8")
    print(f"  耗时 {meta['seconds']}s / 输出 {meta['chars']} 字 / "
          f"token {meta['in_tokens']}→{meta['out_tokens']}", flush=True)

    check_gen_format(which, raw)
    return raw


def check_gen_format(which: str, raw: str, limits: dict[str, int] | None = None) -> None:
    """格式感知的前言检查：单文档以 `#` 开头，多文件产物以分隔行开头。"""
    body = re.sub(r"^```[a-zA-Z]*\n|\n```$", "", raw).lstrip()
    check(f"gen_{which} 没有把整篇套进代码块", not raw.startswith("```"))

    if which == "prompts":
        # 提示词套件是多文件产物：合法开头就是分隔行，不是 Markdown 标题
        ok = body.startswith("=== FILE:")
        check(f"gen_{which} 以分隔行开头（多文件产物）", ok, f"开头是：{body[:40]!r}")
        files = re.findall(r"^=== FILE: (.+?) ===$", raw, re.M)
        check(f"gen_{which} 分隔行格式正确", bool(files) and all(f.endswith(".md") for f in files),
              f"解析到 {files}")
        parts = re.split(r"^=== FILE: (.+?) ===$", raw, flags=re.M)
        for i in range(1, len(parts), 2):
            n = len(re.sub(r"\s", "", parts[i + 1]))
            print(f"        {parts[i]}: {n} 字", flush=True)
    else:
        check(f"gen_{which} 没有前言（直接以标题开头）", body.startswith("#"), f"开头是：{body[:40]!r}")

    if limits:
        for title, cap in limits.items():
            m = re.search(rf"^{re.escape(title)}.*?$(.*?)(?=^## |\Z)", raw, re.M | re.S)
            if m:
                n = len(re.sub(r"\s", "", m.group(1)))
                check(f"{title} 未超字数上限 {cap}", n <= cap, f"实际 {n} 字（超 {n - cap}）")


def _table_frs(raw: str, heading: str) -> set[str]:
    """取某个小节下**表格行**里的 FR 编号 —— 只看表格，不看正文。

    踩过的坑：模型会在表格后加一句「覆盖校验：FR-01 ~ FR-07 全部出现在接口清单中」，
    若把整节正文都拿去匹配，这句总结会被误判成「该 FR 也出现在无接口表里」。
    """
    sec = raw.split(heading)[-1]
    frs: set[str] = set()
    for line in sec.splitlines():
        s = line.strip()
        if s.startswith("|") and "---" not in s:
            frs |= set(re.findall(r"FR-\d{2}", s))
    return frs


def check_api_mutex(raw: str) -> None:
    listed = _table_frs(raw, "### 接口清单")
    noapi = _table_frs(raw, "### 无接口功能")
    both = sorted(listed & noapi)
    check("一个 FR 不同时出现在两张表里（互斥）", not both, f"同时出现：{both}")
    observe(f"接口清单 {len(listed)} 条 FR / 无接口表 {len(noapi)} 条 FR")


PERMISSION_WORDS = ["登录", "鉴权", "角色", "权限", "可见", "JWT", "认证"]
COMPLIANCE_WORDS = ["加密", "审计", "保留", "出境", "合规", "隐私", "脱敏", "日志留存"]


def check_no_substitution(brief: str) -> None:
    """检查「安全与合规约定」小节是否被权限内容顶替。

    本次夹具里 compliance 字段留空 → 正确行为是**整节省略**，
    或标注「本次无该维度输入」（取决于该节是否属于必填）。
    """
    m = re.search(r"^#{1,3} *安全与合规.*?$(.*?)(?=^#{1,3} |\Z)", brief, re.M | re.S)
    if not m:
        check("compliance 为空时「安全与合规」整节省略（未发生顶替）", True)
        observe("「安全与合规」小节已按规则整节省略 —— 修复生效")
        return

    body = m.group(1).strip()
    if not body or "无该维度输入" in body or "待补充" in body:
        check("compliance 为空时标注「无输入」而非顶替", True)
        observe(f"「安全与合规」正文：{body[:40]!r} —— 修复生效")
        return

    perm = [w for w in PERMISSION_WORDS if w in body]
    comp = [w for w in COMPLIANCE_WORDS if w in body]
    substituted = len(perm) >= 3 and not comp
    check("compliance 为空时未用权限内容顶替「安全与合规」", not substituted,
          f"该节含权限词 {perm}，合规词 {comp}")
    if not substituted:
        observe(f"「安全与合规」含合规词 {comp}，权限词 {perm}")


async def recheck() -> int:
    """不调用 LLM，直接对 validation_out/ 里的历史输出重跑全部结构化检查。"""
    if not OUT_DIR.exists():
        print("没有历史输出可复查。先跑一次完整验证。", file=sys.stderr)
        return 2
    print("=" * 78)
    print("复查模式：只分析已保存的输出，不调用 LLM")
    print("=" * 78)

    for name in ("S0", "S1", "S2", "S4"):
        p = OUT_DIR / f"clarify_{name}.txt"
        if not p.exists():
            continue
        print(f"\n【clarify_{name}】")
        raw = p.read_text(encoding="utf-8")
        data, notes = parse_json_reply(raw)
        check_clarify(name, data, notes, {"seconds": "-", "chars": len(raw),
                                          "in_tokens": "-", "out_tokens": "-"})

    if (OUT_DIR / "gen_prd.txt").exists():
        print("\n【gen_prd】")
        raw = (OUT_DIR / "gen_prd.txt").read_text(encoding="utf-8")
        check_gen_format("prd", raw, {
            "## 第 5 章": 300, "## 第 6 章": 600, "## 第 7 章": 1500,
        })

    if (OUT_DIR / "gen_api.txt").exists():
        print("\n【gen_api】")
        raw = (OUT_DIR / "gen_api.txt").read_text(encoding="utf-8")
        check_gen_format("api", raw)
        check_api_mutex(raw)

    if (OUT_DIR / "gen_prompts.txt").exists():
        print("\n【gen_prompts】")
        raw = (OUT_DIR / "gen_prompts.txt").read_text(encoding="utf-8")
        check_gen_format("prompts", raw)
        brief = raw.split("=== FILE: 01-project-brief.md ===")[-1]
        n = len(re.sub(r"\s", "", brief))
        check("01-project-brief 未超字数上限 1000", n <= 1000, f"实际 {n} 字")
        check_no_substitution(brief)
        # 术语表：0 次 = 被漏掉，>1 次 = 重复。方向两边都可能错，故只作观察不判失败。
        terms = raw.count("| 术语 |")
        observe(f"术语表出现 {terms} 次（期望恰好 1 次：0 次=漏掉，>1 次=重复）")

    print()
    if FAILURES:
        print(f"未通过 {len(FAILURES)} 项：")
        for f in FAILURES:
            print(f"  ✗ {f}")
        return 1
    print("全部通过")
    return 0


async def main() -> int:
    parser = argparse.ArgumentParser(description="端到端验证提示词设计假设")
    parser.add_argument("--only", default="", help="只跑指定步骤，逗号分隔，如 s0,gen_prd")
    parser.add_argument("--recheck", action="store_true",
                        help="只复查 validation_out/ 里的历史输出，不调用 LLM（不花钱）")
    args = parser.parse_args()
    only = {s.strip() for s in args.only.split(",") if s.strip()}

    if args.recheck:
        return await recheck()

    def want(step: str) -> bool:
        return not only or step in only

    settings = get_settings()
    if not settings.llm_configured:
        print("未配置 API Key，无法验证。请在 backend/.env 里配置。", file=sys.stderr)
        return 2

    OUT_DIR.mkdir(exist_ok=True)
    questions, form_version = load_questions()

    # ---- 夹具：一份填了 7 道必填 + 若干选填的表单 ----
    answers = {
        "product_name": "群聊周报助手",
        "product_type": "效率工具（解决单点问题）",
        "problem": "团队群聊里信息散落，每周要人工翻聊天记录拼周报，一次要花两小时，还容易漏掉重要进展。",
        "target_users": "20 人以下创业团队的技术负责人，兼做项目管理",
        "core_features": "1. 自动汇总指定群聊的消息\n2. 按人 / 项目生成周报草稿\n3. 支持人工修改后导出",
        "platform": "Web 网站",
        "need_auth": "需要：仅第三方登录（微信 / GitHub 等）",
        "need_database": "需要：要长期保存用户数据",
        "tech_stack": "React + FastAPI + PostgreSQL",
        "existing_system": "全新项目，没有历史包袱",
        "style_preference": "简洁克制（类似 Linear、Notion）",
        "integrations": "企业微信（拉取群消息）、GitHub OAuth（登录）",
        "core_entities": "用户：id、昵称、所属团队；周报：id、周期、正文、状态、创建人",
        "api_convention": "RESTful + JWT",
        "engineering_conventions": "前端按 features 分目录；接口入参必须校验；提交前跑 lint + 测试",
        # 故意留空：success_metrics / scale_expectation / hard_constraints / compliance
    }
    form_summary = build_form_summary(answers, questions)
    covered = ["目标用户", "核心场景", "范围边界"]

    print("=" * 78)
    print(f"provider={settings.llm_provider} model={settings.active_llm_model} "
          f"max_tokens={settings.llm_max_tokens}")
    print(f"表单版本 {form_version}｜已填 {sum(1 for v in answers.values() if v)} 题｜"
          f"留空 {len(questions) - len(answers)} 题")
    print("=" * 78)

    model = build_chat_model(settings)
    base = {
        "product_name": answers["product_name"],
        "form_summary": form_summary,
        "covered_dimensions": "、".join(covered),
        "known_info": "（尚无）",
        "open_questions": "（尚无）",
        "conflicts": "（尚无）",
        "history": "（这是第一轮）",
        "user_reply": "（无）",
        "round_index": "1",
        "max_rounds": "4",
    }

    # ---------- 对话阶段 ----------
    s0_raw = ""
    if want("s0"):
        print("\n【S0 开场复盘】")
        s0_raw, s0_data = await run_clarify(model, "S0", base)
        if s0_data:
            msg = s0_data.get("message", "")
            check("S0 有【我的判断】/【依据】结构", "判断" in msg or "依据" in msg,
                  f"message 前 60 字：{msg[:60]!r}")

    s1_raw = ""
    if want("s1"):
        print("\n【S1 场景落地】")
        ctx = dict(base, known_info="（S0 已确认方向，尚无结构化信息）", round_index="1",
                   history=f"[AI]\n{s0_raw[:800]}")
        s1_raw, _ = await run_clarify(model, "S1", ctx)

    if want("s2"):
        print("\n【S2 功能细化】")
        ctx = dict(base, round_index="2",
                   known_info="主场景动线：周一早上负责人打开工具 → 选群与时间范围 → 生成 → 修改 → 复制到飞书",
                   target_features="1. 自动汇总指定群聊的消息\n2. 按人 / 项目生成周报草稿",
                   history=f"[AI]\n{s1_raw[:600]}")
        await run_clarify(model, "S2", ctx)

    if want("s4"):
        print("\n【S4 约束与验收】")
        ctx = dict(base, round_index="4",
                   known_info="主场景动线已确认；功能边界已细化",
                   empty_fields="`success_metrics`（成功指标）、`scale_expectation`（规模与性能）、"
                                "`hard_constraints`（硬约束）、`compliance`（安全与合规）")
        await run_clarify(model, "S4", ctx)

    # ---------- 生成阶段 ----------
    # 对话产出（为控制调用次数，用夹具拼一份等效的 known_info）
    known_info = (
        "【主场景动线】周一早上，技术负责人打开工具 → 选一个企业微信群 + 时间范围（默认最近 7 天）"
        "→ 点生成 → 等约 30 秒 → 得到按人分组的周报草稿 → 手动改两处 → 复制到飞书群。\n"
        "【角色】单角色：团队成员。团队负责人可查看全部人的周报；成员只能看自己的。\n"
        "【边界】少于 5 字或纯表情的消息不进周报；某人本周无消息则不出现在周报里；"
        "群消息拉取失败时跳过该群并提示，不整体失败。\n"
        "【不做清单】不做跨群汇总；不做实时同步。\n"
        "【验收口径】生成草稿中 80% 的内容不需要修改。"
    )
    gen_base = dict(base, known_info=known_info)

    prd_content = ""
    if want("gen_prd"):
        print("\n【生成 PRD：第 5–7 章】")
        prd_content = await run_gen(
            model, "prd", gen_base,
            scope="第 5 章 范围边界、第 6 章 核心场景与用户动线、第 7 章 功能需求",
            spec=("第 5 章 简洁，≤300 字，必须用两张表（本期做 / 本期明确不做）；\n"
                  "第 6 章 详细，≤600 字，写成编号步骤的完整动线；\n"
                  "第 7 章 详细，≤1500 字，每条功能独立成节，含描述/输入/输出/边界条件/优先级理由；"
                  "功能编号用 FR-01、FR-02……"),
        )
        check("gen_prd 只输出了要求的章节（未越界）",
              "## 第 5 章" in prd_content and "## 第 1 章" not in prd_content
              and "## 第 13 章" not in prd_content)
        check("gen_prd 出现了 FR 编号", bool(re.search(r"FR-\d{2}", prd_content)))
        check("gen_prd 第 5 章用了两张表", prd_content.count("|") >= 8)

    api_content = ""
    # 只跑部分步骤时，上游产物从上次的输出里取，避免重复调用
    if not want("gen_prd") and (OUT_DIR / "gen_prd.txt").exists():
        prd_content = (OUT_DIR / "gen_prd.txt").read_text(encoding="utf-8")
        print(f"\n[复用] gen_prd.txt（{len(prd_content)} 字）")

    if want("gen_api") and prd_content:
        print("\n【生成接口文档：第 6 章 接口清单】")
        api_content = await run_gen(
            model, "api", dict(gen_base, prd_content=prd_content),
            scope="第 6 章 接口清单",
            spec=("概要，≤400 字。必须输出两张表：\n"
                  "① 接口清单：方法 / 路径 / 用途 / 对应的 FR-xx；\n"
                  "② 无接口功能：FR-xx / 功能名 / 为什么不需要接口。"),
        )
        check("gen_api 输出了「无接口功能」表", "无接口功能" in api_content)
        check("gen_api 的接口都标了 FR 来源", bool(re.search(r"FR-\d{2}", api_content)))
        check_api_mutex(api_content)

    if not want("gen_api") and not api_content and (OUT_DIR / "gen_api.txt").exists():
        api_content = (OUT_DIR / "gen_api.txt").read_text(encoding="utf-8")
        print(f"[复用] gen_api.txt（{len(api_content)} 字）")

    if want("gen_prompts") and prd_content:
        print("\n【生成提示词套件：00-README + 01-project-brief】")
        prompts_content = await run_gen(
            model, "prompts", dict(gen_base, prd_content=prd_content, api_content=api_content or "（尚无）"),
            scope="00-README.md、01-project-brief.md",
            spec=("00-README：套件说明、使用顺序、覆盖矩阵（FR-xx × 优先级 × 覆盖步骤）；\n"
                  "01-project-brief：≤1000 字，含技术栈、目录结构、数据约定、错误处理约定、"
                  "视觉与交互约定、安全与合规约定、硬性禁止事项。\n"
                  "用 === FILE: <相对路径> === 分隔各文件。"),
        )
        files = re.findall(r"^=== FILE: (.+?) ===$", prompts_content, re.M)
        observe(f"gen_prompts 解析出 {len(files)} 个文件块：{files}")
        check("gen_prompts 输出了分隔行", bool(files))
        check("gen_prompts 分隔行格式正确（含 .md 路径）",
              all(f.endswith(".md") for f in files) if files else False)
        check("gen_prompts 的分隔契约可解析出预期文件数", len(files) == 2, f"实际 {len(files)} 个")
        brief = prompts_content.split("=== FILE: 01-project-brief.md ===")[-1]
        check_no_substitution(brief)

    # ---------- 汇总 ----------
    print("\n" + "=" * 78)
    print(f"输出已保存到 {OUT_DIR.relative_to(BACKEND_DIR)}/")
    if OBSERVATIONS:
        print(f"\n需要注意的观察（{len(OBSERVATIONS)} 条）：")
        for o in OBSERVATIONS:
            print(f"  - {o}")
    print()
    if FAILURES:
        print(f"未通过 {len(FAILURES)} 项：")
        for f in FAILURES:
            print(f"  ✗ {f}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
