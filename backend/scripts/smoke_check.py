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
        # 前缀匹配：包装调用现在还带 `run_type="..."`（观测用）。
        # 这条断言只关心「有没有把 outcome 交出去」，不该被后面的参数绑死。
        if all(
            s not in source
            for s in (
                "_make_sse_generator(stream, outcome",
                "_make_stage_sse_generator(events, outcome",
            )
        ):
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
        "/api/v1/sessions",
        "/api/v1/sessions/{session_id}",
        "/api/v1/sessions/{session_id}/form",
        "/api/v1/sessions/{session_id}/events",
        "/api/v1/sessions/{session_id}/documents/{kind}",
        "/api/v1/sessions/{session_id}/messages",
        "/api/v1/config",
        # 会话快照存储：**3 条路径**（GET 与 DELETE 共用 `/{id}`），路径参数就叫 `id`
        "/api/session/list",
        "/api/session/{id}",
        "/api/session/save",
        # 文档槽位与版本链：**6 条路径**（挂在快照存储那一族的下一层，`api/documents.py`）
        "/api/session/{session_id}/documents",
        "/api/session/{session_id}/documents/{doc_type}/versions",
        "/api/session/{session_id}/documents/{doc_type}/versions/{version_id}",
        "/api/session/{session_id}/documents/{doc_type}/versions/checkpoint",
        "/api/session/{session_id}/documents/{doc_type}/versions/{version_id}/restore",
        "/api/session/{session_id}/documents/{doc_type}/versions/{version_id}/note",
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
        # 会话快照存储 4 条（已实现，所以必须声明 200 而不是 501）
        "/api/session/list",
        "/api/session/{id}",
        "/api/session/save",
        # 生成任务 3 条（Generation Job，已实现；`api/jobs.py`）。
        # 三条路径都由路由自带 `/api/jobs` 前缀，**不在 `/api/v1` 下**（需求给定）。
        "/api/jobs",
        "/api/jobs/{job_id}",
        "/api/jobs/{job_id}/stream",
        # 文档槽位与版本链 6 条（已实现；`api/documents.py`）。
        # 同样不在 `/api/v1` 下，且挂在 `/api/session/*` 那一族的下一层。
        "/api/session/{session_id}/documents",
        "/api/session/{session_id}/documents/{doc_type}/versions",
        "/api/session/{session_id}/documents/{doc_type}/versions/{version_id}",
        "/api/session/{session_id}/documents/{doc_type}/versions/checkpoint",
        "/api/session/{session_id}/documents/{doc_type}/versions/{version_id}/restore",
        "/api/session/{session_id}/documents/{doc_type}/versions/{version_id}/note",
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
    from services.document_service import _skill_system_prompt  # noqa: PLC0415
    from services.job_models import JOB_ARTIFACTS  # noqa: PLC0415
    from services.skill_loader import (  # noqa: PLC0415
        ROLE_ORDER,
        SkillConfigError,
        compose_prompt_sections,
        load_skill_bundle,
        load_skill_manifest,
        read_artifact_file,
        validate_registry,
        validate_registry_data,
    )

    # ---- 全局注册表 config/skills.yaml ----
    try:
        resolved = validate_registry()
    except Exception as exc:  # noqa: BLE001 - 自检要报告任何校验失败
        check("config/skills.yaml 可加载且通过校验", False, f"{type(exc).__name__}: {exc}")
        resolved = {}
    else:
        check(
            "config/skills.yaml 可加载且通过校验（含 applies_to 交叉校验）",
            True,
            "、".join(f"{key}→{list(value) or '空'}" for key, value in sorted(resolved.items())),
        )

    check(
        "六个产物都绑上了各自的 skill（RAG 下线后不再有留空项）",
        resolved.get("prd") == ("prd-generator",)
        and resolved.get("optimize-prd") == ("prd-generator",)
        and resolved.get("api-docs") == ("api-docs-generator",)
        and resolved.get("optimize-api-docs") == ("api-docs-generator",)
        and resolved.get("prompts") == ("prompts-generator",)
        and resolved.get("optimize-prompts") == ("prompts-generator",),
        "、".join(f"{key}→{list(value)}" for key, value in sorted(resolved.items())),
    )

    # `skills: []` 仍然是**合法状态**（现在没有产物用它，但规则要留住：
    # 将来加产物时不该被逼着立刻写一个 skill）。两件事分开测：
    # 注册表允许空列表、消费侧拿到空 bundle 不炸。
    try:
        validate_registry_data({"bindings": {key: {"skills": []} for key in JOB_ARTIFACTS}})
    except Exception as exc:  # noqa: BLE001
        check("`skills: []` 合法：六个产物全留空也能通过校验", False, f"{type(exc).__name__}: {exc}")
    else:
        check("`skills: []` 合法：六个产物全留空也能通过校验", True, "校验通过")

    from services.skill_loader import SkillBundle, bundle_to_injected_meta  # noqa: PLC0415

    empty_bundle = SkillBundle(artifact="none", skills=(), artifacts=())
    check(
        "空 bundle 在消费侧退化成空（compose → {}、meta → []）",
        compose_prompt_sections(empty_bundle) == {} and bundle_to_injected_meta(empty_bundle) == [],
        "compose={} meta=[]",
    )

    # 注册表写错必须**尽早报错**，而不是静默变成"这个产物没有技能包"
    # （静默的后果是产物缺规范却看不出来）。这里把五种典型写错各喂一遍。
    valid_bindings = {artifact: {"skills": []} for artifact in JOB_ARTIFACTS}
    valid_bindings["prd"] = {"skills": ["prd-generator"]}
    valid_bindings["optimize-prd"] = {"skills": ["prd-generator"]}
    bogus_registries = {
        "引用不存在的 skill": {**valid_bindings, "prd": {"skills": ["no-such-skill"]}},
        "artifact key 拼错": {**valid_bindings, "prd-gen": {"skills": []}},
        "少写一个 artifact 条目": {
            key: value for key, value in valid_bindings.items() if key != "prompts"
        },
        "绑到 applies_to 之外的产物": {**valid_bindings, "api-docs": {"skills": ["prd-generator"]}},
        "缺 skills 键": {**valid_bindings, "prompts": {}},
    }
    bad_registries = []
    for name, bindings in bogus_registries.items():
        try:
            validate_registry_data({"bindings": bindings})
        except SkillConfigError:
            continue
        except Exception as exc:  # noqa: BLE001
            bad_registries.append(f"{name}（抛的是 {type(exc).__name__}）")
        else:
            bad_registries.append(f"{name}（居然通过了）")
    check(
        "注册表写错时配置校验失败（五种错法都要报）",
        not bad_registries,
        "；".join(bad_registries) if bad_registries else "、".join(bogus_registries),
    )

    # ---- skill.yaml 与 bundle ----
    manifest = load_skill_manifest("prd-generator")
    check(
        "skill.yaml 运行时字段完整（id / applies_to / artifacts / enabled / priority）",
        manifest["id"] == "prd-generator"
        and manifest["applies_to"] == ["prd", "optimize-prd"]
        and manifest["enabled"] is True
        and isinstance(manifest["priority"], int)
        and len(manifest["artifacts"]) >= 4,
        "、".join(f"{item['path']}({item['role']})" for item in manifest["artifacts"]),
    )

    bundle = load_skill_bundle("prd")
    bundle_roles = {item.role for item in bundle.artifacts}
    check(
        "load_skill_bundle('prd') 非空，且含 template 与 schema",
        bool(bundle.artifacts) and {"template", "schema"} <= bundle_roles,
        f"{len(bundle.artifacts)} 份，roles={'、'.join(sorted(bundle_roles))}",
    )

    sections = compose_prompt_sections(bundle)
    check(
        "compose_prompt_sections 按 role 分组，顺序 = ROLE_ORDER",
        list(sections) == [role for role in ROLE_ORDER if role in sections] and bool(sections),
        " → ".join(sections) if sections else "（空）",
    )

    # ---- 另外两个技能包（RAG 下线那一篇新增）----
    for artifact, skill_id, must_have in (
        ("api-docs", "api-docs-generator", ("reference", "example")),
        ("prompts", "prompts-generator", ("template", "reference")),
    ):
        bundle_for = load_skill_bundle(artifact)
        roles_for = {item.role for item in bundle_for.artifacts}
        check(
            f"{artifact} 绑定 {skill_id}，且含 {' / '.join(must_have)} 两类文件",
            skill_id in bundle_for.skills and set(must_have) <= roles_for,
            "、".join(f"{item.path}({item.role})" for item in bundle_for.artifacts),
        )
        declared = load_skill_manifest(skill_id).get("constraints", {}).get("max_injected_bytes")
        injected = sum(len(item.content) for item in bundle_for.artifacts)
        check(
            f"{artifact} 的注入体积在 skill.yaml 声明的上限内",
            isinstance(declared, int) and injected <= declared,
            f"{injected} / {declared} 字节（全量注入，不再按关键词筛选）",
        )

    # ---- RAG 检索整条链路必须不存在（少一段就会留下"两套规范来源"）----
    services_dir = Path(__file__).resolve().parents[1] / "services"
    leftovers = sorted(
        path.name
        for path in services_dir.glob("*.py")
        if "rag_service" in path.read_text(encoding="utf-8")
        or "retrieve_api_docs" in path.read_text(encoding="utf-8")
    )
    check(
        "services/ 里没有 RAG 检索残留（模块 / 函数 / 常量名一个都不剩）",
        not leftovers,
        f"仍在：{leftovers}" if leftovers else f"扫了 {len(list(services_dir.glob('*.py')))} 个模块",
    )
    removed_symbols = [
        name
        for name in ("_rag_index", "_rag_search", "_rag_query", "RAG_CORPUS", "RAG_DROP_DIRS")
        if hasattr(ds, name)
    ]
    check(
        "document_service 的检索实现与语料常量已删除",
        not removed_symbols,
        f"仍在：{removed_symbols}" if removed_symbols else "5 个符号都不在了",
    )
    check(
        "检索路由不在 OpenAPI 里（前端也调不到了）",
        "/api/v1/conversation/retrieve-api-docs-rag" not in paths,
        "已移除",
    )

    try:
        instructions = read_artifact_file("prd-generator", "instructions.md")
    except Exception as exc:  # noqa: BLE001 - 自检要报告任何读取失败
        check("技能包 instructions.md 可读", False, f"{type(exc).__name__}: {exc}")
    else:
        check(
            "技能包 instructions.md 可读且是工作流",
            bool(instructions.strip()) and "校验输入" in instructions,
            f"{len(instructions)} 字符",
        )

    # 目录穿越必须被拒 —— 不拦的话读得到 `backend/.env`，
    # API Key 会跟着每一次生成请求发给模型（这条守的是安全，不是风格）
    traversal = []
    for probe in ("../../backend/.env", "references/../../../backend/.env"):
        try:
            read_artifact_file("prd-generator", probe)
        except SkillConfigError:
            continue
        except Exception as exc:  # noqa: BLE001
            traversal.append(f"{probe}（抛的是 {type(exc).__name__}）")
        else:
            traversal.append(f"{probe}（居然读成功了）")
    check(
        "技能加载器拒绝目录穿越（读不到技能目录之外）",
        not traversal,
        "；".join(traversal) if traversal else "两种越界写法都被拒（抛 SkillConfigError）",
    )

    skill_prompt = _skill_system_prompt()
    check(
        "技能 prompt 由声明过的文件拼成且每份带来源标题",
        all(f"文件：{item.path}" in skill_prompt for item in bundle.artifacts),
        f"{len(skill_prompt)} 字符 / {len(bundle.artifacts)} 份",
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

    # ---------- 方案库（SQLite，纯本地文件，不联网） ----------
    # 用**仓库内的临时目录**而不是系统 TEMP：SQLite 要在同目录建 `-wal`/`-shm`，
    # 而受限环境下系统临时目录可能不允许建文件（实测踩到 `unable to open database file`）。
    # 跑完删掉；`.gitignore` 里也备了一条，崩了也不会进仓库。
    plans_dir = BACKEND_DIR / "_tmp_plans_smoke"
    try:
        import shutil  # 局部 import：只为这一段的清理，不值得进文件头

        from services.plan_models import ENTRY_MODES, STAGES, STATUSES, PlanCreate, PlanUpdate
        from services.plan_repository import PlanStorageError, TABLE_NAME  # noqa: F401
        from services.plan_service import PlanService, PlanValidationError

        shutil.rmtree(plans_dir, ignore_errors=True)
        service = PlanService(Settings(sqlite_path=plans_dir / "plans.db", sqlite_busy_timeout_ms=1000))
        service.ensure_ready()
        service.ensure_ready()  # 幂等
        check(
            "方案库建表幂等且文件落在指定路径",
            (plans_dir / "plans.db").exists(),
            f"{TABLE_NAME} @ {plans_dir.name}",
        )

        raw = '{"sessionId":"s1","viewState":"review-prd","form":{"product_name":"群聊周报助手"}}'
        created = service.create(
            PlanCreate(snapshot=raw, entry_mode="prd-shortcut", current_stage="review-prd")
        )
        check(
            "快照按**原文**往返（不重排键、不改空白）",
            service.get(created.id).snapshot == raw,
            "字节一致",
        )
        check(
            "标题留空时从快照里尽力取产品名",
            created.title == "群聊周报助手",
            created.title,
        )
        mapped = service.create(
            PlanCreate(snapshot={"form": {"productName": "中文名"}}, status="active")
        )
        check(
            "映射快照落成 JSON 对象且中文不被转义",
            '"中文名"' in service.get(mapped.id).snapshot,
            service.get(mapped.id).snapshot[:40],
        )
        summaries = service.list(limit=1)
        check(
            "列表按 updated_at 倒序、且**不读快照**（后写入的在前）",
            len(summaries) == 1 and summaries[0].id == mapped.id and not hasattr(summaries[0], "snapshot"),
            f"共 {service.count()} 条",
        )
        before = service.get(created.id).updated_at
        touched = service.update(created.id, PlanUpdate(status="archived"))
        check(
            "更新推进 updated_at、且不动没提到的快照",
            touched.updated_at > before and touched.snapshot == raw and touched.status == "archived",
            f"{before} → {touched.updated_at}",
        )
        check(
            "按入口/状态过滤生效",
            len(service.list(entry_mode="structured")) == 1
            and len(service.list(status="archived")) == 1,
            "structured=1 / archived=1",
        )
        check(
            "删除返回真假、取不存在的记录返回 None",
            service.delete(mapped.id) is True
            and service.delete(mapped.id) is False
            and service.get(mapped.id) is None
            and service.update("missing", PlanUpdate(status="active")) is None,
            f"剩余 {service.count()} 条",
        )
        rejected: list[str] = []
        for label, payload in (
            ("空", ""),
            ("坏 JSON", "{不是 json"),
            ("数组", "[1,2,3]"),
        ):
            try:
                service.create(PlanCreate(snapshot=payload))
            except PlanValidationError:
                rejected.append(label)
        check(
            "快照必须是 JSON **对象**（空/坏 JSON/数组都要被拒）",
            len(rejected) == 3,
            "、".join(rejected),
        )
        try:
            service.create(PlanCreate(snapshot="{}", status="bogus"))  # type: ignore[arg-type]
        except ValueError as exc:
            check("非法枚举在进库前就被挡（pydantic/Service 两层之一）", True, type(exc).__name__)
        else:
            check("非法枚举在进库前就被挡（pydantic/Service 两层之一）", False, "居然通过了")

        import sqlite3

        ddl = service.repository.table_sql()
        missing = [value for value in (*ENTRY_MODES, *STAGES, *STATUSES) if f"'{value}'" not in ddl]
        check(
            "数据库 CHECK 与 plan_models 的枚举取值完全一致（防两处漂移）",
            not missing,
            f"覆盖 {len(ENTRY_MODES) + len(STAGES) + len(STATUSES)} 个取值" if not missing else f"缺 {missing}",
        )
        try:
            service.repository.insert(
                {
                    "id": "raw",
                    "title": "t",
                    "entry_mode": "structured",
                    "current_stage": "form",
                    "status": "bogus",
                    "snapshot": "{}",
                    "created_at": "x",
                    "updated_at": "x",
                }
            )
        except sqlite3.IntegrityError:
            check("绕过 Service 直写仓储时，数据库 CHECK 仍拦得住", True, "IntegrityError")
        else:
            check("绕过 Service 直写仓储时，数据库 CHECK 仍拦得住", False, "CHECK 没生效")

        live = sqlite3.connect(str(plans_dir / "plans.db"))
        version = live.execute("PRAGMA user_version").fetchone()[0]
        journal = live.execute("PRAGMA journal_mode").fetchone()[0]
        live.close()
        check(
            "库版本号与 WAL 模式已设置",
            version == 1 and str(journal).lower() == "wal",
            f"user_version={version}, journal_mode={journal}",
        )
        check(
            "默认库路径落在 backend/ 下（不跟随 cwd）",
            Settings(_env_file=None).plans_db_path.parent == BACKEND_DIR,
            Settings(_env_file=None).plans_db_path.name,
        )
    finally:
        import shutil as _shutil

        _shutil.rmtree(plans_dir, ignore_errors=True)

    # ---------- 会话业务层（同一张表，纯本地，不联网） ----------
    sessions_dir = BACKEND_DIR / "_tmp_plans_smoke"
    try:
        import shutil

        from services.session_models import SessionLoad
        from services.session_service import (
            SessionNotFound,
            SessionService,
            downgrade_view_state,
            parse_prd_title,
        )

        shutil.rmtree(sessions_dir, ignore_errors=True)
        sessions = SessionService(Settings(sqlite_path=sessions_dir / "sessions.db"))
        sessions.ensure_ready()

        def snap(**over: object) -> dict:
            base: dict = {
                "sessionId": "s1",
                "formVersion": "1.2",
                "form": {"product_name": "表单里的名字"},
                "messages": [{"role": "user", "content": "你好"}],
                "roundIndex": 1,
                "documents": {},
                "viewState": "review-prd",
                "updatedAt": "2026-09-28T00:00:00.000+00:00",
            }
            base.update(over)
            return base

        made = sessions.save_session(
            snap(documents={"prd": {"content": "# 群聊周报助手\n\n## 1. 产品概述\n"}})
        )
        check(
            "save_session 无 id → 新建，并用 PRD 一级标题命名",
            made.created is True and len(made.id) == 32 and made.title == "群聊周报助手",
            f"id={made.id[:8]} title={made.title}",
        )
        by_name = sessions.save_session(
            snap(documents={"prd": {"content": "## 2. 功能需求\n\n产品名称：次级来源\n"}})
        )
        check(
            "标题优先级：`##` 不算一级标题 → 退到「产品名称：xxx」",
            by_name.title == "次级来源",
            by_name.title,
        )
        no_title = sessions.save_session(snap(form={}, documents={}))
        check(
            "标题都取不到（正文与表单都没有）→ 用记录 id（不留空标题）",
            no_title.title == no_title.id,
            no_title.title[:8],
        )
        by_form = sessions.save_session(snap(documents={}))
        check(
            "没有 PRD 正文时，标题退到表单里的产品名（新建方案在开始对话时就落库）",
            by_form.title == "表单里的名字",
            by_form.title,
        )
        by_body = sessions.save_session(
            snap(documents={"prd": {"content": "# 正文标题\n"}})
        )
        check(
            "有 PRD 正文时，正文一级标题优先于表单产品名",
            by_body.title == "正文标题",
            by_body.title,
        )
        again = sessions.save_session(
            snap(documents={"prd": {"content": "# 换了个名字\n"}}, viewState="done"),
            session_id=made.id,
        )
        check(
            "save_session 有 id → 更新，且**不改标题**（正文换了标题也不改）",
            again.created is False and again.id == made.id and again.title == "群聊周报助手",
            f"title={again.title}",
        )
        sessions.save_session(snap(viewState="generating-api-docs"), session_id=made.id)
        check(
            "写入前降级：库里不出现 generating-*",
            "generating" not in str(sessions.plans.get(made.id).snapshot)
            and sessions.plans.get(made.id).current_stage == "review-api-docs",
            "库内 viewState=review-api-docs",
        )
        sessions.plans.repository.insert(
            {
                "id": "legacy",
                "title": "旧记录",
                "entry_mode": "structured",
                "current_stage": "generating-prompts",
                "status": "draft",
                "snapshot": json.dumps(snap(viewState="generating-prompts"), ensure_ascii=False),
                "created_at": "2026-09-27T00:00:00.000+00:00",
                "updated_at": "2026-09-27T00:00:00.000+00:00",
            }
        )
        loaded = sessions.get_session("legacy")
        check(
            "get_session 读到 generating-* 时先降级，并报出被打断的产物",
            isinstance(loaded, SessionLoad)
            and json.loads(loaded.session_data)["viewState"] == "review-prompts"
            and loaded.downgraded_from == "generating-prompts"
            and loaded.interrupted_kind == "prompts",
            f"{loaded.downgraded_from} → review-prompts（kind={loaded.interrupted_kind}）",
        )
        check(
            "降级**不回写**（读操作不该改数据）",
            json.loads(sessions.plans.get("legacy").snapshot)["viewState"] == "generating-prompts",
            "库内仍是 generating-prompts",
        )
        # ⚠️ 这份快照**必须带上 prd 正文**：版本层此刻已经有一个 current（上面
        # `again` 那次保存写进去的 `# 换了个名字`），镜像里没有的话会被 02 篇的
        # "回填"补上 —— 那是刻意的（下面单独有一条断言），但这条断言要测的是
        # "不需要降级时不重排键、不改空白"，别让它去测一件与回填无关的事。
        raw = json.dumps(
            snap(viewState="review-prd", documents={"prd": {"content": "# 换了个名字\n"}}),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        sessions.save_session(raw, session_id=made.id)
        check(
            "无需降级时快照**字节一致**（不重排键、不改空白）",
            sessions.get_session(made.id).session_data == raw,
            "原样往返",
        )
        # 02 篇：版本层有、镜像**没有** → 回填（正是"Job 完成后镜像那一步没写成"的修复路径）。
        # ⚠️ 这是"字节一致"那条性质的**唯一例外**，而且是刻意的：快照里那一份正文根本
        # 不存在，回填补的是缺口而不是覆盖用户的东西（两边都有但不一致时**保留镜像**）。
        emptied = json.dumps(snap(viewState="review-prd", documents={}), ensure_ascii=False, separators=(",", ":"))
        sessions.save_session(emptied, session_id=made.id)
        backfilled = sessions.get_session(made.id).session_data
        check(
            "镜像缺正文时从版本层回填，并写回库",
            json.loads(backfilled)["documents"]["prd"]["content"] == "# 换了个名字\n"
            and json.loads(sessions.plans.get(made.id).snapshot)["documents"]["prd"]["content"]
            == "# 换了个名字\n",
            "回填到镜像与库",
        )
        rows = sessions.list_sessions()
        check(
            "list_sessions 按 updated_at 倒序且**不含 session_data**",
            len(rows) >= 2 and not hasattr(rows[0], "session_data") and rows[0].id == made.id,
            f"共 {len(rows)} 条，首条={rows[0].title}",
        )
        sessions.delete_session("legacy")
        raised = 0
        for action in (
            lambda: sessions.get_session("legacy"),
            lambda: sessions.delete_session("legacy"),
            lambda: sessions.save_session(snap(), session_id="legacy"),
        ):
            try:
                action()
            except SessionNotFound:
                raised += 1
        check(
            "delete_session 后 get/delete/save 三个操作都抛 SessionNotFound（HTTP → 404）",
            raised == 3,
            f"{raised}/3",
        )
        check(
            "纯函数：降级表与标题兜底",
            downgrade_view_state("review-prd") == ("review-prd", None)
            and downgrade_view_state("generating-prd") == ("review-prd", "prd")
            and parse_prd_title("{}", "fallback") == "fallback",
            "generating-prd → review-prd / fallback",
        )
    finally:
        import shutil as _shutil

        _shutil.rmtree(sessions_dir, ignore_errors=True)

    # ---------- 会话存储 HTTP 接口（4 条，TestClient 真打，临时库） ----------
    try:
        import shutil

        from fastapi.testclient import TestClient

        from main import create_app

        api_dir = BACKEND_DIR / "_tmp_plans_smoke"
        shutil.rmtree(api_dir, ignore_errors=True)
        # 记一下默认库的条数：接口**必须**用注入的临时库（依赖注入若绕过 app.state，
        # 就会写进 backend/harnessprd.db —— 测试之间互相污染，且极难发现）
        from services.plan_service import PlanService as _PlanService

        try:
            default_before: int | None = _PlanService(Settings(_env_file=None)).count()
        except Exception:  # noqa: BLE001 - 默认库读不了不影响本段结论
            default_before = None

        session_app = create_app(
            Settings(_env_file=None, sqlite_path=api_dir / "api.db", sqlite_busy_timeout_ms=1000)
        )
        with TestClient(session_app) as client:
            empty = client.get("/api/session/list")
            check(
                "GET /api/session/list 回 {items: []} 信封",
                empty.status_code == 200 and empty.json() == {"items": []},
                f"{empty.status_code} {empty.json()}",
            )
            made = client.post(
                "/api/session/save",
                json={
                    "session_data": {
                        "sessionId": "s1",
                        "viewState": "review-prd",
                        "documents": {"prd": {"content": "# 群聊周报助手\n"}},
                    }
                },
            )
            made_body = made.json()
            plan_id = made_body.get("id", "")
            check(
                "POST /api/session/save 新建 → 统一回 {id, title}",
                made.status_code == 200
                and set(made_body) == {"id", "title"}
                and made_body.get("title") == "群聊周报助手",
                f"{made_body}",
            )
            items = client.get("/api/session/list").json().get("items", [])
            check(
                "列表项只有摘要字段（无 session_data）",
                len(items) == 1 and "session_data" not in items[0],
                "、".join(sorted(items[0])) if items else "空",
            )
            detail = client.get(f"/api/session/{plan_id}")
            detail_body = detail.json()
            check(
                "GET /api/session/{id} 带完整快照与降级信息",
                detail.status_code == 200
                and bool(detail_body.get("session_data"))
                and "downgraded_from" in detail_body
                and "interrupted_kind" in detail_body,
                f"{detail.status_code} 字段 {len(detail_body)} 个",
            )
            again = client.post(
                "/api/session/save",
                json={
                    "id": plan_id,
                    "session_data": {
                        "sessionId": "s1",
                        "viewState": "generating-prd",
                        "documents": {"prd": {"content": "# 换了个名字\n"}},
                    },
                },
            )
            check(
                "POST /save 带 id → 更新同一条、标题不变、写入前已降级",
                again.json().get("id") == plan_id
                and again.json().get("title") == "群聊周报助手"
                and '"viewState":"review-prd"' in client.get(f"/api/session/{plan_id}").json()["session_data"],
                f"{again.json()}",
            )
            check(
                "查询与删除不存在的 id 都是 404",
                client.get("/api/session/nope").status_code == 404
                and client.delete("/api/session/nope").status_code == 404,
                "GET/DELETE → 404",
            )
            deleted = client.delete(f"/api/session/{plan_id}")
            check(
                "DELETE 存在 → {ok: true}，且之后列表为空",
                deleted.status_code == 200
                and deleted.json() == {"ok": True}
                and client.get("/api/session/list").json() == {"items": []},
                f"{deleted.status_code} {deleted.json()}",
            )
            check(
                "请求体类型不合法 → 422（Pydantic 拦在业务层之前）",
                client.post("/api/session/save", json={"session_data": 123}).status_code == 422,
                "session_data=123",
            )
        if default_before is not None:
            check(
                "接口用的是注入的库，没写进默认库（依赖注入没绕过 app.state）",
                _PlanService(Settings(_env_file=None)).count() == default_before,
                f"默认库仍为 {default_before} 条",
            )
    finally:
        import shutil as _shutil2

        _shutil2.rmtree(BACKEND_DIR / "_tmp_plans_smoke", ignore_errors=True)

    # ---------- 文档槽位与版本链（两张表 + service + 6 个 HTTP 接口） ----------
    #
    # 这是「**历史版本只追加、正文不可原地修改**」这条原则的守卫。分三层：
    #
    #   ① 建表：两张表真的存在、`source_kind` 的 CHECK 覆盖全部枚举取值、两条 UNIQUE 都在；
    #   ② service：三种**升号**（generate / checkpoint / restore）与两种**不升号**
    #      （optimize / auto_save）各自的行为，加上五个边界；
    #   ③ HTTP：6 条路径的响应形状与 400 / 404。
    #
    # ⚠️ **全部走临时库**，不碰 `backend/harnessprd.db`（否则测试之间互相污染）。
    try:
        import shutil as _shutil_dv

        from services.document_version_repository import (
            DOCUMENTS_TABLE,
            DOC_TYPES,
            VERSIONS_TABLE,
            VERSION_SOURCE_KINDS,
        )
        from services.document_version_service import (
            DocumentVersionService,
        )
        from services.document_version_service import (
            DocumentNotFound as _DocVerNotFound,
        )
        from services.document_version_service import (
            InvalidDocumentRequest as _InvalidDocRequest,
        )
        from services.plan_models import PlanCreate
        from services.plan_service import PlanService as _PlanSvc
        from services.quality_gate import run_quality_gate

        _dv_dir = BACKEND_DIR / "_tmp_docver_smoke"
        _shutil_dv.rmtree(_dv_dir, ignore_errors=True)
        _dv_settings = Settings(
            _env_file=None, sqlite_path=_dv_dir / "dv.db", sqlite_busy_timeout_ms=1000
        )
        _dv_plans = _PlanSvc(_dv_settings)
        _dv_plans.ensure_ready()
        _dv = DocumentVersionService(_dv_settings, plans=_dv_plans)
        _dv.ensure_ready()

        # ---- ① 建表
        _doc_sql = _dv.repository.table_sql(DOCUMENTS_TABLE)
        _ver_sql = _dv.repository.table_sql(VERSIONS_TABLE)
        check(
            "文档版本表：documents 与 document_versions 两张表都建出来了",
            bool(_doc_sql) and bool(_ver_sql),
            f"{DOCUMENTS_TABLE}={bool(_doc_sql)} {VERSIONS_TABLE}={bool(_ver_sql)}",
        )
        # 用**枚举**去查 CHECK 文本，而不是反过来 —— 这样新增取值而忘了改建表语句会被抓住
        # （与 `job_repository` 的 CHECK↔Literal 断言同一个思路；只是这里的枚举就在仓储里，
        #  所以是"建表语句没跟上枚举"而不是"两处枚举不一致"）。
        _missing_kinds = [k for k in VERSION_SOURCE_KINDS if f"'{k}'" not in _ver_sql]
        check(
            "文档版本表：source_kind 的 CHECK 覆盖全部 6 个枚举取值",
            not _missing_kinds,
            f"缺 {_missing_kinds}" if _missing_kinds else "、".join(VERSION_SOURCE_KINDS),
        )
        _missing_types = [t for t in DOC_TYPES if f"'{t}'" not in _doc_sql]
        check(
            "文档版本表：doc_type 的 CHECK 覆盖三种槽位（prd / api-docs / prompts）",
            not _missing_types,
            f"缺 {_missing_types}" if _missing_types else "、".join(DOC_TYPES),
        )
        _flat_doc = _doc_sql.replace("\n", " ")
        _flat_ver = _ver_sql.replace("\n", " ")
        check(
            "文档版本表：documents 的 UNIQUE(session_id, doc_type) 在（一个槽位只一行）",
            "UNIQUE(session_id, doc_type)" in _flat_doc,
            "唯一约束",
        )
        check(
            "文档版本表：document_versions 的 UNIQUE(document_id, version_no) 在（兜底重复插入）",
            "UNIQUE(document_id, version_no)" in _flat_ver,
            "唯一约束",
        )
        with _dv.repository.connection() as _conn:
            _index_names = {
                row[0]
                for row in _conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'index'"
                ).fetchall()
            }
        check(
            "文档版本表：两条需求点名的索引都在",
            {
                f"idx_{DOCUMENTS_TABLE}_session",
                f"idx_{VERSIONS_TABLE}_document",
            }
            <= _index_names,
            "、".join(sorted(n for n in _index_names if n.startswith("idx_doc"))),
        )

        # ---- ② service：槽位
        _plan = _dv_plans.create(
            PlanCreate(title="dv", entry_mode="structured", current_stage="form", snapshot="{}")
        )
        _sid = _plan.id
        _slots = _dv.get_documents_by_session(_sid)
        check(
            "槽位：新 Session 返回三种 doc_type，current 均为空",
            [s.doc_type for s in _slots] == ["prd", "api-docs", "prompts"]
            and all(s.current_version_id is None and s.current_version_no is None for s in _slots),
            "、".join(f"{s.doc_type}={s.current_version_no}" for s in _slots),
        )
        check(
            "槽位：尚无版本时 list_versions 回 [] 而不是 404",
            _dv.list_versions(_sid, "prd") == [] and _dv.get_current_version(_sid, "prd") is None,
            "空",
        )

        # ---- ② service：首版 → 就地更新（不升号）
        _v1 = _dv.create_initial_version(_sid, "prd", "# PRD v1", source_job_id="job-1")
        check(
            "首版：create_initial_version → version_no=1、parent 为空、成为 current",
            _v1.version_no == 1
            and _v1.parent_version_id is None
            and _dv.get_current_version(_sid, "prd").id == _v1.id,
            f"v{_v1.version_no}",
        )
        _v1b = _dv.update_current_content(
            _sid, "prd", "# PRD v1 改过了", metadata={"run_summary": {"request_id": "req_x"}}
        )
        check(
            "不升号：update_current_content 后 version_no 仍为 1、正文已变",
            _v1b.version_no == 1 and _v1b.id == _v1.id and "改过了" in _v1b.content,
            f"v{_v1b.version_no}",
        )
        check(
            "不升号：metadata 浅合并（run_summary 写进去了）",
            _v1b.metadata().get("run_summary") == {"request_id": "req_x"},
            str(_v1b.metadata()),
        )

        # ---- ② service：sync_from_job（整份生成升号；优化不升号且保留 review）
        _api1 = _dv.sync_from_job(
            session_id=_sid, artifact="api-docs", content="# API v1", job_id="job-a1"
        )
        _api2 = _dv.sync_from_job(
            session_id=_sid, artifact="api-docs", content="# API v2", job_id="job-a2"
        )
        _api_versions = {v.version_no: v for v in _dv.list_versions(_sid, "api-docs")}
        check(
            "升号：第二次 sync_from_job（整份生成）→ version_no=2，且 v1 正文不变",
            _api1.version_no == 1
            and _api2.version_no == 2
            and _api_versions[1].content == "# API v1"
            and _api2.parent_version_id == _api1.id,
            f"v{_api1.version_no}→v{_api2.version_no}，v1={_api_versions[1].content!r}",
        )
        _prd2 = _dv.sync_from_job(
            session_id=_sid,
            artifact="prd",
            content="# PRD v2",
            job_id="job-p2",
            review={"passed": True, "summary": "ok"},
        )
        _opt = _dv.sync_from_job(
            session_id=_sid,
            artifact="optimize-prd",
            content="# PRD v2 优化了一节",
            job_id="job-o1",
            review={"passed": False, "summary": "不该写入"},
        )
        check(
            "升号：PRD 整份生成 → v2，并写入本次 Review Agent 结果",
            _prd2.version_no == 2
            and _prd2.metadata().get("review") == {"passed": True, "summary": "ok"},
            f"v{_prd2.version_no}",
        )
        check(
            "不升号：优化 Job **不升号**（仍 v2）且保留该版生成时的 review",
            _opt.version_no == 2
            and _opt.id == _prd2.id
            and _opt.metadata().get("review") == {"passed": True, "summary": "ok"},
            f"v{_opt.version_no} review={_opt.metadata().get('review')}",
        )
        check(
            "不升号：优化只改 current 正文、**不改 source_kind**（仍是它出生时的 generate）",
            "优化了一节" in _opt.content and _opt.source_kind.value == "generate",
            f"source_kind={_opt.source_kind.value}",
        )

        # ---- ② service：checkpoint
        _cp = _dv.checkpoint(_sid, "prd")
        check(
            "checkpoint：在已有基础上 +1（v3）、parent 指向旧 current、正文沿用 current",
            _cp.version_no == 3 and _cp.parent_version_id == _prd2.id and _cp.content == _opt.content,
            f"v{_cp.version_no}",
        )
        check(
            "checkpoint：**不继承** metadata（新版无 review、也不自动写备注）",
            _cp.metadata() == {},
            str(_cp.metadata()),
        )
        _cp_front = _dv.checkpoint(_sid, "prd", "# 前端编辑器全文")
        check(
            "checkpoint：请求体带 content 时用它的正文（前端手改还没同步的场景）",
            _cp_front.version_no == 4 and _cp_front.content.startswith("# 前端编辑器全文"),
            f"v{_cp_front.version_no}",
        )

        # ---- ② service：restore（current=v4 时恢复 v1）
        _before = {v.version_no: v for v in _dv.list_versions(_sid, "prd")}
        _restored = _dv.restore_version(_sid, "prd", _before[1].id)
        _after = {v.version_no: v for v in _dv.list_versions(_sid, "prd")}
        check(
            "restore：current=v4 时恢复 v1 → 产生 v5，正文与 v1 逐字相同、是 current",
            _restored.version_no == 5
            and _restored.content == _before[1].content
            and _restored.source_kind.value == "restore"
            and _dv.get_current_version(_sid, "prd").id == _restored.id,
            f"v{_restored.version_no}",
        )
        check(
            "restore：**不删不改历史行** —— v1…v4 仍在，且 v1 / v4 正文一字未动",
            sorted(_after) == [1, 2, 3, 4, 5]
            and _after[1].content == _before[1].content
            and _after[4].content == _before[4].content,
            f"链={sorted(_after)}",
        )
        check(
            "restore：metadata 记 restored_from_*，且**丢掉**历史版的 change_note",
            _restored.metadata().get("restored_from_version_id") == _before[1].id
            and _restored.metadata().get("restored_from_version_no") == 1
            and "change_note" not in _restored.metadata(),
            str({k: v for k, v in _restored.metadata().items() if k != "review"}),
        )

        # ---- ② service：备注（不升号、不改正文）
        _noted = _dv.update_version_note(_sid, "prd", _before[1].id, "定稿前备份")
        _noted_detail = _dv.get_version(_sid, "prd", _before[1].id)
        check(
            "备注：写入后列表与详情都能看到 change_note，且正文与 version_no 都没变",
            _noted.change_note() == "定稿前备份"
            and _noted_detail.change_note() == "定稿前备份"
            and _noted_detail.content == _before[1].content
            and _noted_detail.version_no == 1,
            str(_noted.change_note()),
        )
        check(
            "备注：空串 / None 都能清空（键被删掉，而不是留一个空串）",
            _dv.update_version_note(_sid, "prd", _before[1].id, "").change_note() is None
            and "change_note" not in _dv.get_version(_sid, "prd", _before[1].id).metadata()
            and _dv.update_version_note(_sid, "prd", _before[1].id, None).change_note() is None,
            "已清空",
        )

        # ---- ② service：边界
        def _raises(fn, exc_type) -> bool:  # noqa: ANN001 - 局部小工具
            try:
                fn()
            except exc_type:
                return True
            except Exception:  # noqa: BLE001 - 抛了别的类型也算没通过
                return False
            return False

        check(
            "边界：非法 doc_type → 400 类异常（需求点名要 400 而不是 422）",
            _raises(lambda: _dv.list_versions(_sid, "nope"), _InvalidDocRequest),
            "InvalidDocumentRequest",
        )
        check(
            "边界：session 不存在 → 404 类异常",
            _raises(lambda: _dv.get_documents_by_session("no-such"), _DocVerNotFound),
            "DocumentNotFound",
        )
        check(
            "边界：restore 的目标就是 current → 400 类异常",
            _raises(
                lambda: _dv.restore_version(_sid, "prd", _restored.id), _InvalidDocRequest
            ),
            "InvalidDocumentRequest",
        )
        check(
            "边界：checkpoint 无 current 且请求体无 content → 400 类异常（无内容可保存）",
            _raises(lambda: _dv.checkpoint(_sid, "prompts"), _InvalidDocRequest),
            "InvalidDocumentRequest",
        )
        check(
            "边界：版本不属于该 document → 404 类异常（不区分「不存在」与「不是你的」）",
            _raises(lambda: _dv.get_version(_sid, "prd", _api1.id), _DocVerNotFound),
            "DocumentNotFound",
        )
        check(
            "边界：不同 doc_type 的版本链互不干扰",
            len(_dv.list_versions(_sid, "api-docs")) == 2
            and len(_dv.list_versions(_sid, "prd")) == 5,
            "api-docs=2 prd=5",
        )

        # ---- ④ 04 篇：结构校验（quality_gate）与 `review` 的**相反**语义
        # review 是"没有值才写"（优化不带审稿就不许覆盖），gate 是"每次都覆盖"
        # （它描述的是眼前这份正文，留着上一版的结论就是撒谎）。
        # ⚠️ 用**新开一个 Session**：这一段会把 current 正文改掉（`optimize-prd` 就地更新），
        # 挂在上面那条链上会打乱 `_sid` 的版本号/正文断言（实测踩过一次）。
        _gate_sid = _dv_plans.create(
            PlanCreate(title="gate", entry_mode="structured", current_stage="form", snapshot="{}")
        ).id
        _gate_pass_sample = (BACKEND_DIR / "validation_out" / "prd_skill.txt").read_text(
            encoding="utf-8"
        )
        _gated = _dv.sync_from_job(
            session_id=_gate_sid,
            artifact="prd",
            content=_gate_pass_sample,
            job_id="job-g1",
            review={"passed": True, "summary": "ok"},
            quality_gate=run_quality_gate("prd", _gate_pass_sample),
        )
        _regated = _dv.sync_from_job(
            session_id=_gate_sid,
            artifact="optimize-prd",
            content="# 只剩一个标题",
            job_id="job-g2",
            quality_gate=run_quality_gate("prd", "# 只剩一个标题"),
        )
        check(
            "结构校验：落库的 gate 与**这一版正文**一致（真实 PRD 留档 = 100 分全过）",
            _gated.metadata().get("quality_gate", {}).get("passed") is True
            and _gated.metadata().get("quality_gate", {}).get("score") == 100,
            f"passed={_gated.metadata().get('quality_gate', {}).get('passed')}"
            f" score={_gated.metadata().get('quality_gate', {}).get('score')}",
        )
        check(
            "结构校验：不升号地**覆盖** quality_gate（第二次替换第一次，与 review 相反）",
            _regated.version_no == 1
            and _regated.id == _gated.id
            and _regated.metadata().get("quality_gate", {}).get("passed") is False,
            f"v{_regated.version_no}"
            f" passed={_regated.metadata().get('quality_gate', {}).get('passed')}",
        )
        check(
            "结构校验：覆盖 gate 时**不动**同版的 review（两个键互不干扰）",
            _regated.metadata().get("review") == {"passed": True, "summary": "ok"},
            json.dumps(_regated.metadata().get("review"), ensure_ascii=False),
        )

        # ---- ③ HTTP：6 条接口（TestClient，仍是临时库）
        from fastapi.testclient import TestClient as _DocClient

        from main import create_app as _create_doc_app

        _http_dir = BACKEND_DIR / "_tmp_docver_http_smoke"
        _shutil_dv.rmtree(_http_dir, ignore_errors=True)
        _doc_app = _create_doc_app(
            Settings(
                _env_file=None,
                sqlite_path=_http_dir / "api.db",
                sqlite_busy_timeout_ms=1000,
            )
        )
        with _DocClient(_doc_app) as _dc:
            _made = _dc.post(
                "/api/session/save",
                json={"session_data": {"sessionId": "h1", "viewState": "form"}},
            ).json()
            _hsid = _made["id"]
            _hbase = f"/api/session/{_hsid}/documents"

            _h1 = _dc.get(_hbase)
            _h1_body = _h1.json()
            check(
                "HTTP 1：GET /documents → 200，固定三项、current 与 updated_at 均为 null",
                _h1.status_code == 200
                and [i["doc_type"] for i in _h1_body["items"]] == ["prd", "api-docs", "prompts"]
                and all(
                    i["current_version_id"] is None
                    and i["current_version_no"] is None
                    and i["updated_at"] is None
                    and i["document_id"]
                    for i in _h1_body["items"]
                ),
                f"{_h1.status_code}",
            )
            _h_prd_doc = _h1_body["items"][0]["document_id"]

            _h2 = _dc.get(f"{_hbase}/prd/versions")
            check(
                "HTTP 2：尚无版本时 GET versions → 200 且 versions=[]、current_version_id=null",
                _h2.status_code == 200
                and _h2.json()["versions"] == []
                and _h2.json()["current_version_id"] is None,
                "空列表",
            )

            check(
                "HTTP 错误：非法 doc_type → 400（不是 422）",
                _dc.get(f"{_hbase}/nope/versions").status_code == 400
                and _dc.post(f"{_hbase}/nope/versions/checkpoint", json={}).status_code == 400,
                "GET / POST 都是 400",
            )
            check(
                "HTTP 错误：session 不存在 → 404",
                _dc.get("/api/session/no-such/documents").status_code == 404,
                "404",
            )
            _h_cp_bad = _dc.post(f"{_hbase}/prd/versions/checkpoint", json={})
            check(
                "HTTP 错误：checkpoint 无 current 且无 content → 400，detail 含「无内容可保存」",
                _h_cp_bad.status_code == 400 and "无内容可保存" in _h_cp_bad.json()["detail"],
                f"{_h_cp_bad.status_code} {_h_cp_bad.json()['detail'][:24]}",
            )
            check(
                "HTTP 4：checkpoint **完全不带请求体**也走业务层（400 而不是 422）",
                _dc.post(f"{_hbase}/prd/versions/checkpoint").status_code == 400,
                "请求体整个可选",
            )

            _h_cp = _dc.post(
                f"{_hbase}/prd/versions/checkpoint", json={"content": "# PRD v1\n\n第一版"}
            )
            _h_cp_body = _h_cp.json()
            check(
                "HTTP 4：checkpoint 带 content → 200，回 {version_id, version_no, document_id}",
                _h_cp.status_code == 200
                and set(_h_cp_body) == {"version_id", "version_no", "document_id"}
                and _h_cp_body["version_no"] == 1
                and _h_cp_body["document_id"] == _h_prd_doc,
                str(_h_cp_body),
            )
            _h_v1 = _h_cp_body["version_id"]

            _h1b = _dc.get(_hbase).json()["items"][0]
            check(
                "HTTP 1：有版本后 current_version_id / current_version_no / updated_at 都填上",
                _h1b["current_version_id"] == _h_v1
                and _h1b["current_version_no"] == 1
                and bool(_h1b["updated_at"]),
                f"v{_h1b['current_version_no']}",
            )

            _h_lst = _dc.get(f"{_hbase}/prd/versions").json()
            _h_item = _h_lst["versions"][0]
            check(
                "HTTP 2：列表信封是 {doc_type, document_id, current_version_id, versions[]}",
                set(_h_lst) == {"doc_type", "document_id", "current_version_id", "versions"}
                and _h_lst["doc_type"] == "prd"
                and _h_lst["document_id"] == _h_prd_doc,
                "、".join(sorted(_h_lst)),
            )
            check(
                "HTTP 2：列表项含 content_preview / is_current，**不含全文**",
                set(_h_item)
                == {"id", "version_no", "source_kind", "created_at", "content_preview", "is_current"}
                and _h_item["is_current"] is True
                and _h_item["source_kind"] == "checkpoint"
                and _h_item["content_preview"].startswith("# PRD v1"),
                "、".join(sorted(_h_item)),
            )
            check(
                "HTTP 2：**无备注时 change_note 这个键不出现**（不是 null）",
                "change_note" not in _h_item,
                "省略",
            )
            _h_note = _dc.patch(
                f"{_hbase}/prd/versions/{_h_v1}/note", json={"change_note": "定稿前备份"}
            )
            check(
                "HTTP 6：PATCH note → 200，回 {version_id, version_no, change_note}",
                _h_note.status_code == 200
                and _h_note.json()
                == {"version_id": _h_v1, "version_no": 1, "change_note": "定稿前备份"},
                str(_h_note.json()),
            )
            check(
                "HTTP 6：写了备注后列表项就带上 change_note",
                _dc.get(f"{_hbase}/prd/versions").json()["versions"][0].get("change_note")
                == "定稿前备份",
                "有备注",
            )

            _h_det = _dc.get(f"{_hbase}/prd/versions/{_h_v1}").json()
            check(
                "HTTP 3：单版详情含全文与 metadata，身份字段齐全",
                _h_det["id"] == _h_v1
                and _h_det["version_no"] == 1
                and _h_det["content"] == "# PRD v1\n\n第一版"
                and _h_det["parent_version_id"] is None
                and _h_det["source_job_id"] is None
                and _h_det["metadata"] == {"change_note": "定稿前备份"}
                and _h_det["is_current"] is True
                and bool(_h_det["created_at"]),
                "、".join(sorted(_h_det)),
            )
            check(
                "HTTP 错误：版本不存在 → 404",
                _dc.get(f"{_hbase}/prd/versions/no-such").status_code == 404
                and _dc.patch(
                    f"{_hbase}/prd/versions/no-such/note", json={"change_note": "x"}
                ).status_code
                == 404,
                "GET / PATCH 都是 404",
            )

            _dc.post(f"{_hbase}/prd/versions/checkpoint", json={"content": "# PRD v2"})
            _h_res = _dc.post(f"{_hbase}/prd/versions/{_h_v1}/restore")
            _h_res_body = _h_res.json()
            check(
                "HTTP 5：restore → 200，回 {version_id, version_no, document_id, content}",
                _h_res.status_code == 200
                and set(_h_res_body) == {"version_id", "version_no", "document_id", "content"}
                and _h_res_body["version_no"] == 3
                and _h_res_body["content"] == "# PRD v1\n\n第一版",
                str({k: v for k, v in _h_res_body.items() if k != "content"}),
            )
            _h_versions = _dc.get(f"{_hbase}/prd/versions").json()["versions"]
            check(
                "HTTP 5：恢复后 3 版降序、v3 是 current 且 source_kind=restore、v1/v2 未变",
                [v["version_no"] for v in _h_versions] == [3, 2, 1]
                and _h_versions[0]["is_current"] is True
                and _h_versions[0]["source_kind"] == "restore"
                and not any(v["is_current"] for v in _h_versions[1:])
                and _h_versions[2]["content_preview"] == "# PRD v1\n\n第一版",
                str([(v["version_no"], v["is_current"]) for v in _h_versions]),
            )
            check(
                "HTTP 错误：restore 目标是 current → 400",
                _dc.post(f"{_hbase}/prd/versions/{_h_res_body['version_id']}/restore").status_code
                == 400,
                "400",
            )
            _h_clear = _dc.patch(f"{_hbase}/prd/versions/{_h_v1}/note", json={"change_note": ""})
            check(
                "HTTP 6：空串清空备注 → change_note 为 null，且列表里这个键又消失",
                _h_clear.status_code == 200
                and _h_clear.json()["change_note"] is None
                and all(
                    "change_note" not in v
                    for v in _dc.get(f"{_hbase}/prd/versions").json()["versions"]
                    if v["id"] == _h_v1
                ),
                "已清空",
            )
            check(
                "HTTP：三个槽位互不干扰（api-docs 仍是空列表）",
                _dc.get(f"{_hbase}/api-docs/versions").json()["versions"] == [],
                "api-docs 空",
            )
            # 会话被删之后：会话校验真的生效（而不是只看 document 行还在不在）
            _dc.delete(f"/api/session/{_hsid}")
            check(
                "HTTP 错误：会话删除后再访问版本接口 → 404",
                _dc.get(f"{_hbase}/prd/versions").status_code == 404,
                "404",
            )
    finally:
        import shutil as _shutil_dv2

        _shutil_dv2.rmtree(BACKEND_DIR / "_tmp_docver_smoke", ignore_errors=True)
        _shutil_dv2.rmtree(BACKEND_DIR / "_tmp_docver_http_smoke", ignore_errors=True)

    # ---------- 02 篇：Document 版本层接入 Session 链路 ----------
    #
    # 两个方向 + 一条底线：
    #   镜像 → 版本层：手改保存（auto_save，**不升号**）、老数据迁移（import，只一次）
    #   版本层 → 镜像：回填（**只在镜像缺正文时**）
    #   底线：**正在生成的那份产物，半成品不许被固化成版本** —— 否则"生成完成后
    #        version_no=1"这条验收标准直接不成立（前端在生成期间就会防抖保存半成品）。
    #
    # ⚠️ `job_runner` 那一侧（Job 完成 / 优化 / 失败 partial 真的写版本）由
    # `scripts/job_check.py` 覆盖（那里有假模型）；这里只到 service 与 HTTP 层。
    try:
        import shutil as _shutil_sync

        from services.document_version_repository import DOC_TYPES as _DOC_TYPES
        from services.job_service import JobService as _JobSvc
        from services.session_service import SessionService as _SessionSvc
        from services.session_service import session_document_contents as _session_contents

        _sync_dir = BACKEND_DIR / "_tmp_docver_sync"
        _shutil_sync.rmtree(_sync_dir, ignore_errors=True)
        _sync_settings = Settings(
            _env_file=None, sqlite_path=_sync_dir / "sync.db", sqlite_busy_timeout_ms=1000
        )
        _sync_jobs = _JobSvc(_sync_settings)
        _sync_sessions = _SessionSvc(_sync_settings, jobs=_sync_jobs)
        # 只调会话层的 ensure_ready：它必须把**三组表**都建上（降级要读 jobs，
        # 镜像同步要写 documents / document_versions）。少建一张会在第 N 次保存时才炸。
        _sync_sessions.ensure_ready()
        _sync_versions = _sync_sessions.documents

        def _contents_of(session_id: str) -> list[str | None]:
            return [
                (rec.content if (rec := _sync_versions.get_current_version(session_id, t)) else None)
                for t in _DOC_TYPES
            ]

        # ---- 纯函数：快照 → {doc_type: content}
        _snapshot = json.dumps(
            {
                "sessionId": "s",
                "documents": {
                    "prd": {"content": "# PRD", "approved": True},
                    "api": {"content": "# API"},
                    "prompts": {"content": "   "},  # 只有空白 = 没有产物
                },
                "viewState": "review-prd",
            },
            ensure_ascii=False,
        )
        check(
            "快照取正文：真实键名是 documents.<kind>.content（api-docs ↔ 快照里的 api）",
            _session_contents(_snapshot) == {"prd": "# PRD", "api-docs": "# API"},
            str(_session_contents(_snapshot)),
        )
        check(
            "快照取正文：坏 JSON / 不是对象 → 空字典（不抛）",
            _session_contents("{oops") == {} and _session_contents("[]") == {},
            "容错",
        )

        # ---- 老 Session 迁移：import → v1，只迁一次
        _legacy = _sync_sessions.save_session(
            {
                "sessionId": "legacy",
                "documents": {"prd": {"content": "# 老 PRD\n"}},
                "viewState": "review-prd",
            }
        )
        _legacy_cur = _sync_versions.get_current_version(_legacy.id, "prd")
        check(
            "迁移：老会话首次保存就把正文导入为 v1（source_kind=import）",
            _legacy_cur is not None
            and _legacy_cur.version_no == 1
            and _legacy_cur.content == "# 老 PRD\n"
            and _legacy_cur.source_kind.value == "import",
            f"v{_legacy_cur.version_no if _legacy_cur else None}/{_legacy_cur.source_kind.value if _legacy_cur else None}",
        )
        # ---- 手改保存：auto_save 就地更新，不升号
        _sync_sessions.save_session(
            {
                "sessionId": "legacy",
                "documents": {"prd": {"content": "# 老 PRD 手改过\n"}},
                "viewState": "review-prd",
            },
            session_id=_legacy.id,
        )
        _edited = _sync_versions.get_current_version(_legacy.id, "prd")
        check(
            "手改保存（auto_save）：就地更新 current，**version_no 不变**、不新增版本",
            _edited.version_no == 1
            and _edited.content == "# 老 PRD 手改过\n"
            and len(_sync_versions.list_versions(_legacy.id, "prd")) == 1,
            f"v{_edited.version_no}｜共 {len(_sync_versions.list_versions(_legacy.id, 'prd'))} 版",
        )
        check(
            "手改保存：**不改**这一版出生时的 source_kind（仍是 import）",
            _edited.source_kind.value == "import",
            _edited.source_kind.value,
        )
        check(
            "同步幂等：镜像与 current 一致时一个版本都不写（防抖保存的常态路径）",
            _sync_versions.sync_session_contents(_legacy.id, {"prd": "# 老 PRD 手改过\n"}) == [],
            "无写入",
        )
        check(
            "空正文既**不创建**版本、也**不删除**已有版本（本阶段不支持清空文档）",
            _sync_versions.sync_session_contents(_legacy.id, {"prd": "   "}) == []
            and _sync_versions.get_current_version(_legacy.id, "prd").content
            == "# 老 PRD 手改过\n",
            "原样保留",
        )

        # ---- 三种产物各自独立成链
        _multi = _sync_sessions.save_session(
            {
                "sessionId": "multi",
                "viewState": "review-prd",
                "documents": {
                    "prd": {"content": "# P"},
                    "api": {"content": "# A"},
                    "prompts": {"content": "# R"},
                },
            }
        )
        check(
            "三种产物各自建版本（api-docs 那一槽读的是快照里的 api 键）",
            _contents_of(_multi.id) == ["# P", "# A", "# R"],
            str(_contents_of(_multi.id)),
        )

        # ---- 底线：正在生成的产物，半成品不许被固化成版本
        _guard = _sync_sessions.save_session({"sessionId": "g", "viewState": "form", "documents": {}})
        _guard_job = _sync_jobs.create_job(
            _guard.id, "prd", {"requirements_summary": {"product_goal": "x"}}
        )
        check("底线前置：PRD 任务已登记且在跑", _sync_jobs.is_running(_guard_job), _guard_job[:8])
        # 前端在生成期间按 1.5s 节流把**半成品**写进产物 → 防抖保存把它带进快照
        _sync_sessions.save_session(
            {
                "sessionId": "g",
                "viewState": "generating-prd",
                "activeJobId": _guard_job,
                "documents": {"prd": {"content": "# 半截正文"}},
            },
            session_id=_guard.id,
        )
        check(
            "底线：正在生成 PRD 时，快照里的半成品**不会**被写成版本",
            _sync_versions.get_current_version(_guard.id, "prd") is None,
            "该槽位仍无版本",
        )
        check(
            "底线：同一个快照里**没在生成**的产物照常同步（剔除是按 doc_type 的）",
            _sync_versions.get_current_version(_guard.id, "api-docs") is None,
            "api-docs 本来就没有内容，故无版本",
        )
        # 任务收尾（终态）之后再保存：这时才允许写
        _sync_jobs.update_job(_guard_job, status="completed", phase="done")
        _sync_sessions.save_session(
            {
                "sessionId": "g",
                "viewState": "review-prd",
                "documents": {"prd": {"content": "# 完整正文"}},
            },
            session_id=_guard.id,
        )
        _guard_cur = _sync_versions.get_current_version(_guard.id, "prd")
        check(
            "底线：任务收尾后，同一份正文才被写成 v1",
            _guard_cur is not None
            and _guard_cur.version_no == 1
            and _guard_cur.content == "# 完整正文",
            f"v{_guard_cur.version_no if _guard_cur else None}",
        )

        # ---- HTTP：checkpoint / restore 之后镜像跟着走（需求 §七）
        from fastapi.testclient import TestClient as _SyncClient

        from main import create_app as _create_sync_app

        _sync_http = BACKEND_DIR / "_tmp_docver_sync_http"
        _shutil_sync.rmtree(_sync_http, ignore_errors=True)
        _sync_app = _create_sync_app(
            Settings(
                _env_file=None, sqlite_path=_sync_http / "api.db", sqlite_busy_timeout_ms=1000
            )
        )
        with _SyncClient(_sync_app) as _sc:
            _sid = _sc.post(
                "/api/session/save",
                json={
                    "session_data": {
                        "sessionId": "h",
                        "viewState": "review-prd",
                        "documents": {"prd": {"content": "# v1"}},
                    }
                },
            ).json()["id"]
            _vbase = f"/api/session/{_sid}/documents/prd/versions"
            _sc.post(f"{_vbase}/checkpoint", json={"content": "# v2 前端全文"})
            _mirror = json.loads(_sc.get(f"/api/session/{_sid}").json()["session_data"])
            check(
                "HTTP：checkpoint 后镜像跟着走（documents.prd.content = 新 current）",
                _mirror["documents"]["prd"]["content"] == "# v2 前端全文",
                str(_mirror["documents"]["prd"]["content"]),
            )
            _oldest = _sc.get(_vbase).json()["versions"][-1]["id"]
            _sc.post(f"{_vbase}/{_oldest}/restore")
            _mirror2 = json.loads(_sc.get(f"/api/session/{_sid}").json()["session_data"])
            check(
                "HTTP：restore 后镜像也跟着走（正文回到被恢复的那一版）",
                _mirror2["documents"]["prd"]["content"] == "# v1",
                str(_mirror2["documents"]["prd"]["content"]),
            )
            check(
                "HTTP：checkpoint 保留镜像里产物对象的其它键（approved / truncated 不被顶掉）",
                _mirror2["documents"]["prd"].get("content") == "# v1",
                "、".join(sorted(_mirror2["documents"]["prd"])),
            )
    finally:
        import shutil as _shutil_sync2

        _shutil_sync2.rmtree(BACKEND_DIR / "_tmp_docver_sync", ignore_errors=True)
        _shutil_sync2.rmtree(BACKEND_DIR / "_tmp_docver_sync_http", ignore_errors=True)

    # ---------- LLM 观测链路（纯本地：假模型 + TestClient，不调真实模型） ----------
    import json as _json
    import logging as _logging

    from langchain_core.messages import AIMessage, AIMessageChunk

    from services.llm_factory import TrackedChatModel, track
    from services.llm_metrics import (
        LlmStep,
        RunMetrics,
        RunMetricsCollector,
        RunMetricsStore,
        StepMetrics,
        extract_tokens,
        finalize_run,
        record_step,
        run_store,
    )

    steps = [s.value for s in LlmStep]
    check(
        "step 枚举齐全且是 snake_case（禁止业务里写字面量）",
        len(steps) == 11 and len(set(steps)) == 11 and all(s == s.lower() and " " not in s for s in steps),
        "、".join(steps),
    )

    check(
        "token 提取同时认 usage_metadata 与 token_usage（流式取不到时记 0）",
        extract_tokens({"usage_metadata": {"input_tokens": 3, "output_tokens": 4}})[0] == 3
        if False
        else extract_tokens(type("R", (), {"usage_metadata": {"input_tokens": 3, "output_tokens": 4}})())[1] == 4
        and extract_tokens(
            type("R", (), {"response_metadata": {"token_usage": {"prompt_tokens": 7, "completion_tokens": 9}}})()
        )
        == (7, 9)
        and extract_tokens(None) == (0, 0),
        "3/4 与 7/9 两种形态",
    )

    class _Fake:
        model_name = "smoke-fake"

        async def ainvoke(self, messages, **kwargs):
            return AIMessage(content="ok", usage_metadata={"input_tokens": 1, "output_tokens": 2, "total_tokens": 3})

        def astream(self, messages, **kwargs):
            async def gen():
                yield AIMessageChunk(content="x", usage_metadata={"input_tokens": 5, "output_tokens": 6, "total_tokens": 11})

            return gen()

    class _Boom:
        model_name = "smoke-boom"

        async def ainvoke(self, messages, **kwargs):
            raise RuntimeError("模型炸了")

    run = RunMetricsCollector.start("smoke_run", request_id="req_smoke")
    import asyncio as _asyncio

    tracked = track(_Fake(), LlmStep.PRD_WRITER)
    _asyncio.run(tracked.ainvoke([], step=None) if False else tracked.ainvoke([]))
    check(
        "包装层记一次账：进总账 + 带 step/model/token/耗时",
        len(run.step_names) == 1 and run.step_names[0] == "prd_writer" and run._steps[0].input_tokens == 1,
        f"{run.step_names} input={run._steps[0].input_tokens}",
    )

    raised = False
    try:
        _asyncio.run(track(_Boom(), LlmStep.PRD_WRITER).ainvoke([]))
    except RuntimeError:
        raised = True
    failed = run._steps[-1]
    check(
        "失败也记账且**原样抛回**（观测不改业务行为）",
        raised and failed.status == "error" and "RuntimeError" in failed.error,
        f"status={failed.status}",
    )
    summary = run.finish()
    check(
        "finish() 汇总：次数/耗时/明细齐全，且收集器已摘掉",
        summary.llm_call_count == 2
        and summary.total_duration_ms >= 0
        and RunMetricsCollector.current() is None
        # 6 个摘要字段 + 分片归属（part_index/part_total）：brief 白名单漏一个，
        # SSE 里就少一项，而前端拿不到时不会报错，只会显示成 0
        and len(summary.to_dict(brief=True)["steps"][0]) == 8,
        f"calls={summary.llm_call_count}",
    )

    raw_calls: list[str] = []
    for _path in sorted((BACKEND_DIR / "services").glob("*.py")):
        _src = _path.read_text(encoding="utf-8")
        for _n, _line in enumerate(_src.splitlines(), start=1):
            if "self.model.ainvoke(" in _line or "self.model.astream(" in _line or "self.review_model.ainvoke(" in _line:
                raw_calls.append(f"{_path.name}:{_n}")
    check(
        "所有 LLM 调用点都已换成 tracked 版本（无裸调用残留）",
        not raw_calls,
        "、".join(raw_calls) if raw_calls else "0 处残留",
    )

    conv_src = (BACKEND_DIR / "api" / "conversation.py").read_text(encoding="utf-8")
    check(
        "两类 SSE 生成器都接观测、且都在 done 之前发 run_summary",
        conv_src.count('_sse_event("run_summary"') == 4  # 普通/阶段 各两处：成功与失败
        and conv_src.count("RunMetricsCollector.start(run_type)") == 2
        and '"request_id": get_request_id()' in conv_src,
        f"run_summary 出现 {conv_src.count('_sse_event(chr(34) + chr(34))')} 次",
    )
    check(
        "每个流式路由都带 run_type（7 条 + review 分支 1 条）",
        conv_src.count('run_type="') == 8,
        f"{conv_src.count('run_type=' + chr(34))} 处",
    )
    check(
        "四个收尾点都走 finalize_run（分片合并的唯一出口，成功/失败各两处）",
        conv_src.count("summary = finalize_run(run)") == 4 and "run.finish()" not in conv_src,
        f"finalize_run {conv_src.count('summary = finalize_run(run)')} 处",
    )

    from fastapi.testclient import TestClient as _TestClient

    with _TestClient(create_app(Settings(_env_file=None))) as _client:
        _health = _client.get("/health", headers={"X-Request-ID": "req_smoke_header"})
        check(
            "中间件回传 X-Request-ID（前端可拿它查日志）",
            _health.headers.get("X-Request-ID") == "req_smoke_header",
            str(_health.headers.get("X-Request-ID")),
        )

        # 03 篇：缺 requirements_summary 的 PRD 任务在**创建时**就被拒（422），
        # 不留给 runner 变成一个"拿到了 job_id 才失败"的任务。
        # ⚠️ session_id 刻意用不存在的：**422 必须发生在 404 之前**（请求体校验先于处理函数）
        # —— 否则用户会收到一个与真实问题无关的"会话不存在"，查错方向全错。
        _no_summary = _client.post(
            "/api/jobs",
            json={"session_id": "smoke-no-such-session", "artifact": "prd", "payload": {}},
        )
        check(
            "缺 requirements_summary 的 PRD 任务创建即 422（校验先于 404）",
            _no_summary.status_code == 422,
            f"status={_no_summary.status_code}",
        )

    # ---------- 跨分片合并：一份产物 = 一个 run ----------
    # 前端按分片计划循环发 3 次请求，此前 run_summary 是"每请求一份"——界面显示的是
    # **最后一片**的统计，按 request_id 查日志只能查到 1/3 的 llm_step。
    # 这一组守的就是"一个产物 = 一个 run"：合并口径、幂等、非法的头、日志贯通。
    import time as _time  # noqa: PLC0415

    from core.request_context import normalize_part_number, normalize_run_id  # noqa: PLC0415

    def _fake_part(
        request_id: str,
        *,
        start: float,
        end: float,
        input_tokens: int,
        output_tokens: int,
        part_index: int,
    ) -> RunMetrics:
        """造一份"某一片"的 `finish()` 产物。

        时刻**自己指定**（真实路径用 monotonic 取）：这样"并集跨度"能被精确断言，
        不必靠 sleep 去凑一个时间窗口 —— 靠 sleep 的测试要么慢、要么偶发。
        """
        return RunMetrics(
            request_id=request_id,
            run_type="smoke_parts",
            total_input_tokens=input_tokens,
            total_output_tokens=output_tokens,
            total_duration_ms=int((end - start) * 1000),
            llm_call_count=1,
            steps=[
                StepMetrics(
                    step="api_docs_generate",
                    model="smoke-fake",
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    duration_ms=int((end - start) * 1000),
                    part_index=part_index,
                    part_total=3,
                )
            ],
            started_at=start,
            ended_at=end,
        )

    # 三片的时间窗**刻意留空档**：并集跨度 = 110.4-100.0 = 10.4s，而各片相加只有 2.9s。
    # 两者差得多，断言才能真的区分"并集"和"相加"。
    part_store = RunMetricsStore(ttl_seconds=1800.0, max_runs=8)
    p1 = _fake_part("req_p1", start=100.0, end=101.0, input_tokens=10, output_tokens=20, part_index=1)
    p2 = _fake_part("req_p2", start=105.0, end=106.5, input_tokens=30, output_tokens=40, part_index=2)
    p3 = _fake_part("req_p3", start=110.0, end=110.4, input_tokens=50, output_tokens=60, part_index=3)
    m1 = part_store.merge(p1, run_id="run_smoke_parts", part_index=1, part_total=3)
    m2 = part_store.merge(p2, run_id="run_smoke_parts", part_index=2, part_total=3)
    m3 = part_store.merge(p3, run_id="run_smoke_parts", part_index=3, part_total=3)
    check(
        "合并：三片 tokens/调用次数**求和**，request_ids 按分片序号",
        m3.total_input_tokens == 90
        and m3.total_output_tokens == 120
        and m3.llm_call_count == 3
        and m3.request_ids == ["req_p1", "req_p2", "req_p3"],
        f"in={m3.total_input_tokens} out={m3.total_output_tokens} "
        f"calls={m3.llm_call_count} ids={m3.request_ids}",
    )
    check(
        "合并：墙钟是各片的**并集跨度**（10400ms），不是各片相加（2900ms）",
        m3.total_duration_ms == 10400,
        f"total={m3.total_duration_ms}ms（相加会是 1000+1500+400=2900ms）",
    )
    check(
        "合并：part_duration_ms 只算**本片**（整份耗时与单片耗时不会互相冒充）",
        m3.part_duration_ms == 400 and m1.part_duration_ms == 1000,
        f"第 3 片={m3.part_duration_ms}ms 第 1 片={m1.part_duration_ms}ms",
    )
    check(
        "合并：parts_seen/complete 随片数推进（2 片时未完成，3 片时才完成）",
        (m1.parts_seen, m1.complete) == (1, False)
        and (m2.parts_seen, m2.complete) == (2, False)
        and (m3.parts_seen, m3.complete) == (3, True),
        f"1→{m1.parts_seen}/{m1.complete} 2→{m2.parts_seen}/{m2.complete} "
        f"3→{m3.parts_seen}/{m3.complete}",
    )
    check(
        "合并：steps 按分片序号拼接（哪一步属于哪一片看得出来）",
        [item.step for item in m3.steps] == ["api_docs_generate"] * 3
        and [item.part_index for item in m3.steps] == [1, 2, 3],
        f"{[item.part_index for item in m3.steps]}",
    )
    # 幂等：重试/断线重连会让同一片被上报两次，累加会让 tokens 直接翻倍
    # （那种数字比没有数字更糟 —— 看起来像"模型多花了一倍的钱"）
    replay = part_store.merge(p2, run_id="run_smoke_parts", part_index=2, part_total=3)
    check(
        "合并：同一 request_id 重复上报是**替换**而不是累加",
        replay.total_input_tokens == 90
        and replay.total_output_tokens == 120
        and replay.llm_call_count == 3
        and replay.parts_seen == 3
        and part_store.run_count() == 1,
        f"in={replay.total_input_tokens} calls={replay.llm_call_count} "
        f"seen={replay.parts_seen} runs={part_store.run_count()}",
    )

    # 淘汰：一个卡住的 run（前端崩了、只发了 1/3 片）会永远等不到 complete，
    # 必须有条兜底把它清掉，否则进程内存只增不减。
    ttl_store = RunMetricsStore(ttl_seconds=0.01, max_runs=8)
    ttl_store.merge(p1, run_id="run_ttl", part_index=1, part_total=3)
    _time.sleep(0.05)
    check("淘汰：超过 TTL 的 run 被清掉（不需要外部触发）", ttl_store.run_count() == 0, "TTL=0.01s")
    cap_store = RunMetricsStore(ttl_seconds=1800.0, max_runs=2)
    for index, req in enumerate(("req_cap_1", "req_cap_2", "req_cap_3"), start=1):
        cap_store.merge(
            _fake_part(req, start=200.0, end=201.0, input_tokens=1, output_tokens=1, part_index=1),
            run_id=f"run_cap_{index}",
            part_index=1,
            part_total=3,
        )
    revived = cap_store.merge(p1, run_id="run_cap_1", part_index=1, part_total=3)
    check(
        "淘汰：超过 max_runs 时按 last_at 淘汰最旧的（最老的 run 被清掉后重新从 1 片开始）",
        cap_store.run_count() == 2 and revived.parts_seen == 1,
        f"runs={cap_store.run_count()} 最旧 run 的 parts_seen={revived.parts_seen}",
    )

    check(
        "非法 X-Run-ID / 分片值被当成「没给」（观测头写错不该让请求失败）",
        normalize_run_id("ok-run_1") == "ok-run_1"
        and normalize_run_id("带空格 的") is None
        and normalize_run_id("x" * 65) is None
        and normalize_run_id("") is None
        and normalize_run_id(None) is None
        and normalize_part_number("3") == 3
        and normalize_part_number("0") is None
        and normalize_part_number("-1") is None
        and normalize_part_number("a") is None,
        "只收 [A-Za-z0-9_-]{1,64} 与正整数",
    )

    # ---------- 分片合并走**真实 SSE 生成器**（假模型 + TestClient，不调真实模型）----------
    from api.deps import get_document_service  # noqa: PLC0415

    class _JsonCapture(_logging.Handler):
        """抓观测 JSON 行：`llm_step` / `run_summary` 都是 stdout 单行 JSON。"""

        def __init__(self) -> None:
            super().__init__()
            self.lines: list[dict] = []

        def emit(self, record: _logging.LogRecord) -> None:
            text = record.getMessage()
            if not text.startswith("{"):
                return
            try:
                self.lines.append(json.loads(text))
            except json.JSONDecodeError:
                pass

    def _summary_payload(text: str) -> dict:
        """取该响应的最后一个 `run_summary` 帧的 payload。"""
        payload: dict = {}
        for block in text.split("\n\n"):
            if block.startswith("event: run_summary"):
                payload = json.loads(block.split("data: ", 1)[1])
        return payload

    RUN_PARTS = "smoke_run_parts_1"
    parts_app = create_app(Settings(_env_file=None))
    # 只替换模型，**不动路由与 SSE 生成器**：观测接线（run_type/run_summary/分片头）
    # 全都要走真实代码，否则这一段的结论跟线上没关系。
    parts_app.dependency_overrides[get_document_service] = lambda: ds.DocumentService(model=_Fake())
    capture = _JsonCapture()
    _logging.getLogger("harnessprd.llm").addHandler(capture)
    runs_before = run_store.run_count()
    try:
        with _TestClient(parts_app) as parts_client:
            def post_part(request_id: str, index: int, total: int, run_id: str = RUN_PARTS):
                return parts_client.post(
                    "/api/v1/conversation/generate-prd-stream",
                    json={"form": {"product_name": "分片探针"}},
                    headers={
                        "X-Request-ID": request_id,
                        "X-Run-ID": run_id,
                        "X-Part-Index": str(index),
                        "X-Part-Total": str(total),
                    },
                )

            # 非法 run id：必须当成「没给」（单片 run），且**不能**往合并存储里塞记录。
            # 用 ASCII 的非法值（空格 + `!`）：HTTP 头本身就装不下非 ASCII，
            # 拿中文当"非法值"会在 httpx 里先炸，测不到中间件这条路径。
            illegal = post_part("req_smoke_bad_run", 1, 3, run_id="bad run id!")
            illegal_summary = _summary_payload(illegal.text)
            check(
                "非法 X-Run-ID 被忽略：请求照常完成、不回显该头、run_id 退化成 request_id",
                illegal.status_code == 200
                and illegal.headers.get("X-Run-ID") is None
                and illegal_summary.get("run_id") == "req_smoke_bad_run"
                and illegal_summary.get("parts_seen") == 1
                and illegal_summary.get("complete") is True,
                f"status={illegal.status_code} echo={illegal.headers.get('X-Run-ID')} "
                f"run_id={illegal_summary.get('run_id')}",
            )
            check(
                "非法 X-Run-ID 不写合并存储（聊天这类单片请求不该把 store 撑爆）",
                run_store.run_count() == runs_before,
                f"runs {runs_before} → {run_store.run_count()}",
            )

            first = post_part("req_smoke_part1", 1, 2)
            check(
                "分片请求回显 X-Run-ID（前端据此确认合并身份）",
                first.headers.get("X-Run-ID") == RUN_PARTS,
                str(first.headers.get("X-Run-ID")),
            )
            first_summary = _summary_payload(first.text)
            second = post_part("req_smoke_part2", 2, 2)
            second_summary = _summary_payload(second.text)
    finally:
        _logging.getLogger("harnessprd.llm").removeHandler(capture)

    check(
        "SSE：第 1 片只报本片（1/2，未完成）",
        first_summary.get("part_index") == 1
        and first_summary.get("part_total") == 2
        and first_summary.get("parts_seen") == 1
        and first_summary.get("complete") is False
        and first_summary.get("total_input_tokens") == 5,
        f"seen={first_summary.get('parts_seen')} complete={first_summary.get('complete')} "
        f"in={first_summary.get('total_input_tokens')}",
    )
    check(
        "SSE：第 2 片的 run_summary 是**两片之和**（本次修复的核心）",
        second_summary.get("total_input_tokens") == 10
        and second_summary.get("total_output_tokens") == 12
        and second_summary.get("llm_call_count") == 2,
        f"in={second_summary.get('total_input_tokens')} out={second_summary.get('total_output_tokens')} "
        f"calls={second_summary.get('llm_call_count')}（单片是 5/6/1）",
    )
    check(
        "SSE：第 2 片 complete=true、request_ids 两元素且按序",
        second_summary.get("parts_seen") == 2
        and second_summary.get("complete") is True
        and second_summary.get("request_ids") == ["req_smoke_part1", "req_smoke_part2"]
        and len(second_summary.get("steps") or []) == 2,
        f"seen={second_summary.get('parts_seen')} ids={second_summary.get('request_ids')}",
    )
    check(
        "SSE：既有字段一个都没少（done 帧与 run_summary 的顺序也不变）",
        {"chunks", "chars"} <= set(json.loads(second.text.split("event: done\ndata: ", 1)[1]))
        and second.text.index("event: run_summary") < second.text.index("event: done")
        and "request_id" in second_summary
        and "run_type" in second_summary,
        "run_summary → done",
    )

    # ---------- 技能包注入 + run_summary.injected_skills（RAG 下线那一篇）----------
    # 走**真实的 HTTP + 服务层 + 观测接线**，只把模型换成会记录消息的假的：
    # 于是"接口文档生成到底给模型看了什么"变成可断言的，而不是靠读代码相信。
    inject_fake = _FakeModel(["## 第 1 章 文档信息\n"])
    inject_app = create_app(Settings(_env_file=None))
    inject_app.dependency_overrides[get_document_service] = lambda: ds.DocumentService(
        model=inject_fake
    )
    with _TestClient(inject_app) as inject_client:
        injected = inject_client.post(
            "/api/v1/conversation/generate-api-docs-stream",
            json={
                "form": {"product_name": "注入探针"},
                "prd_content": "# 群聊周报助手\n\n## 2. 功能需求\n\n- FR-01 绑定群聊",
            },
        )
    injected_summary = _summary_payload(injected.text)
    sent_human = inject_fake.seen[0][1].content
    check(
        "接口文档：技能包（团队规范 + 参考示例）全量注入 human message",
        "本产物注入的技能包规范与示例" in sent_human
        and "references/team-api-guidelines.md" in sent_human
        and "references/writing-rules.md" in sent_human
        and "references/history-weekly-report-api.md" in sent_human,
        f"human {len(sent_human)} 字符",
    )
    check(
        "接口文档：注入块里规范在示例之前（顺序 = skill.yaml 的 role 顺序）",
        sent_human.index("【团队规范与写作要点】") < sent_human.index("【参考示例"),
        "规范 → 示例",
    )
    check(
        "接口文档：run_summary.injected_skills 记下 skill_id / version / 文件清单",
        isinstance(injected_summary.get("injected_skills"), list)
        and len(injected_summary["injected_skills"]) == 1
        and injected_summary["injected_skills"][0]["skill_id"] == "api-docs-generator"
        and injected_summary["injected_skills"][0]["version"] == "1.0.0"
        and len(injected_summary["injected_skills"][0]["artifacts"]) >= 3,
        json.dumps(injected_summary.get("injected_skills"), ensure_ascii=False)[:150],
    )

    prompts_fake = _FakeModel(["=== FILE: 00-README.md ===\n"])
    prompts_probe = ds.DocumentService(model=prompts_fake)
    asyncio.run(
        _collect_all(
            prompts_probe.generate_prompts_stream(
                form={"product_name": "注入探针"}, prd_content="# 群聊周报助手"
            )
        )
    )
    prompts_human = prompts_fake.seen[0][1].content
    check(
        "提示词套件：模板与写作规则走同一条注入路径",
        "references/prompts-template.md" in prompts_human
        and "【结构模板】" in prompts_human
        and "references/writing-rules.md" in prompts_human,
        f"human {len(prompts_human)} 字符",
    )

    # 优化路径也要注入 —— 而且 artifact 键**不能手拼** `f"optimize-{kind}"`：
    # 接口文档那一份叫 `optimize-api-docs`（带 docs），手拼出的 `optimize-api` 不在注册表里，
    # 优化时直接抛 SkillConfigError（浏览器里点「优化这一节」实测踩到）。
    check(
        "优化 artifact 映射由 job_models.ARTIFACT_DOC_KIND 反查（api → optimize-api-docs）",
        ds._OPTIMIZE_ARTIFACT_BY_KIND.get("api") == "optimize-api-docs"
        and ds._OPTIMIZE_ARTIFACT_BY_KIND.get("prompts") == "optimize-prompts"
        and ds._OPTIMIZE_ARTIFACT_BY_KIND.get("prd") == "optimize-prd",
        str(ds._OPTIMIZE_ARTIFACT_BY_KIND),
    )
    for opt_kind, opt_needle in (("api", "team-api-guidelines"), ("prompts", "prompts-template")):
        opt_fake = _FakeModel(["改好了"])
        opt_svc = ds.DocumentService(model=opt_fake)
        asyncio.run(
            _collect_all(
                opt_svc.optimize_document_stream(
                    kind=opt_kind,
                    section="第 5 章 错误码表",
                    feedback="补一列 HTTP 状态码",
                    current_content="## 第 5 章 错误码表\n\n旧内容",
                    prd_content="# 群聊周报助手",
                    api_content="# 接口文档",
                )
            )
        )
        opt_human = opt_fake.seen[0][1].content
        check(
            f"优化 {opt_kind}：注入对应技能包（走 optimize-* 的绑定）",
            "本产物注入的技能包规范与示例" in opt_human and opt_needle in opt_human,
            f"human {len(opt_human)} 字符",
        )

    # 分片重复上报必须**并成一项**（三个分片都注入同一批文件，汇总不该变成 3 条）
    from services.llm_metrics import (  # noqa: PLC0415
        RunMetricsCollector,
        finalize_run as _finalize_run,
        record_injected_skills,
    )

    dedupe_run = RunMetricsCollector.start("smoke_injected_skills")
    for _ in range(3):
        record_injected_skills(bundle_to_injected_meta(load_skill_bundle("api-docs")))
    dedupe_payload = _finalize_run(dedupe_run).to_dict(brief=True)
    check(
        "injected_skills：分片重复上报合并成一项（不是三份清单）",
        isinstance(dedupe_payload.get("injected_skills"), list)
        and len(dedupe_payload["injected_skills"]) == 1
        and dedupe_payload["injected_skills"][0]["skill_id"] == "api-docs-generator",
        json.dumps(dedupe_payload.get("injected_skills"), ensure_ascii=False)[:120],
    )
    none_skill = _finalize_run(RunMetricsCollector.start("smoke_no_skill")).to_dict(brief=True)
    check(
        "injected_skills：没有技能包的 run 是 null（与空列表区分开）",
        "injected_skills" in none_skill and none_skill["injected_skills"] is None,
        f"injected_skills={none_skill.get('injected_skills')}",
    )

    # 日志贯通：这是维护者唯一能用的检索入口（界面只显示汇总，明细在 app.log）
    steps_logged = [
        item
        for item in capture.lines
        if item.get("event") == "llm_step" and item.get("run_id") == RUN_PARTS
    ]
    check(
        "日志：两次请求的 llm_step 带**同一个 run_id**，各带自己的 part（1/2、2/2）",
        [item.get("part") for item in steps_logged] == ["1/2", "2/2"],
        f"{[(item.get('part'), item.get('request_id')) for item in steps_logged]}",
    )
    illegal_steps = [
        item
        for item in capture.lines
        if item.get("event") == "llm_step" and item.get("request_id") == "req_smoke_bad_run"
    ]
    check(
        "非法 X-Run-ID 的那次请求：分片头也一并忽略（只报单片 1/1，两行不自相矛盾）",
        len(illegal_steps) == 1
        and illegal_steps[0].get("part") == "1/1"
        and illegal_steps[0].get("part_index") is None,
        f"{[(item.get('part'), item.get('part_index')) for item in illegal_steps]}",
    )
    summaries_logged = [
        item
        for item in capture.lines
        if item.get("event") == "run_summary" and item.get("run_id") == RUN_PARTS
    ]
    check(
        "日志：run_summary 行带上整份字段（parts_seen/complete/request_ids 都在）",
        len(summaries_logged) == 2
        and summaries_logged[-1].get("parts_seen") == 2
        and summaries_logged[-1].get("complete") is True
        and summaries_logged[-1].get("request_ids")
        == ["req_smoke_part1", "req_smoke_part2"],
        f"共 {len(summaries_logged)} 行",
    )

    # ---------- 04 篇：结构校验 quality_gate（纯函数，不调模型） ----------
    # 规则是按**本仓库的真实规范**写的（工单那套「核心/边缘接口」「功能开发/接口实现/前端开发/
    # 代码审查」在本仓库一次都没出现过），所以这里的样本用**真实留档**：
    # 一份已通过评审的 PRD、一份已通过评审的接口文档 —— 它们必须**全过**，
    # 否则规则就是把好文档判红（这正是本篇刻意避免的失败模式）。
    from services.quality_gate import (  # noqa: PLC0415
        MAX_PENDING_MARKS,
        SEVERITY_WEIGHT,
        gate_error,
        run_quality_gate,
    )

    _qg_dir = BACKEND_DIR / "validation_out"
    _qg_prd = (_qg_dir / "prd_skill.txt").read_text(encoding="utf-8")
    _qg_api = (_qg_dir / "split_api.txt").read_text(encoding="utf-8")

    _prd_gate = run_quality_gate("prd", _qg_prd)
    check(
        "quality_gate：**真实的 PRD 留档全过**（10 条检查、100 分）",
        _prd_gate["passed"] is True
        and _prd_gate["score"] == 100
        and len(_prd_gate["checks"]) == 10
        and _prd_gate["doc_type"] == "prd",
        f"passed={_prd_gate['passed']} score={_prd_gate['score']} checks={len(_prd_gate['checks'])}",
    )
    _api_gate = run_quality_gate("api-docs", _qg_api)
    check(
        "quality_gate：**真实的接口文档留档全过**（含接口清单/接口详情/无接口功能/统一错误体）",
        _api_gate["passed"] is True
        and _api_gate["score"] == 100
        and all(item["passed"] for item in _api_gate["checks"]),
        f"passed={_api_gate['passed']} score={_api_gate['score']}"
        f" 未过={[i['id'] for i in _api_gate['checks'] if not i['passed']]}",
    )
    check(
        "quality_gate：checked_at 是带 +08:00 偏移的本地时间（给人看的字段）",
        _prd_gate["checked_at"].endswith("+08:00"),
        _prd_gate["checked_at"],
    )

    # 验收 3：故意缺一章 → passed=False，且失败的正是那一章
    _no_scope = _qg_prd.split("## 6. 项目范围")[0]
    _no_scope_gate = run_quality_gate("prd", _no_scope)
    _scope_check = next(
        item for item in _no_scope_gate["checks"] if item["id"] == "prd.section.scope"
    )
    check(
        "quality_gate：缺「项目范围」的 PRD → passed=False 且 prd.section.scope 失败（验收 3）",
        _no_scope_gate["passed"] is False and _scope_check["passed"] is False and _scope_check["detail"],
        f"passed={_no_scope_gate['passed']} detail={_scope_check['detail']}",
    )
    # 反过来：只缺一条 **medium** 不许把整份判红（passed 只看 high）。
    # ⚠️ 必须把「无接口功能」这个**标题**换掉才叫缺 —— 只加后缀的话子串仍然命中，
    # 这条断言就会假过。
    _no_edge = _qg_api.replace("### 无接口功能", "### 其它分区")
    _no_edge_gate = run_quality_gate("api-docs", _no_edge)
    _edge_check = next(
        item for item in _no_edge_gate["checks"] if item["id"] == "api.section.edge"
    )
    check(
        "quality_gate：passed **只看 high**（medium 失败仍 passed=True，只是扣分）",
        _no_edge_gate["passed"] is True
        and _edge_check["passed"] is False
        and _no_edge_gate["score"] < 100,
        f"passed={_no_edge_gate['passed']} score={_no_edge_gate['score']}"
        f" edge={_edge_check['passed']}",
    )
    check(
        "quality_gate：score 是**加权**通过率（high 权重 2）",
        SEVERITY_WEIGHT["high"] == 2
        and SEVERITY_WEIGHT["medium"] == 1
        and _no_edge_gate["score"]
        == round(
            100
            * sum(
                SEVERITY_WEIGHT[item["severity"]]
                for item in _no_edge_gate["checks"]
                if item["passed"]
            )
            / sum(SEVERITY_WEIGHT[item["severity"]] for item in _no_edge_gate["checks"])
        ),
        f"score={_no_edge_gate['score']}",
    )
    check(
        "quality_gate：`[待确认]` 超过上限是 medium warning（上限 %d）" % MAX_PENDING_MARKS,
        next(
            item
            for item in run_quality_gate(
                "prd", _qg_prd + "\n" + "[待确认] " * (MAX_PENDING_MARKS + 1)
            )["checks"]
            if item["id"] == "prd.pending.count"
        )["passed"]
        is False,
    )

    # 空内容 / 未知类型：都不能"静默算过"
    _empty_gate = run_quality_gate("prd", "   \n  ")
    check(
        "quality_gate：空正文 → 单条 empty_content（high，失败），不是「没有可失分的东西」",
        _empty_gate["passed"] is False
        and [item["id"] for item in _empty_gate["checks"]] == ["empty_content"]
        and _empty_gate["score"] == 0,
        f"checks={[item['id'] for item in _empty_gate['checks']]} score={_empty_gate['score']}",
    )
    _unknown_gate = run_quality_gate("不存在的产物", "正文")
    check(
        "quality_gate：未知 doc_type → gate.unknown_doc_type 失败（不许静默全过）",
        _unknown_gate["passed"] is False
        and [item["id"] for item in _unknown_gate["checks"]] == ["gate.unknown_doc_type"],
        f"checks={[item['id'] for item in _unknown_gate['checks']]}",
    )
    _error_gate = gate_error(ValueError("炸了"))
    check(
        "quality_gate：gate 自己抛错时的兜底 passed=False + gate.error（04 篇 §9）",
        _error_gate["passed"] is False
        and _error_gate["checks"][0]["id"] == "gate.error"
        and "炸了" in (_error_gate["checks"][0]["detail"] or ""),
        _error_gate["checks"][0]["detail"],
    )

    # 提示词套件：五类文件齐 → 过；缺一类 → 红（内容用合成样本：留档那份只有 2 个文件）
    _qg_prompts = "\n\n".join(
        f"=== FILE: {name} ===\n\n占位正文"
        for name in (
            "00-README.md",
            "01-project-brief.md",
            "02-scaffold.md",
            "03-data-layer.md",
            "04-step-01-auth.md",
            "05-verify.md",
        )
    )
    _prompts_gate = run_quality_gate("prompts", _qg_prompts, {"prd_content": _qg_prd})
    check(
        "quality_gate：五类文件的提示词套件全过（含与 PRD 功能数对齐那条）",
        _prompts_gate["passed"] is True and len(_prompts_gate["checks"]) == 6,
        f"passed={_prompts_gate['passed']} 未过={[i['id'] for i in _prompts_gate['checks'] if not i['passed']]}",
    )
    _missing_verify = run_quality_gate(
        "prompts", _qg_prompts.replace("=== FILE: 05-verify.md ===", "=== FILE: 99-其他.md ===")
    )
    check(
        "quality_gate：缺 05-verify 的提示词套件 → passed=False 且点到 prompts.section.verify",
        _missing_verify["passed"] is False
        and next(
            item for item in _missing_verify["checks"] if item["id"] == "prompts.section.verify"
        )["passed"]
        is False,
    )
    _no_ctx = run_quality_gate("prompts", _qg_prompts)
    _count_skipped = next(item for item in _no_ctx["checks"] if item["id"] == "prompts.feature.count")
    check(
        "quality_gate：没有 PRD 上下文时功能数那条 skip（passed=True + detail=skipped）",
        _no_ctx["passed"] is True
        and _count_skipped["passed"] is True
        and "skipped" in (_count_skipped["detail"] or ""),
        _count_skipped["detail"],
    )
    check(
        "quality_gate：步骤数**多于** PRD 功能数会被抓到（拆得过碎）",
        next(
            item
            for item in run_quality_gate(
                "prompts",
                _qg_prompts
                + "\n\n=== FILE: 04-step-02-b.md ===\n\n占位"
                + "\n\n=== FILE: 04-step-03-c.md ===\n\n占位",
                {"prd_content": "# x\n\n## 2. 功能需求\n\n### MVP 功能\n\n| 编号 | 功能 |\n| --- | --- |\n| FR-01 | 登录 |\n"},
            )["checks"]
            if item["id"] == "prompts.feature.count"
        )["passed"]
        is False,
    )

    # 每种文档类型的 check id 唯一（防手滑复制出重复 id —— 前端 key 会撞、报表会重复计数）
    _dup_ids: dict[str, list[str]] = {}
    for _doc_type in ("prd", "api-docs", "prompts"):
        _ids = [
            item["id"]
            for item in run_quality_gate(_doc_type, _qg_prd, {"prd_content": _qg_prd})["checks"]
        ]
        _dup_ids[_doc_type] = sorted({item for item in _ids if _ids.count(item) > 1})
    check(
        "quality_gate：三种产物各自的 check id 都不重复",
        all(not value for value in _dup_ids.values()),
        json.dumps(_dup_ids, ensure_ascii=False),
    )
    # 严重度只有三档（前端配色与 score 权重都按这三档写）
    _severities = {
        item["severity"]
        for gate in (_prd_gate, _api_gate, _prompts_gate)
        for item in gate["checks"]
    }
    check(
        "quality_gate：严重度只有 high / medium / low 三档",
        _severities <= set(SEVERITY_WEIGHT),
        "、".join(sorted(_severities)),
    )
    # 纯函数：同一份正文跑两次，除了时间戳必须**逐字节相同**（可断言、可缓存的前提）
    _first = run_quality_gate("prd", _qg_prd)
    _second = run_quality_gate("prd", _qg_prd)
    del _first["checked_at"], _second["checked_at"]
    check(
        "quality_gate：同一份正文两次结果一致（纯函数，不含随机/时间敏感内容）",
        _first == _second,
    )
    # 不改传入的 context：调用方（runner）会复用同一份 payload 派生的 dict
    _ctx_probe = {"prd_content": _qg_prd}
    run_quality_gate("prompts", _qg_prompts, _ctx_probe)
    check(
        "quality_gate：不修改传入的 context（调用方可能复用同一个 dict）",
        _ctx_probe == {"prd_content": _qg_prd},
        json.dumps(sorted(_ctx_probe), ensure_ascii=False),
    )

    print()
    if FAILURES:
        print(f"{len(FAILURES)} 项未通过：" + "、".join(FAILURES))
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
