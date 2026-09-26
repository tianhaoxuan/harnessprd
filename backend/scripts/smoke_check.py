"""骨架自检：不联网、不需要 API Key。

覆盖：配置加载、提示词资源清单、三家 provider 的模型构造、路由注册与占位状态码。
**不含**真实 LLM 验证 —— 那由 scripts/validate_prompts.py 承担（会花钱、会调模型）。

运行（在 backend/ 目录下）：

    .venv\\Scripts\\python scripts\\smoke_check.py
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from pathlib import Path
from types import SimpleNamespace

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from dotenv import dotenv_values  # noqa: E402
from core.config import FALLBACK_MODELS, Settings, get_settings  # noqa: E402
from core.prompts import (  # noqa: E402
    available_prompts,
    build_system_prompt,
    declared_placeholders,
    load_prompt,
    render_prompt,
    render_prompt_text,
)
from core.questions import load_questions  # noqa: E402
from services.llm import (  # noqa: E402
    LlmConfigError,
    StreamOutcome,
    build_chat_model,
    finish_reason_of,
    is_truncated,
)

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    status = "PASS" if condition else "FAIL"
    suffix = f" —— {detail}" if detail else ""
    print(f"[{status}] {name}{suffix}")
    if not condition:
        FAILURES.append(name)


def _outcome_wiring_report() -> str | None:
    """检查每个流式路由都真的把 `StreamOutcome` 交出去了。通过返回 `None`。

    为什么要**遍历路由**而不是写死函数名：漏接 `outcome` **不会报任何错** ——
    那一流的截断就悄悄变回静默（`HANDOFF.md` §4 坑 #18 的原形）。新加一个流式路由时，
    写死名单的检查不会覆盖它，而遍历会自动把它纳进来。
    """
    import inspect  # noqa: PLC0415 - 只在自检里用

    from api import conversation as api_conversation  # noqa: PLC0415
    from services import conversation_service, document_service  # noqa: PLC0415

    # 两个生成服务都要把 finish_reason 记下来（否则 outcome 永远是 None）
    for module in (document_service, conversation_service):
        if "finish_reason_of(" not in inspect.getsource(module):
            return f"{module.__name__} 没有调用 finish_reason_of()"

    checked = 0
    for route in api_conversation.router.routes:
        endpoint = getattr(route, "endpoint", None)
        if endpoint is None:
            continue
        source = inspect.getsource(endpoint)
        if "_make_sse_generator" not in source and "_make_stage_sse_generator" not in source:
            continue
        checked += 1
        path = getattr(route, "path", "?")
        if "StreamOutcome()" not in source:
            return f"{path} 没有创建 StreamOutcome"
        if "outcome=outcome" not in source:
            return f"{path} 没有把 outcome 传给服务层"
        if all(s not in source for s in ("_make_sse_generator(stream, outcome)", "_make_stage_sse_generator(events, outcome)")):
            return f"{path} 没有把 outcome 交给 _make_sse_generator"

    # 7 = 对话 2 条 + 文档 5 条（PRD、从摘要生成 PRD、接口文档、套件、单节修订）；
    # 少一条说明路由被删了或改名了，值得人看一眼
    if checked != 7:
        return f"只发现 {checked} 个流式路由，期望 7 个"
    return None


def _independent_chapter_rows(text: str) -> list[tuple[str, str]]:
    """从 `gen_api.md` 里独立数一遍章节。

    **刻意不复用 `services.document_plan` 的解析函数** —— 用被测代码去核对被测代码
    等于自证。这里用最朴素的方式再解析一遍，两者对不上就说明有一边错了。
    """
    rows: list[tuple[str, str]] = []
    in_section = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            in_section = "章节清单" in stripped
            continue
        if not in_section or not stripped.startswith("|"):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if len(cells) < 2 or not cells[0] or cells[0] == "#":
            continue
        if set(cells[0]) <= {"-", " "}:
            continue
        if cells[0].isdigit() or cells[0].startswith("附"):
            rows.append((cells[0], cells[1]))
    return rows


def _independent_suite_files(text: str) -> list[str]:
    """从 `gen_prompts.md` 的目录结构代码块里独立数一遍文件（同上：不复用被测解析）。"""
    files: list[str] = []
    in_section = False
    in_block = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            in_section = "目录结构" in stripped
            in_block = False
            continue
        if not in_section:
            continue
        if stripped.startswith("```"):
            if in_block:
                break
            in_block = True
            continue
        if not in_block:
            continue
        candidate = stripped.lstrip("│├└─ ").strip()
        if candidate.endswith(".md") and candidate not in files:
            files.append(candidate)
    return files


def main() -> int:
    # ⚠️ **这一句可能就失败**：`.env` 里的某个值不合法时，配置模型会抛 ValidationError
    # —— 应用在容器里也是同样地"启动即崩"（现象：`docker compose ps` 显示 `api unhealthy`，
    # 容器反复 `Restarting (1)`，而日志里只有一行 pydantic 报错，很难联想到是配置值写错）。
    # 所以这里**不许它以 traceback 的形式炸**，要打印一句人话。
    # 实测踩过：模板里写 `ENVIRONMENT=production`（只接受 local/dev/staging/prod）。
    try:
        settings = get_settings()
    except Exception as exc:  # noqa: BLE001 - 自检要报告任何配置加载失败
        print("配置无法加载 —— 服务会以同样的错误**启动即崩**：\n")
        for line in str(exc).splitlines():
            if line.strip():
                print("  " + line.strip())
        print("\n提示：这两个最容易写错（取值大小写敏感）——")
        print("  ENVIRONMENT            只接受 local / dev / staging / prod")
        print("  DEFAULT_LLM_PROVIDER   只接受 deepseek / openai / anthropic")
        print("完整取值清单见 docs/部署.md §5.5。")
        return 1
    print(
        f"配置：provider={settings.llm_provider} model={settings.active_llm_model} "
        f"env={settings.environment} key_configured={settings.llm_configured}"
    )
    print(f"环境文件：{settings.model_config.get('env_file')}")
    print()

    prompts = set(available_prompts())
    expected_prompts = {
        "clarify_common",
        *(f"clarify_s{i}" for i in range(6)),
        "gen_common",
        "gen_prd",
        "gen_api",
        "gen_prompts",
    }
    check(
        "提示词资源齐全（11 份可用）",
        expected_prompts <= prompts,
        f"缺失 {sorted(expected_prompts - prompts)}"
        if expected_prompts - prompts
        else f"共 {len(prompts)} 份",
    )
    # 旧提示词必须已删除 —— 留着就是两套逻辑长期并存（HANDOFF.md §7）
    stale_prompts = {"system_prd", "clarify"} & prompts
    check("旧提示词已删除", not stale_prompts, f"仍存在 {sorted(stale_prompts)}")
    check(
        "gen_prd 提示词非空",
        len(load_prompt("gen_prd")) > 500,
        f"{len(load_prompt('gen_prd'))} 字符",
    )

    # ---------- 提示词组装与渲染（docs/对话阶段设计.md §8 第 1 项）----------
    assembled = build_system_prompt("clarify_s0", {"product_name": "探针产品"})
    check(
        "build_system_prompt 把 common 拼在前面",
        assembled.startswith(load_prompt("clarify_common")[:40])
        and "探针产品" in assembled,
    )
    check(
        "build_system_prompt 确实是两份文件之和",
        len(assembled) > len(load_prompt("clarify_s0")),
        f"{len(assembled)} 字符",
    )
    # 值里带花括号不能被当成占位符 —— 这正是 str.format 会炸、str.replace 不会的场景
    braced = render_prompt("clarify_s0", {"product_name": "A{B}C", "form_summary": "x=y"})
    check("render_prompt 用 replace 而非 format（值含花括号不炸）", "A{B}C" in braced)

    declared = declared_placeholders("gen_common")
    check(
        "declared_placeholders 解析出声明清单",
        {"doc_outline", "generate_scope", "scope_spec", "form_summary"} <= declared,
        f"共 {len(declared)} 项：" + "、".join(sorted(declared)),
    )
    check(
        "declared_placeholders 不把格式记号误当占位符",
        "id" not in declared,
        f"id 是否被误抓 = {'id' in declared}",
    )

    # ---------- 提示词命名入口（services/prompts.py）----------
    # 这几条的价值在于：**谁把提示词正文复制进 Python 常量，它会立刻失败**。
    # 正文只能来自 core/prompts/*.md（HANDOFF.md §4 坑 #10：两份来源必漂移）。
    import services.prompts as sp  # noqa: PLC0415

    check(
        "PRD_GENERATION_PROMPT 逐字等于 gen_common + gen_prd（无内嵌正文）",
        sp.PRD_GENERATION_PROMPT == load_prompt("gen_common") + "\n\n" + load_prompt("gen_prd"),
        f"常量 {len(sp.PRD_GENERATION_PROMPT)} 字符",
    )
    check(
        "API_DOCS_GENERATION_PROMPT 逐字等于 gen_common + gen_api",
        sp.API_DOCS_GENERATION_PROMPT
        == load_prompt("gen_common") + "\n\n" + load_prompt("gen_api"),
    )
    check(
        "PROMPTS_GENERATION_PROMPT 逐字等于 gen_common + gen_prompts",
        sp.PROMPTS_GENERATION_PROMPT
        == load_prompt("gen_common") + "\n\n" + load_prompt("gen_prompts"),
    )
    check(
        "SYSTEM_PROMPT 就是 gen_common（基线不含产物专属规则）",
        sp.SYSTEM_PROMPT == load_prompt("gen_common"),
    )
    check(
        "两份角色基线确实不同（澄清侧 ≠ 生成侧）",
        sp.CLARIFY_SYSTEM_PROMPT == load_prompt("clarify_common")
        and sp.CLARIFY_SYSTEM_PROMPT != sp.SYSTEM_PROMPT,
    )
    check(
        "常量是**未渲染**模板（占位符原样保留，别拿去 str.format）",
        "{generate_scope}" in sp.PRD_GENERATION_PROMPT
        and "{form_summary}" in sp.CLARIFY_SYSTEM_PROMPT,
    )
    check(
        "OPTIMIZE_DOCUMENT_PROMPT_TEMPLATE 是用户消息模板而非 system prompt",
        "{feedback}" in sp.OPTIMIZE_DOCUMENT_PROMPT_TEMPLATE
        and "{section}" in sp.OPTIMIZE_DOCUMENT_PROMPT_TEMPLATE,
    )

    # ---------- LLM 工厂门面（services/llm_factory.py）----------
    from services.llm_factory import SUPPORTED_PROVIDERS, get_llm  # noqa: PLC0415

    check(
        "工厂覆盖三家 provider（含唯一配了 Key 的 deepseek）",
        set(SUPPORTED_PROVIDERS) == {"deepseek", "openai", "anthropic"},
        "、".join(SUPPORTED_PROVIDERS),
    )
    for provider in SUPPORTED_PROVIDERS:
        try:
            llm = get_llm(
                provider,
                settings=Settings(
                    _env_file=None,
                    llm_provider=provider,  # type: ignore[arg-type]
                    deepseek_api_key="sk-dummy",
                    openai_api_key="sk-dummy",
                    anthropic_api_key="sk-dummy",
                ),
            )
        except Exception as exc:  # noqa: BLE001 - 自检要报告任何构造失败
            check(f"get_llm({provider}) 可构造", False, f"{type(exc).__name__}: {exc}")
        else:
            check(f"get_llm({provider}) 可构造", True, type(llm).__name__)

    overridden = get_llm(
        "openai",
        settings=Settings(_env_file=None, llm_provider="deepseek", openai_api_key="sk-dummy"),
    )
    check(
        "get_llm(provider=...) 能覆盖配置",
        type(overridden).__name__ == "ChatOpenAI",
        type(overridden).__name__,
    )
    try:
        get_llm(settings=Settings(_env_file=None, llm_provider="deepseek", deepseek_api_key=None))
    except LlmConfigError as exc:
        check("get_llm 缺 Key 抛 LlmConfigError", True, str(exc)[:40] + "…")
    except Exception as exc:  # noqa: BLE001
        check("get_llm 缺 Key 抛 LlmConfigError", False, f"实际 {type(exc).__name__}")
    else:
        check("get_llm 缺 Key 抛 LlmConfigError", False, "未抛异常")

    # ---------- 对话服务（services/conversation_service.py）----------
    import services.conversation_service as cs  # noqa: PLC0415

    sample_form = {
        "product_name": "群聊周报助手",
        "problem": "群聊信息散落，每周手工拼周报要两小时",
    }
    rendered_form = cs.format_form_data(sample_form)

    check(
        "format_form_data 用「label（`id`）：值」格式",
        "- 产品名称（`product_name`）：群聊周报助手" in rendered_form,
    )
    manual_form = "\n".join(
        f"- {q.label}（`{q.id}`）：{(sample_form.get(q.id) or '').strip() or '**未填写**'}"
        for q in load_questions().all_questions
    )
    check(
        "format_form_data 与验证脚本当初的格式逐字一致",
        rendered_form == manual_form,
        f"{len(rendered_form)} 字符",
    )
    # 未答项必须显式标出：漏了模型会拿邻近内容顶替（HANDOFF §4 坑 #3）
    check(
        "format_form_data 把未答项标为「未填写」",
        rendered_form.count("**未填写**") == 20 - len(sample_form),
        f"{rendered_form.count('**未填写**')} 项未填写",
    )

    dims = cs.covered_dimensions(sample_form)
    check(
        "covered_dimensions 只算已填项（留空不算覆盖）",
        "核心场景" in dims and "成功指标" not in dims,
        "、".join(dims) or "（空）",
    )

    lc_messages = cs._build_lc_messages(
        "SYS",
        [{"role": "user", "content": "第一轮"}, {"role": "ai", "content": "收到"}],
        "第二轮",
    )
    check(
        "_build_lc_messages 顺序为 System → 历史 → 当前输入",
        [type(m).__name__ for m in lc_messages]
        == ["SystemMessage", "HumanMessage", "AIMessage", "HumanMessage"],
        str([type(m).__name__ for m in lc_messages]),
    )
    back = cs._serialize_messages(lc_messages, stage="S1")
    check(
        "_serialize_messages 丢掉 SystemMessage 且 role 映射正确",
        [m["role"] for m in back] == ["user", "ai", "user"],
        str([m["role"] for m in back]),
    )
    check(
        "_serialize_messages 补齐 stage / kind / created_at",
        all({"id", "role", "stage", "kind", "content", "created_at"} <= set(m) for m in back)
        and all(m["stage"] == "S1" for m in back),
    )

    from langchain_core.messages import AIMessage  # noqa: PLC0415

    check(
        "message_text 只取 text block（排除思考模式的 reasoning）",
        cs.message_text(
            AIMessage(
                content=[
                    {"type": "reasoning", "text": "内心戏"},
                    {"type": "text", "text": "给用户看的话"},
                ]
            )
        )
        == "给用户看的话",
    )

    # 六个阶段的提示词渲染后**正文不得残留占位符**（HANDOFF §4 坑 #11）。
    # 只看正文、跳过 `|` 开头的行 —— 提示词里的「占位符清单」表格本身就该含字面量 `{name}`。
    service = cs.ConversationService()
    for stage in ("S0", "S1", "S2", "S3", "S4", "S5"):
        values = service.clarify_values(stage=stage, form=sample_form, round_index=2)
        prompt = service.render_system_prompt(stage, values)
        body = "\n".join(
            line for line in prompt.splitlines() if not line.lstrip().startswith("|")
        )
        leftover = sorted(set(re.findall(r"\{([a-z][a-z0-9_]*)\}", body)))
        check(f"clarify_{stage} 渲染后正文无残留占位符", not leftover, str(leftover) if leftover else "干净")

    # ---------- 产物生成服务（services/document_service.py）----------
    # 全部是**纯逻辑 + 假模型**，不联网。真实验证由 validate_prompts.py 承担。
    import services.document_service as ds  # noqa: PLC0415

    doc_service = ds.DocumentService()

    # gen 族的三个分片占位符是坑 #11 的正主：声明了却从没被注入过。
    # 这里逐产物渲染一遍，确认**运行时占位符**一个都没剩下。
    #
    # ⚠️ 只针对**已声明**的占位符，不能"凡是 `{小写}` 就算残留"：
    # 提示词里还有**格式记号**（`gen_api.md` 的 `{id}`、`{模块缩写}{3位序号}`），
    # 它们是给人看的、本就不该被替换（`gen_common.md` §渲染方式警告）。
    # 拿一个大而全的正则去扫会把这些误报成 bug —— 实测 `{id}` 就被误报过。
    doc_values = doc_service.generation_values(
        form=sample_form,
        known_info="【主场景动线】周一早上打开工具 → 选群 → 生成 → 改 → 复制出去。",
        open_questions="- 是否支持导出为 Word？",
        conflicts="（无）",
        scope=ds.DocumentScope(
            outline="第 1–17 章（完整结构清单见模板）",
            scope="第 5 章 范围边界、第 6 章 核心场景、第 7 章 功能需求",
            spec="第 5 章 ≤300 字，两张表；第 7 章 ≤1500 字，功能编号 FR-01 起。",
        ),
        prd_content="# PRD\n\n## 第 7 章 功能需求\n\n- FR-01 自动汇总群消息",
        api_content="# 接口文档\n\n## 第 6 章 接口清单\n\n- POST /api/v1/reports",
    )
    check(
        "gen 族声明的占位符已全部注入（坑 #11 的守卫）",
        not ds.missing_placeholders("prd", doc_values)
        and not ds.missing_placeholders("api", doc_values)
        and not ds.missing_placeholders("prompts", doc_values),
        f"prd 缺 {ds.missing_placeholders('prd', doc_values)}；"
        f"api 缺 {ds.missing_placeholders('api', doc_values)}；"
        f"prompts 缺 {ds.missing_placeholders('prompts', doc_values)}",
    )
    for kind in ("prd", "api", "prompts"):
        prompt = doc_service.render_system_prompt(kind, doc_values)
        # `system_template()`（取常量）与 `render_system_prompt()`（现读现拼）是两条路径，
        # 必须钉住它们等价 —— 否则一边改了组装规则、另一边没改，就出现第三份提示词来源。
        check(
            f"gen_{kind} 的「模板常量」与「现拼」两条路径等价",
            render_prompt_text(doc_service.system_template(kind), doc_values) == prompt,
        )
        declared_here = set(declared_placeholders("gen_common")) | set(
            declared_placeholders(f"gen_{kind}")
        )
        found = set(re.findall(r"\{([a-z][a-z0-9_]*)\}", prompt))
        leftover = sorted(declared_here & found)
        check(
            f"gen_{kind} 渲染后无残留的**已声明**占位符",
            not leftover,
            str(leftover) if leftover else f"干净（正文里另有格式记号 {sorted(found - declared_here)}）",
        )
        # 组装顺序：`gen_common` 在前、产物文件在后，且**不夹带任何额外文字**。
        # 用逐字比对而不是 startswith —— 基线里的占位符已被替换，
        # 拿未渲染的原文去 startswith 永远不成立。
        expected = (
            render_prompt_text(load_prompt("gen_common"), doc_values)
            + "\n\n"
            + render_prompt_text(load_prompt(f"gen_{kind}"), doc_values)
        )
        check(
            f"gen_{kind} 的 system prompt 逐字等于「渲染后的 gen_common + gen_{kind}」",
            prompt == expected,
            f"实际 {len(prompt)} 字符 / 期望 {len(expected)} 字符",
        )
        check(
            f"gen_{kind} 基线与产物文件的先后顺序正确",
            prompt.find(render_prompt_text(load_prompt(f"gen_{kind}"), doc_values)) > 0,
        )

    # human message 必须与 validate_prompts.py 验证时的形状**逐字一致** ——
    # 改了措辞就换了验证条件，validation_out/ 那 7 份留档不再说明任何事。
    check(
        "human 指令模板与验证脚本逐字一致",
        ds.GEN_HUMAN_TEMPLATE == "本次只生成：{generate_scope}\n\n详细规格：\n{scope_spec}",
        repr(ds.GEN_HUMAN_TEMPLATE),
    )
    human = doc_service.render_human_prompt(doc_values)
    check(
        "human message 渲染后注入的是同一个 generate_scope（两处不可能不一致）",
        doc_values["generate_scope"] in human and "{generate_scope}" not in human,
    )

    # 链条约束：接口文档 / 提示词套件都必须从 PRD 推导，缺 PRD 直接失败。
    # 用 asyncio 驱动到第一个 yield —— 校验发生在产出任何内容之前，所以不需要真模型。
    async def _first_chunk(agen):
        return await anext(agen)

    async def _collect_all(agen):
        return "".join([piece async for piece in agen])

    for label, coro_factory in (
        ("generate_api_docs_stream", lambda: doc_service.generate_api_docs_stream(
            form=sample_form, prd_content="   ")),
        ("generate_prompts_stream", lambda: doc_service.generate_prompts_stream(
            form=sample_form, prd_content="")),
    ):
        try:
            asyncio.run(_first_chunk(coro_factory()))
            raised = ""
        except ValueError as exc:
            raised = str(exc)
        check(f"{label} 缺 PRD 时抛 ValueError", "prd_content" in raised, raised or "没抛")

    # ---------- 生成/修订的组装正确性（假模型，不联网）----------
    class _Chunk:
        """`message_text` 只读 `.content`，所以假片段有这个属性就够了。"""

        def __init__(self, content):
            self.content = content

    class _FakeModel:
        """记录收到的消息，按给定片段回流。**不继承 BaseChatModel** ——
        这里只需要 `astream`，继承反而要跑一遍 LangChain 的校验。"""

        def __init__(self, chunks):
            self.chunks = chunks
            self.seen: list = []

        async def astream(self, messages):
            self.seen.append(list(messages))
            for c in self.chunks:
                yield _Chunk(c)

    fake = _FakeModel(["第一段", "", "第二段"])  # 空片段应被跳过
    svc = ds.DocumentService(model=fake)
    got = asyncio.run(_collect_all(svc.generate_prd_stream(form=sample_form)))
    check("流式产出跳过空片段", got == "第一段第二段", repr(got))
    sent = fake.seen[0]
    check(
        "消息顺序为 System → Human",
        len(sent) == 2 and type(sent[0]).__name__ == "SystemMessage"
        and type(sent[1]).__name__ == "HumanMessage",
        "、".join(type(m).__name__ for m in sent),
    )

    # 修订：generate_scope **强制**等于 section。
    # 否则 system prompt 说"只输出 A"、human 说"只输出 B"，模型收到两条矛盾指令。
    fake2 = _FakeModel(["修订稿"])
    svc2 = ds.DocumentService(model=fake2)
    asyncio.run(_collect_all(svc2.optimize_document_stream(
        kind="prd",
        section="## 第 5 章 范围边界",
        feedback="本期不做跨群汇总，请写清楚",
        current_content="## 第 5 章 范围边界\n\n（原文）",
    )))
    opt_system = fake2.seen[0][0].content
    opt_human = fake2.seen[0][1].content
    check(
        "修订时 generate_scope 被强制对齐到 section",
        "## 第 5 章 范围边界" in opt_system and "{generate_scope}" not in opt_system,
    )
    check(
        "修订的 human message 带上了该节现有正文与用户反馈",
        "（原文）" in opt_human and "本期不做跨群汇总" in opt_human
        and "{current_content}" not in opt_human,
        opt_human[:80],
    )
    low = "\n".join(
        line for line in opt_system.splitlines() if not line.lstrip().startswith("|")
    )
    check(
        "修订渲染后正文同样无残留占位符",
        not sorted(set(re.findall(r"\{([a-z][a-z0-9_]*)\}", low))),
        str(sorted(set(re.findall(r"\{([a-z][a-z0-9_]*)\}", low)))),
    )

    # ---------- 对话 → 结构化摘要回填（纯逻辑 + 假模型，不联网） ----------
    # 这一组守的是 prompt 里那四条规则的**硬保证**：模型不听话时服务端仍然不失守。
    from langchain_core.messages import AIMessage  # noqa: PLC0415

    class _FakeSyncModel:
        """`ainvoke` 返回一段固定文本，模拟模型给的 JSON。"""

        def __init__(self, text, finish_reason="stop"):
            self.text = text
            self.seen: list = []
            self._reason = finish_reason

        async def ainvoke(self, messages):
            self.seen.append(list(messages))
            return AIMessage(
                content=self.text,
                response_metadata={"finish_reason": self._reason},
            )

    base_summary = {
        "product_name": "群聊周报助手",
        "product_goal": "把周报整理从两小时降到十分钟",
        "target_users": ["技术负责人"],
        "platform": "Web 网站",
        "mvp_features": [{"name": "绑定群聊并汇总", "description": "选定范围后汇总"}],
        "technical_constraints": {"stack": "React + FastAPI", "auth": "账号密码"},
    }

    # 1) 围栏 + 前后有解释文字也要能解析（模型最爱干的两件事）
    ok_parse = True
    detail = ""
    try:
        parsed = ds._extract_json_object(
            '好的，这是结果：\n```json\n{"product_name": "X"}\n```\n希望有帮助'
        )
        ok_parse = parsed == {"product_name": "X"}
    except Exception as exc:  # noqa: BLE001
        ok_parse, detail = False, f"{type(exc).__name__}: {exc}"
    check("回填：能从围栏与解释文字里抠出 JSON 对象", ok_parse, detail or "解析正确")

    # 2) 越界键必须被丢掉（规则 3 的硬保证）
    filtered, dropped = ds._filter_by_schema(
        {
            "product_name": "A",
            "invented_key": "不该出现",
            "mvp_features": [{"name": "F", "user_value": "也不该出现"}],
        },
        {"product_name", "mvp_features"},
        {"mvp_features": {"name", "description", "priority"}},
    )
    check(
        "回填：schema 之外的键（含数组元素内的）被丢弃并留痕",
        set(filtered) == {"product_name", "mvp_features"}
        and filtered["mvp_features"] == [{"name": "F"}]
        and len(dropped) == 2,
        f"dropped={dropped}",
    )

    # 3) 空值不覆盖非空值（规则 2）+ 未返回的键保持原值（规则 1）
    merged, changed = ds._merge_summary(
        base_summary, {"product_goal": "", "platform": "微信小程序", "v2_features": []}
    )
    check(
        "回填：模型返回空值时保留原值，返回新值时接受并记为改动",
        merged["product_goal"] == base_summary["product_goal"]
        and merged["platform"] == "微信小程序"
        and "platform" in changed
        and "product_goal" not in changed,
        f"changed={changed}",
    )
    check(
        "回填：模型没返回的键原样保留（是合并而不是替换）",
        merged["target_users"] == base_summary["target_users"]
        and merged["technical_constraints"] == base_summary["technical_constraints"],
    )

    # 4) AI 侧原始 JSON 信封要被剥成可读正文 —— 否则模型会照着模仿输出信封
    rendered = ds._render_conversation(
        [
            {"role": "user", "content": "我们大概 20 个人用"},
            {"role": "ai", "content": '{"message": "明白，我记下了", "questions": []}'},
        ]
    )
    check(
        "回填：对话历史里 AI 的 JSON 信封被剥成正文",
        "明白，我记下了" in rendered and '"questions"' not in rendered
        and "【用户】我们大概 20 个人用" in rendered,
        rendered[:60],
    )

    # 5) 端到端（假模型）：完整结构 + 改动字段 + 越界键留痕
    fake_sync = _FakeSyncModel(
        '```json\n{"product_name": "群聊周报助手", "platform": "微信小程序",'
        ' "bogus": 1}\n```'
    )
    svc3 = ds.DocumentService(model=fake_sync)
    result = asyncio.run(
        svc3.sync_requirements_summary(
            summary=base_summary,
            history=[{"role": "user", "content": "改成小程序吧"}],
        )
    )
    check(
        "回填：返回完整结构（含未改动的字段）",
        set(result.summary) >= set(base_summary),
        "、".join(sorted(result.summary)),
    )
    check(
        "回填：changed 只列真正变了的字段",
        result.changed == ("platform",),
        f"changed={result.changed}",
    )
    check(
        "回填：越界键进 dropped_keys（前端可据此提示提示词退化）",
        result.dropped_keys == ("bogus",),
        f"dropped={result.dropped_keys}",
    )
    check(
        "回填：截断信息被如实带出（正常写完时 finish_reason=stop 且不截断）",
        result.truncated is False and result.finish_reason == "stop",
        f"{result.finish_reason} / truncated={result.truncated}",
    )
    sync_system = fake_sync.seen[0][0].content
    sync_human = fake_sync.seen[0][1].content
    check(
        "回填：system 用的是 sync_summary.md，且 human 注入了 schema 原文与当前摘要",
        "只回填" in sync_system
        and '"product_name"' in sync_human
        and "additionalProperties" in sync_human
        and "{current_summary}" not in sync_human,
        f"system={len(sync_system)} 字 / human={len(sync_human)} 字",
    )

    # 6) 模型给出不可解析的输出时必须**报错**，不能返回半个摘要
    try:
        asyncio.run(
            ds.DocumentService(model=_FakeSyncModel("抱歉，我没法完成这个请求。"))
            .sync_requirements_summary(summary=base_summary, history=[])
        )
    except ds.SummarySyncError:
        check("回填：不可解析的输出抛 SummarySyncError（不返回残摘要）", True)
    except Exception as exc:  # noqa: BLE001
        check("回填：不可解析的输出抛 SummarySyncError（不返回残摘要）", False,
              f"抛的是 {type(exc).__name__}")
    else:
        check("回填：不可解析的输出抛 SummarySyncError（不返回残摘要）", False, "居然成功了")

    try:
        asyncio.run(_collect_all(svc2.optimize_document_stream(
            kind="prd", section="", feedback="x", current_content="y")))
        section_raised = ""
    except ValueError as exc:
        section_raised = str(exc)
    check("修订缺 section 时抛 ValueError（不该静默）", "section" in section_raised,
          section_raised or "没抛")

    # ---------- 生成 / 修订接口的入参契约（纯 pydantic，不联网）----------
    # 这些校验**必须发生在开流之前**。服务层的生成方法是 async generator，它的
    # `ValueError` 发生在产出第一个 chunk 之前，会被 `_make_sse_generator` 转成
    # "HTTP 200 + 一个 error 事件" —— 而配置类/入参类错误应当是 4xx / 503
    # （见 api/conversation.py 模块 docstring）。
    import api.schemas as sc  # noqa: PLC0415
    from pydantic import ValidationError  # noqa: PLC0415

    def _rejected(factory) -> str:
        """返回校验错误文本；**没被拒**时返回空串。"""
        try:
            factory()
        except ValidationError as exc:
            return str(exc)
        return ""

    blank = _rejected(lambda: sc.GenerateApiDocsRequest(prd_content="   "))
    check("prd_content 只有空白被拒（NonBlankStr）", "prd_content" in blank, blank[:70] or "没拒")
    missing = _rejected(lambda: sc.GenerateApiDocsRequest())
    check("generate-api-docs 缺 prd_content 被拒", "prd_content" in missing, missing[:70] or "没拒")

    api_kwargs = sc.GenerateApiDocsRequest(
        form=sample_form,
        prd_content="# PRD\n\n- FR-01 自动汇总群消息",
        scope={"outline": "第 1–17 章", "scope": "第 6 章 接口清单", "spec": "≤400 字，两张表"},
    ).generation_kwargs()
    check(
        "scope 被转成服务层的 DocumentScope（不在 API 层另造同形模型）",
        isinstance(api_kwargs.get("scope"), ds.DocumentScope),
        repr(api_kwargs.get("scope")),
    )
    check(
        "未提供的对话侧字段**不出现**在 kwargs 里（哨兵只由服务层定义）",
        not {"known_info", "open_questions", "conflicts"} & set(api_kwargs),
        str(sorted(api_kwargs)),
    )

    need_prd = _rejected(
        lambda: sc.OptimizeDocumentRequest(kind="api", section="### x", current_content="y")
    )
    check(
        "修订 api 类产物时缺 prd_content 被拒（model_validator）",
        "prd_content" in need_prd,
        need_prd[:70] or "没拒",
    )
    opt_ok = sc.OptimizeDocumentRequest(kind="prd", section="### x", current_content="y")
    opt_kwargs = opt_ok.optimize_kwargs()
    check(
        "修订 kwargs 里**没有** scope —— generate_scope 被强制取 section",
        "scope" not in opt_kwargs,
        str(sorted(opt_kwargs)),
    )
    check(
        "修订 kwargs 带上了 kind / section / current_content",
        {"kind", "section", "current_content"} <= set(opt_kwargs)
        and opt_kwargs["section"] == "### x",
        str(sorted(opt_kwargs)),
    )

    # ---------- SSE 协议（api/conversation.py 的 _make_sse_generator）----------
    # 纯逻辑：喂假的异步迭代器。**不花钱、不依赖上游**，所以能覆盖到
    # "中途失败"这种真实调用极难复现的分支。
    import api.conversation as conv  # noqa: PLC0415

    async def _ok_source():
        yield "第一段"
        yield ""  # 空片段应被跳过
        yield "第二段"

    async def _fail_source():
        yield "第一段"
        raise RuntimeError("模拟上游失败")

    async def _multiline_source():
        yield "第一行\n第二行"

    async def _collect(agen):
        return [frame async for frame in agen]

    ok_frames = asyncio.run(_collect(conv._make_sse_generator(_ok_source())))
    check("SSE 成功流产出 2 个 chunk + 1 个 done", len(ok_frames) == 3, f"{len(ok_frames)} 帧")
    check(
        "SSE 帧结构为「event: 名 / data: JSON / 空行」",
        all(
            frame.startswith("event: ") and "\ndata: " in frame and frame.endswith("\n\n")
            for frame in ok_frames
        ),
    )
    done_payload = json.loads(ok_frames[-1].split("data: ", 1)[1])
    check(
        "SSE done 带 chunks / chars，且空片段不计入",
        done_payload == {"chunks": 2, "chars": 6},
        str(done_payload),
    )

    fail_frames = asyncio.run(_collect(conv._make_sse_generator(_fail_source())))
    check("SSE 失败流产出 chunk + error，且**不补 done**", len(fail_frames) == 2, f"{len(fail_frames)} 帧")
    error_payload = json.loads(fail_frames[-1].split("data: ", 1)[1])
    check(
        "SSE error 带 type / message",
        error_payload.get("type") == "RuntimeError" and "模拟上游失败" in error_payload.get("message", ""),
        str(error_payload),
    )

    multiline = asyncio.run(_collect(conv._make_sse_generator(_multiline_source())))
    check(
        "多行文本被 JSON 转义成单行 data（SSE 帧格式要求）",
        multiline[0].count("\n") == 3,
        f"{multiline[0].count(chr(10))} 个换行",
    )
    check(
        "SSE_HEADERS 禁缓存 / 禁代理缓冲",
        conv.SSE_HEADERS.get("Cache-Control") == "no-cache"
        and conv.SSE_HEADERS.get("X-Accel-Buffering") == "no",
        "、".join(f"{k}={v}" for k, v in conv.SSE_HEADERS.items()),
    )
    check(
        "Content-Type 不写进 SSE_HEADERS（由 media_type 提供，避免两份声明）",
        "Content-Type" not in conv.SSE_HEADERS and conv.SSE_MEDIA_TYPE == "text/event-stream",
    )

    # ---------- 表单题目（/conversation/questions 的数据源）----------
    # 配置外置是硬要求（F2.6「禁止在代码里再硬编码一份」），所以这里盯住契约而不是代码
    form = load_questions()
    questions = form.all_questions
    check("表单配置可加载", form.version == "1.2", f"版本 {form.version}")
    check("题目总数 20", len(questions) == 20, f"{len(questions)} 题")
    check("必填 7 题", sum(q.required for q in questions) == 7, f"{sum(q.required for q in questions)} 题")
    check("题目 id 唯一", len({q.id for q in questions}) == len(questions))
    check(
        "text/textarea 的 options 为空数组",
        all(not q.options for q in questions if q.type in ("text", "textarea")),
    )
    check(
        "select/radio 必有选项且 maxLength 为 null",
        all(
            q.options and q.maxLength is None
            for q in questions
            if q.type in ("select", "radio")
        ),
    )

    # ---------- 配置：环境变量名是外部契约，写错会静默失效，必须验 ----------
    # 注意：模块导入时 core.config 的 load_dotenv 已把 .env 的值灌进 os.environ，
    # 而 os.environ 优先级高于 _env_file，所以 _env_file=None **不能**隔离本机 .env。
    # 结果可复现靠两点：init 参数优先级最高；下面这段临时改写 os.environ。
    probe_env = {
        "DEFAULT_LLM_PROVIDER": "openai",
        "DEFAULT_LLM_MODEL": "gpt-4o-mini-probe",
        "CORS_ORIGINS": "http://a.example,http://b.example",
        "HOST": "0.0.0.0",
        "PORT": "8123",
    }
    os.environ.update(probe_env)
    try:
        probe = Settings(_env_file=None)
    finally:
        for key in probe_env:
            os.environ.pop(key, None)

    check("DEFAULT_LLM_PROVIDER 被读取", probe.llm_provider == "openai", probe.llm_provider)
    check(
        "DEFAULT_LLM_MODEL 被读取",
        probe.active_llm_model == "gpt-4o-mini-probe",
        probe.active_llm_model,
    )
    check(
        "CORS_ORIGINS 逗号分隔可解析",
        probe.cors_origins == ["http://a.example", "http://b.example"],
        str(probe.cors_origins),
    )
    check(
        "CORS_ORIGINS 单个 URL 可解析",
        Settings(_env_file=None, cors_origins="http://localhost:5173").cors_origins
        == ["http://localhost:5173"],
    )
    check(
        "CORS_ORIGINS JSON 数组仍兼容",
        Settings(_env_file=None, cors_origins='["http://x.example"]').cors_origins
        == ["http://x.example"],
    )
    check(
        "CORS_ORIGINS 空值 → 空列表",
        Settings(_env_file=None, cors_origins="").cors_origins == [],
    )
    check(
        "DEFAULT_LLM_MODEL 留空时回退到厂商默认",
        Settings(_env_file=None, llm_provider="anthropic", llm_model=None).active_llm_model
        == FALLBACK_MODELS["anthropic"],
        FALLBACK_MODELS["anthropic"],
    )
    check(
        "HOST/PORT 被读取",
        probe.host == "0.0.0.0" and probe.port == 8123,
        f"{probe.host}:{probe.port}",
    )

    # ---------- .env.example 模板本身也要能对上配置 ----------
    # pydantic-settings 对多余变量是 ignore：变量名拼错不会报错，只会静默不生效。
    # 这类问题只有对着模板逐个核对字段才能发现，所以在这里固化成检查。
    template = dotenv_values(BACKEND_DIR / ".env.example")
    valid_env_names = {
        str(field.validation_alias) if isinstance(field.validation_alias, str) else name.upper()
        for name, field in Settings.model_fields.items()
    }
    unknown = sorted(set(template) - valid_env_names)
    check(
        ".env.example 无拼错的变量名",
        not unknown,
        f"未识别：{unknown}" if unknown else f"{len(template)} 个变量全部对应配置字段",
    )
    check(
        ".env.example 符合约定值",
        template.get("DEFAULT_LLM_PROVIDER") == "anthropic"
        and template.get("DEFAULT_LLM_MODEL") == "claude-sonnet-4-20250514"
        and template.get("CORS_ORIGINS") == "http://localhost:5173"
        and template.get("HOST") == "0.0.0.0"
        and template.get("PORT") == "8000"
        and template.get("ANTHROPIC_API_KEY") == ""
        and template.get("OPENAI_API_KEY") == "",
    )
    # 设计文档点名 4096 不够用（PRD模板 §4.2/§4.3、接口文档模板、提示词套件模板）
    check(
        "LLM_MAX_TOKENS 不低于文档下限 8192",
        settings.llm_max_tokens >= 8192 and int(template.get("LLM_MAX_TOKENS", "0")) >= 8192,
        f"配置 {settings.llm_max_tokens} / 模板 {template.get('LLM_MAX_TOKENS')}",
    )

    # ---------- 现场 .env：变量名必须对得上（**真踩过：把别的项目的模板填进来**） ----------
    # pydantic-settings 对多余变量是 **ignore**（实测：塞进 ADMIN_TOKEN / DATA_DIR /
    # TOKEN_BUDGET_MODE 等陌生变量，应用照常启动、一个都不生效）。所以"填错了"不会以报错
    # 暴露，只会以"生成接口全 503、/health 报 llm_configured=false"暴露 ——
    # 那时人往往去查代码，而不是查配置。这两条断言就是把这件事提前点出来。
    live_env = BACKEND_DIR / ".env"
    if live_env.exists():
        live = dotenv_values(live_env)
        # 大小写无关地比：pydantic-settings 默认大小写不敏感，写 `llm_timeout` 也合法
        known = {name.lower() for name in valid_env_names}
        unknown_live = sorted(key for key in live if key.lower() not in known)
        check(
            ".env 里没有本项目不认识的变量名（不认识的名字会被静默忽略）",
            not unknown_live,
            (
                "这些变量不会被读取，确认是不是把别的项目的模板填进来了："
                + "、".join(unknown_live)
            )
            if unknown_live
            else f"{len(live)} 个变量全部对应配置字段",
        )
        # 最要命的一条：Key 缺了应用照样起得来，只有点"生成"时才发现
        key_name = f"{settings.llm_provider.upper()}_API_KEY"
        check(
            f".env 里配了当前 provider（{settings.llm_provider}）的 Key",
            bool((live.get(key_name) or "").strip()),
            f"{key_name} 是空的 —— /health 会报 llm_configured=false，生成接口全部 503",
        )
        # **变量名对不代表值对**。实测踩过：模板里写 `ENVIRONMENT=production`，
        # 而配置模型只认 `local/dev/staging/prod` —— 服务**启动即崩**，容器进入
        # CrashLoop，`docker compose ps` 只显示 `api unhealthy`，日志里只有一行 pydantic 报错。
        # ⚠️ 这条靠的是**开头那句 `get_settings()` 的守卫**（见 `main()`）：配置非法时
        # 本脚本会先打印一句人话再退出 —— 那里才是真正的检查点，此处不再重复构造。
        _ = live  # 上面的变量名检查已经用过它
    else:
        check(
            "backend/.env 不存在 → 跳过现场配置检查",
            True,
            "开发机上没有它很正常（生成接口会 503）；**部署时必须先建**，见 docs/部署.md",
        )

    # 三家 provider 都用假 Key 试构造：验证参数名与当前安装的 langchain 版本匹配。
    # 这类"版本升级后 kwarg 改名"的问题只在构造期暴露，不构造就发现不了。
    for provider in ("deepseek", "openai", "anthropic"):
        try:
            model = build_chat_model(
                Settings(
                    _env_file=None,
                    llm_provider=provider,  # type: ignore[arg-type]
                    deepseek_api_key="sk-dummy",
                    openai_api_key="sk-dummy",
                    anthropic_api_key="sk-dummy",
                )
            )
        except Exception as exc:  # noqa: BLE001 - 自检要报告任何构造失败
            check(f"{provider} 模型构造", False, f"{type(exc).__name__}: {exc}")
        else:
            check(f"{provider} 模型构造", True, type(model).__name__)

    # 缺 Key 必须是 LlmConfigError（路由层据此返回 503），而不是别的异常
    try:
        build_chat_model(
            Settings(_env_file=None, llm_provider="deepseek", deepseek_api_key=None)
        )
    except LlmConfigError as exc:
        check("缺 Key 抛 LlmConfigError", True, str(exc)[:52] + "…")
    except Exception as exc:  # noqa: BLE001
        check("缺 Key 抛 LlmConfigError", False, f"实际抛出 {type(exc).__name__}")
    else:
        check("缺 Key 抛 LlmConfigError", False, "未抛异常")

    # ---------- 输出上限必须真的到得了厂商（HANDOFF §4 坑 #18） ----------
    # 回归防线：`ChatOpenAI(max_tokens=N)` 和 `model_kwargs={"max_tokens": N}` **都会**
    # 被 langchain 改名成 `max_completion_tokens`，而 DeepSeek 静默忽略它 —— 于是
    # `LLM_MAX_TOKENS` 形同虚设、输出在厂商默认上限处被**静默**截断（实测接口文档必现）。
    # 只有 `extra_body` 会原样进请求体。这条断言锁的就是"别再改回那两种写法"。
    # ⚠️ 只构造不调用：请求体里的 extra_body 在 `_get_request_payload()` 里看不到，
    # 所以这里断言的是**构造参数**，行为验证由 `e2e_flow.py` 的截断横幅负责。
    size_probe = build_chat_model(
        Settings(
            _env_file=None,
            llm_provider="deepseek",
            deepseek_api_key="sk-dummy",
            llm_max_tokens=1234,
        )
    )
    check(
        "deepseek 的输出上限走 extra_body（否则配置静默失效）",
        (getattr(size_probe, "extra_body", None) or {}).get("max_tokens") == 1234
        and getattr(size_probe, "max_tokens", "missing") is None,
        f"extra_body={getattr(size_probe, 'extra_body', None)} / "
        f"max_tokens={getattr(size_probe, 'max_tokens', 'missing')}",
    )

    # ---------- 截断判定（纯函数，喂假分片，不调模型） ----------
    check("is_truncated 认得 OpenAI 系的 length", is_truncated("length"))
    check("is_truncated 认得 Anthropic 的 max_tokens（大小写无关）", is_truncated("MAX_TOKENS"))
    check(
        "is_truncated 不把正常结束当截断",
        not is_truncated("stop") and not is_truncated("end_turn") and not is_truncated(None),
    )
    check(
        "finish_reason_of 兼容 OpenAI 与 Anthropic 两种字段名",
        finish_reason_of(SimpleNamespace(response_metadata={"finish_reason": "length"}))
        == "length"
        and finish_reason_of(SimpleNamespace(response_metadata={"stop_reason": "max_tokens"}))
        == "max_tokens",
    )
    check(
        "finish_reason_of 取不到就返回 None（不猜）",
        finish_reason_of(SimpleNamespace(response_metadata={})) is None
        and finish_reason_of(SimpleNamespace(response_metadata=None)) is None
        and finish_reason_of(SimpleNamespace()) is None,
    )
    outcome_probe = StreamOutcome()
    default_not_truncated = not outcome_probe.truncated
    outcome_probe.finish_reason = "length"
    check(
        "StreamOutcome：默认不截断，填入 length 后判定截断",
        default_not_truncated and outcome_probe.truncated,
    )
    # 服务层与路由层必须成对使用它：只降级不报告 = 静默截断（这正是坑 #18 的形态）
    wiring = _outcome_wiring_report()
    check("七个流式路由与两个生成服务都接了 StreamOutcome", wiring is None, wiring or "全部接上")

    # ---------- 分片计划：覆盖必须完整、不重叠、不把示例当穷举（纯解析，不调模型） ----------
    from services.document_plan import build_plan  # noqa: PLC0415

    prd_plan = build_plan("prd")
    check(
        "PRD 刻意不分片（整份实测装得下，分片只会白增调用与拼接风险）",
        not prd_plan.multi_part and len(prd_plan.parts) == 1,
        f"{len(prd_plan.parts)} 片",
    )
    # 标签与 outline 会显示在生成进度里，必须反映**真实**结构：
    # 走技能包却写"15 章 + 2 附录"就是"界面说一套、产物另一套"（正是要避免的那个坑）
    prd_label = prd_plan.parts[0].label
    skill_chapters = [f"- {n}. " for n in range(1, 7)]
    check(
        "PRD 计划标签与 outline 跟着开关走（技能包 → 6 章）",
        "6 章" in prd_label
        and all(prefix in prd_plan.parts[0].outline for prefix in skill_chapters)
        and "使用说明" not in prd_plan.parts[0].outline,
        prd_label,
    )
    fallback_label = build_plan("prd", use_skill=False).parts[0].label
    check(
        "关掉开关时计划标签回到 15 章（回退路径自洽）",
        "15 章" in fallback_label,
        fallback_label,
    )

    api_plan = build_plan("api")
    check("接口文档必须分片（整份实测必被截断）", api_plan.multi_part, f"{len(api_plan.parts)} 片")
    api_rows = _independent_chapter_rows(load_prompt("gen_api"))
    check("接口文档的章节清单能从提示词里独立解析出来", len(api_rows) >= 10, f"{len(api_rows)} 章")
    # 每一章**恰好**落在一个分片里：漏一章 = 文档缺章；重复 = 同一章生成两遍
    miscovered = [
        f"{number} {title}"
        for number, title in api_rows
        if sum(1 for part in api_plan.parts if f"第 {number} 章 {title}" in part.scope) != 1
    ]
    check("每一章恰好在一个分片里（全覆盖、不重叠）", not miscovered, f"有问题的章节：{miscovered}")
    check(
        "分片的三段文本都非空（空的话占位符会以字面量留在提示词里）",
        all(p.scope.strip() and p.spec.strip() and p.outline.strip() for p in api_plan.parts),
    )

    prompts_plan = build_plan("prompts")
    check("提示词套件必须分片（整份实测必被截断）", prompts_plan.multi_part, f"{len(prompts_plan.parts)} 片")
    suite_files = _independent_suite_files(load_prompt("gen_prompts"))
    fixed_files = [name for name in suite_files if "<" not in name and "＜" not in name]
    check("套件的目录结构能独立解析出固定文件", len(fixed_files) >= 5, f"{len(fixed_files)} 个")
    missing_files = [
        name for name in fixed_files if not any(name in part.scope for part in prompts_plan.parts)
    ]
    check("每个固定文件都落进了某个分片", not missing_files, f"漏掉：{missing_files}")
    # ⚠️ 这条守的是一个**真踩过的错**：模板里 `04-step-01/02-<模块名>.md` 后面跟着省略号，
    # 那只是示意图；照抄成"本次只输出这两个文件"会把步骤数**卡死在 2 个**
    # （同一条 PRD 实测能产出 4 个步骤文件）。所以可变组必须说"数量由项目决定"。
    variable_parts = [p for p in prompts_plan.parts if "<" in p.scope or "＜" in p.scope]
    check(
        "可变组（04-step-*）不把示例文件名当成本次范围",
        bool(variable_parts) and all("步骤数量" in p.scope for p in variable_parts),
        f"{len(variable_parts)} 个可变分片",
    )

    # 路由注册（不启动服务）。
    # 用 OpenAPI 文档而不是遍历 app.routes：Starlette 1.6 起 include_router 会保留
    # 嵌套的 router 对象（没有 .path 属性），遍历 app.routes 已不可靠。
    from main import app  # noqa: PLC0415 - 放在末尾，避免 import 顺序影响上面的检查

    # 一律以 OpenAPI 文档为准，不遍历 app.routes：
    # Starlette 1.6 起 include_router 保留嵌套 router 对象，子路由既不通过 .routes
    # 暴露、.path 也不含前缀（前缀在匹配时才拼），按构造遍历既脆弱又容易写错。
    spec = app.openapi()
    paths = set(spec.get("paths", {}))
    expected = {
        "/",
        "/health",
        "/api/v1/health",
        "/api/v1/conversation/continue-stream",
        "/api/v1/conversation/document-plan",
        "/api/v1/conversation/generate-api-docs-stream",
        "/api/v1/conversation/generate-prd-stream",
        "/api/v1/conversation/generate-prd-from-summary-stream",
        "/api/v1/conversation/generate-prompts-stream",
        "/api/v1/conversation/optimize-document-stream",
        "/api/v1/conversation/questions",
        "/api/v1/conversation/start-stream",
        "/api/v1/conversation/sync-summary-from-conversation",
        "/api/v1/conversation/retrieve-api-docs-rag",
        "/api/v1/sessions",
        "/api/v1/sessions/{session_id}",
        "/api/v1/sessions/{session_id}/form",
        "/api/v1/sessions/{session_id}/events",
        "/api/v1/sessions/{session_id}/documents/{kind}",
        "/api/v1/sessions/{session_id}/messages",
        "/api/v1/config",
    }
    missing = expected - paths
    check("路由注册齐全", not missing, "缺失：" + "、".join(sorted(missing)) if missing else f"共 {len(paths)} 条")

    # 旧形状必须已移除 —— 留着就是两套并存的接口面
    #   /prd/*              旧骨架（HANDOFF.md §7）
    #   /conversation/*     旧的"对话消息"中心形状 → 改为"会话"中心
    #   /form/questions     并入 /conversation/questions
    stale_routes = {
        "/api/v1/prd/draft",
        "/api/v1/prd/clarify",
        "/api/v1/conversation/messages",
        "/api/v1/conversation/sessions",
        "/api/v1/form/questions",
        "/api/v1/conversation/stream",  # 被 start-stream / continue-stream 取代
    } & paths
    check("旧形状路由已移除", not stale_routes, f"仍存在 {sorted(stale_routes)}")

    # /health 应与版本化路径同源：比较两份操作定义（忽略 operationId，它按路径生成必然不同）。
    # 真实运行时的内容一致性由 http_check.py 验证。
    root_op = {k: v for k, v in spec["paths"]["/health"]["get"].items() if k != "operationId"}
    api_op = {k: v for k, v in spec["paths"]["/api/v1/health"]["get"].items() if k != "operationId"}
    check("/health 与 /api/v1/health 定义一致", root_op == api_op)

    # 除「服务自述 + 健康检查 + 已实现的接口」外，其余接口都还是占位：
    # 必须声明 501、不能声明 200。写成通用规则而不是列举路径 —— 新增占位接口时自动被覆盖。
    implemented = {
        "/",
        "/health",
        "/api/v1/health",
        "/api/v1/conversation/questions",
        "/api/v1/conversation/document-plan",
        "/api/v1/conversation/start-stream",
        "/api/v1/conversation/continue-stream",
        "/api/v1/conversation/generate-prd-stream",
        "/api/v1/conversation/generate-prd-from-summary-stream",
        "/api/v1/conversation/generate-api-docs-stream",
        "/api/v1/conversation/generate-prompts-stream",
        "/api/v1/conversation/optimize-document-stream",
        "/api/v1/conversation/sync-summary-from-conversation",
        "/api/v1/conversation/retrieve-api-docs-rag",
    }
    placeholders = {
        f"{method.upper()} {path}": operation.get("responses", {})
        for path, methods in spec["paths"].items()
        if path not in implemented
        for method, operation in methods.items()
    }
    check(
        "占位接口只声明 501、不声明 200",
        bool(placeholders)
        and all("501" in codes and "200" not in codes for codes in placeholders.values()),
        f"{len(placeholders)} 个：" + "、".join(sorted(placeholders)),
    )

    # ---------- 技能包：加载器与 PRD 技能 prompt（离线，不调模型） ----------
    from services.document_service import (  # noqa: PLC0415
        SKILL_PRD_ARTIFACTS,
        _load_skill_artifact,
        _skill_system_prompt,
    )

    try:
        instructions = _load_skill_artifact("instructions.md")
    except Exception as exc:  # noqa: BLE001 - 自检要报告任何读取失败
        check("技能包 instructions.md 可读", False, f"{type(exc).__name__}: {exc}")
    else:
        check(
            "技能包 instructions.md 可读且是工作流",
            bool(instructions.strip()) and "校验输入" in instructions,
            f"{len(instructions)} 字符",
        )

    missing_artifacts = []
    for name in SKILL_PRD_ARTIFACTS:
        try:
            _load_skill_artifact(name)
        except Exception as exc:  # noqa: BLE001
            missing_artifacts.append(f"{name}（{type(exc).__name__}）")
    check(
        "技能包声明的四份文件都在",
        not missing_artifacts,
        "缺：" + "、".join(missing_artifacts)
        if missing_artifacts
        else "、".join(SKILL_PRD_ARTIFACTS),
    )

    # 目录穿越必须被拒 —— 不拦的话 `_load_skill_artifact("../../backend/.env")`
    # 会把 API Key 读进 system prompt（这条守的是安全，不是风格）
    try:
        _load_skill_artifact("../../backend/.env")
    except ValueError:
        check("技能加载器拒绝目录穿越（读不到技能目录之外）", True)
    except Exception as exc:  # noqa: BLE001
        check("技能加载器拒绝目录穿越（读不到技能目录之外）", False, f"抛的是 {type(exc).__name__}")
    else:
        check("技能加载器拒绝目录穿越（读不到技能目录之外）", False, "居然读成功了")

    skill_prompt = _skill_system_prompt()
    check(
        "技能 prompt 由四份文件拼成且每份带来源标题",
        all(f"文件：{name}" in skill_prompt for name in SKILL_PRD_ARTIFACTS),
        f"{len(skill_prompt)} 字符",
    )
    chapter_names = [
        "1. 产品概述",
        "2. 功能需求",
        "3. 页面设计",
        "4. 技术规格",
        "5. 非功能需求",
        "6. 项目范围",
    ]
    missing_ch = [name for name in chapter_names if name not in skill_prompt]
    check(
        "技能 prompt 里含技能包的 6 章结构",
        not missing_ch,
        f"缺：{missing_ch}" if missing_ch else "6 章齐",
    )

    # 下游链条靠这两套编号取数：接口文档要按 FR-xx 列接口、套件按 FR/AC 做覆盖矩阵。
    # 技能包少了它们，链条就断在 PRD 这一步（这是把技能包升为默认结构的前提条件）。
    missing_ids = [
        token for token in ("FR-01", "AC-01", "验收标准") if token not in skill_prompt
    ]
    check(
        "技能 prompt 里有 FR/AC 编号与验收标准（下游追溯链的取数依据）",
        not missing_ids,
        f"缺：{missing_ids}" if missing_ids else "FR-01 / AC-01 / 验收标准都在",
    )

    check(
        "PRD 默认走技能包（PRD_USE_SKILL 默认打开）",
        settings.prd_use_skill is True,
        f"prd_use_skill={settings.prd_use_skill}"
        "（默认必须是 True，产品只有技能包这一套结构）",
    )

    # 默认路径解析：不带开关时拿到的 system prompt 必须是技能包那份，
    # 而不是 gen_common + gen_prd 的 15 章模板。**用服务自己的解析方法判**，
    # 不在测试里重写一遍分支逻辑（否则测试和实现会各说各话）。
    from services.document_service import DocumentService  # noqa: PLC0415

    probe = DocumentService(settings)
    values = probe.generation_values(form={})
    default_system, default_human = probe.prd_prompts(values)
    check(
        "PRD 默认 system prompt 来自技能包（6 章，不含 15 章模板）",
        "6. 项目范围" in default_system and "第 15 章" not in default_system,
        f"{len(default_system)} 字符",
    )
    check(
        "PRD 默认 human message 用的是技能包模板（整份生成，不套分片指令）",
        "生成一份 PRD" in default_human and "generate_scope" not in default_human,
        default_human.splitlines()[0] if default_human else "（空）",
    )
    fallback_system, _ = probe.prd_prompts(values, use_skill=False)
    check(
        "显式关掉开关时仍拿得到老的 15 章模板（回退路径没被删）",
        "第 15 章" in fallback_system,
        f"{len(fallback_system)} 字符",
    )

    print()
    if FAILURES:
        print(f"{len(FAILURES)} 项未通过：" + "、".join(FAILURES))
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
