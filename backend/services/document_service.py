"""三份产物的**生成 / 优化**服务；审核与质检仍是占位。

产物是**链条不是并列**：PRD 是唯一源头，接口文档从 PRD 推导，提示词套件消费前两者
（`HANDOFF.md` §1）。所以三个生成方法有硬性调用顺序，本模块用**入参校验**把它挡住：

| 方法 | 必需的上游 |
| --- | --- |
| `generate_prd_stream` | 表单 + 对话（`form` / `known_info`） |
| `generate_api_docs_stream` | **`prd_content`**（空则直接抛错） |
| `generate_prompts_stream` | **`prd_content`**（`api_content` 选填） |

⚠️ 本层**不 import `api.*`、也不 import `fastapi`** —— 依赖方向 `api → services → core` 单向。
这里的输入输出全是普通类型，所以 CLI、批处理、脚本都能直接复用（`scripts/validate_prompts.py`
就是同一个组装路径）。

---

## 这一层做了什么、没做什么

**做了**：模板加载 / 组装 / 渲染、占位符注入的完整性守卫、单次 LLM 调用的流式产出、
按 `DocKind` 选提示词、文档修订（F8.6 / F4.8）。

**没做**，逐条写清楚免得被当成已完成：

| 缺口 | 为什么不能在这一层做 | 出处 |
| --- | --- | --- |
| **生成不是后台任务** | 设计要求生成**不能绑在 HTTP 请求上**（刷新会 abort 请求、结果无人接收），要做成后台任务 + 2s 轮询。那需要 `SessionStore` / `apply_event`，两者都还没实现 | `docs/会话持久化方案.md` §7.1 / §7.5 |
| **没有并发上限 / 重试 / 计费保护** | 属于任务层，不是生成层。⚠️ 尤其**不要自动重试** —— 生成有真实成本，不该在用户不知情时重复扣费 | `状态数据设计` §6.1 |
| **不落任何状态** | 产出后要写 `provider` / `model` / `temperature` / `usage`（F4.13、`状态数据设计` §2.3）。现在这些元信息**只在流里**，调用方拿不到 | — |
| **没有 HTTP 路由** | `/sessions/{id}/documents/{kind}` 仍是 501 占位。上面的表说明了原因 | `api/sessions.py` |
| **分片计划没有第二份来源** | `{doc_outline}` 等三个占位符由调用方通过 `DocumentScope` 注入，本模块**不自带章节清单** —— 提示词里已经内嵌了一份，再抄一份必然漂移（坑 #10）。是否改成解析 `docs/*模板.md` 见 `gen_common.md` 的待决策 | `HANDOFF.md` §4 坑 #10、§6 第 6 项 |
| **`generate` / `stream_generation` / `check` 仍是占位** | 需要会话状态层（前两个）与 M8 质检规则（最后一个）。质检**只校验不改写**，要改一律回到生成 | F8.6 |

## 与已验证行为的关系

`scripts/validate_prompts.py` 跑通过 gen 族（7 份留档在 `validation_out/`）。本模块刻意与它**对齐**：

- 同一个组装函数（`core.prompts.build_system_prompt`），不在服务层另拼一遍
- human message **逐字保持**验证时的形状：`本次只生成：{generate_scope}\n\n详细规格：\n{scope_spec}`
- 表单格式化复用 `conversation_service.format_form_data`（同一份实现，避免两份漂移）

⚠️ **唯一的差异是"多注入"**：验证脚本当时**没有注入** `{doc_outline}` / `{generate_scope}` /
`{scope_spec}`，它们是**以字面量**留在 system prompt 里的（`HANDOFF.md` §4 坑 #11）——
模型看到 `{generate_scope}` 这种字样照样能输出像样的结果，于是"验证通过"掩盖了"输入根本没送到"。
本模块把这三个补上了（**叠加**在原有 human message 之外，不是替换），所以生成的 prompt
比留档那次更完整。**严格说留档不等于对本模块的验证**，见 `missing_placeholders()`。
"""

from __future__ import annotations

import json
import logging
import math
import re
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage

from core.config import Settings, get_settings
from core.prompts import (
    build_system_prompt,
    declared_placeholders,
    load_prompt,
    render_prompt_text,
)
from core.questions import load_questions
from services.conversation_service import format_form_data, message_text
from services.api_docs_review_agent import build_api_docs_review_messages
from services.job_models import ARTIFACT_DOC_KIND, is_optimize_artifact
from services.llm import LlmConfigError, StreamOutcome, finish_reason_of, is_truncated
from services.llm_factory import get_llm, track
from services.llm_metrics import LlmStep, record_injected_skills

_DEFAULT_STEP_BY_KIND: dict[str, LlmStep] = {
    "prd": LlmStep.PRD_WRITER,
    "api": LlmStep.API_DOCS_GENERATE,
    "prompts": LlmStep.PROMPTS_GENERATE,
}
from services.prompts import (
    API_DOCS_GENERATION_PROMPT,
    OPTIMIZE_DOCUMENT_PROMPT_TEMPLATE,
    PRD_GENERATION_PROMPT,
    PROMPTS_GENERATION_PROMPT,
)
from services.prompts_review_agent import build_prompts_review_messages
from services.review_verdict import normalize_review_verdict
from services.skill_loader import (
    SkillBundle,
    SkillConfigError,
    bundle_to_injected_meta,
    compose_prompt_sections,
    load_skill_bundle,
)
from services.state import DocKind

logger = logging.getLogger(__name__)

# 空占位。与 `conversation_service._EMPTY` 同样取值、同样理由：
# **不能留空字符串** —— 留空等于告诉模型"这个维度没输入"，而它就会拿邻近内容顶替
# （`HANDOFF.md` §4 坑 #3，实测合规小节被权限内容填满）。
_EMPTY = "（尚无）"

# 生成指令的 human message 模板。
# ⚠️ 与 `scripts/validate_prompts.py` 的 `run_gen()` **逐字一致** —— 换了措辞就换了
# 验证条件，那 7 份留档不再说明任何事。
# 用占位符而不是 f-string 拼：这样它与 system prompt 注入的是**同一个** `generate_scope`，
# 两处不可能不一致。
GEN_HUMAN_TEMPLATE = "本次只生成：{generate_scope}\n\n详细规格：\n{scope_spec}"

# 三份产物各自的 system prompt 模板（**未渲染**，含 `gen_common` 基线）。
# 键与 `DocKind` 一致，也就是与 `core/prompts/gen_*.md` 的后缀一致 —— 三处命名对齐，
# 不需要任何映射表（有映射表就多一处要同步的地方）。
SYSTEM_TEMPLATES: dict[DocKind, str] = {
    "prd": PRD_GENERATION_PROMPT,
    "api": API_DOCS_GENERATION_PROMPT,
    "prompts": PROMPTS_GENERATION_PROMPT,
}

# ---------------------------------------------------------------- 技能包（skills/）

# 技能包的**文件清单与路径不在这里**（01 篇改造）：
#   - 「哪个 skill 有哪些文件、各是什么角色」= `skills/{id}/skill.yaml` 的自描述
#   - 「哪个产物用哪些 skill」= `config/skills.yaml` 的全局绑定
#   - 读文件、校验、按角色分组 = `services/skill_loader.py`
#
# 本模块只消费 loader 的结果，不再自己拼路径。改造前这里放着
# `SKILL_ROOT` / `SKILL_PRD_DIR` / `SKILL_PRD_TEMPLATE` / `SKILL_PRD_FIELD_SCHEMA` /
# `SKILL_PRD_ARTIFACTS` 五个常量 + 一个 `_load_skill_artifact()`：路径绑死在业务模块里，
# 加一个 skill 只能把"读文件 + 目录穿越检查"再抄一遍，而且"这个 skill 适用于哪些产物"
# 没有地方声明（工单 01 篇 §二）。
#
# PRD 技能对应的 **artifact 名**：与 `config/skills.yaml` 的 key 一致（也就是 Job artifact）。
SKILL_PRD_ARTIFACT = "prd"

# 技能路径的 human message 模板。
# ⚠️ 与 `GEN_HUMAN_TEMPLATE` 是**两份**，不能混用：那个模板是"本次只生成某个分片"的
# 指令（为 15 章分片生成写的），而技能包是整份 6 章、不分片，套过去会给出矛盾指令
# （提示词说"只输出这些章节"，技能包说"输出这 6 章"）。
# 用占位符而不是 f-string：这样它与 system prompt 取的是**同一批** values。
SKILL_PRD_HUMAN_TEMPLATE = (
    "按上面给出的工作流，为「{product_name}」生成一份 PRD。\n\n"
    "表单作答：\n{form_summary}\n\n"
    "对话确认的信息（优先级高于表单，冲突时以它为准）：\n{known_info}\n\n"
    "用户跳过的项（必须原样落进文档，并在原处标注 [待确认]）：\n{open_questions}\n\n"
    "已发现的矛盾及其结论：\n{conflicts}\n"
)

# ---------------------------------------------------------------- 对话 → 结构化摘要回填

# 回填用的 system prompt 名。规则（只回填、四不改、完整返回）都在这个文件里，
# **不在这里再写一遍** —— 提示词只有一份真相（HANDOFF.md §4 坑 #10）。
SYNC_SUMMARY_PROMPT = "sync_summary"

# 回填的 human message 模板。
# `{field_schema}` 注入的是 `field-schema.json` 的**原文**，而不是在 Python 里再列一遍字段：
# 字段集合必须只有一处真相，否则提示词与 schema 必然漂移（同上坑 #10）。
SYNC_SUMMARY_HUMAN_TEMPLATE = (
    "字段定义（唯一真相，键名以此为准）：\n\n{field_schema}\n\n"
    "当前结构化需求摘要：\n\n{current_summary}\n\n"
    "对话历史（按时间正序）：\n\n{conversation}\n\n"
    "按上面的规则回填，只输出更新后的**完整**摘要 JSON。"
)


# ---------------------------------------------------------------- 从结构化摘要直接生成 PRD

# 这条路径的 human message：输入是**结构化摘要**（8 字段），而不是 20 题表单作答。
# 约束写在这里而不是散进提示词文件：它是"这一次调用"的指令（同 `GEN_HUMAN_TEMPLATE` 的定位），
# 而产物结构、写作规则仍然全部来自技能包那三份文件（system prompt 负责）。
#
# ⚠️ 占位符必须**全部**被 `_build_prd_from_summary_prompt()` 注入 —— 少一个就会以字面量
# 进提示词，而模型看到 `{summary_json}` 这种字样照样能写出像样的结果（坑 #11）。
PRD_FROM_SUMMARY_HUMAN_TEMPLATE = (
    "结构化需求摘要（本次生成的**唯一**输入）：\n\n{summary_json}\n\n"
    "对话历史（可选，仅用于理解摘要里没写全的措辞）：\n\n{conversation}\n\n"
    "生成约束（优先级高于上面所有内容）：\n\n"
    "1. **只用用户提供的信息**。摘要与对话里没有的事实，一律不要补。\n"
    "2. **不要编造**：不写没给过的业务规则、角色权限、数据实体、指标数值、\n"
    "   外部依赖、时间计划。\n"
    "3. **缺失就标 `[待确认]`**：某一章或某一项在输入里找不到出处时，就地标注 `[待确认]`\n"
    "   并说明缺什么，不要用邻近内容顶替、也不要留空标题。\n"
    "4. 摘要字段与 PRD 章节的对应关系、每章必含子项、写作规范，全部按上面提供的技能包执行。\n"
)


# ---------------------------------------------------------------- PRD 双智能体（Writer + Reviewer）

# 审核用的 system prompt 名（规则与 JSON 判定契约都在这个文件里，不写进 Python）
PRD_REVIEW_PROMPT = "prd_review"

# 审核的 human message：判定依据（摘要）+ 待审对象（初稿）
PRD_REVIEW_HUMAN_TEMPLATE = (
    "判定依据之一：结构化需求摘要（初稿里凡是在下面两部分都找不到出处的技术约束、指标、\n"
    "合规要求，才算编造）：\n\n{summary_json}\n\n"
    "判定依据之二：对话历史（用户在这里**亲口说过**的事实与修正，与摘要同等有效 ——\n"
    "只出现在这里的细节不算编造，不要因此判不通过）：\n\n{conversation}\n\n"
    "待审的 PRD 初稿：\n\n{draft}\n\n"
    "按上面的规则输出 JSON 判定。"
)

# 重写上限。不设上限就是死循环烧钱；实测一次重写能修掉明显问题（缺章、空话、编造）。
PRD_MAX_REWRITES = 1


def _render_review_feedback(issues: Sequence[Mapping[str, Any]]) -> str:
    """把审核意见渲染成给 Writer 的修订要求。

    只给"改什么"，**不给"重写整篇"的暗示**：模型收到"重写"这类字眼时容易把已经写好的部分
    也一起改掉，而多数问题只涉及一两处。
    """
    lines = ["上一稿的审核意见（**只改这些问题，其余部分保持原样**）："]
    for index, issue in enumerate(issues, 1):
        section = str(issue.get("section") or "（未指明章节）")
        problem = str(issue.get("problem") or "").strip()
        suggestion = str(issue.get("suggestion") or "").strip()
        lines.append(f"{index}. {section}｜问题：{problem}｜要求：{suggestion}")
    return "\n".join(lines)


def _build_prd_from_summary_prompt(
    summary: Mapping[str, Any], history: Sequence[Mapping[str, str]] = ()
) -> str:
    """把结构化摘要（+ 可选对话历史）渲染成 PRD 生成的 human message。

    与 `SKILL_PRD_HUMAN_TEMPLATE` 的分工：那个吃的是"20 题表单 + 对话确认信息"，
    这个吃的是**已经结构化的 8 字段摘要** —— 后者本就与技能包的输入契约同形，
    所以不再需要模型自己从表单文本里对字段（少一层有损转换）。

    摘要用**缩进 JSON** 原样给出，不做二次加工：字段名就是技能包 schema 的字段名，
    模型照着 schema 找值即可；任何"帮它翻译一下"的改写都可能丢字段。
    """
    return render_prompt_text(
        PRD_FROM_SUMMARY_HUMAN_TEMPLATE,
        {
            "summary_json": json.dumps(summary, ensure_ascii=False, indent=2),
            "conversation": _render_conversation(history),
        },
    )


class SummarySyncError(RuntimeError):
    """回填失败：模型没给出可解析的 JSON 对象。

    刻意不返回"半个摘要"：调用方要靠整份结构做 diff，残缺的结果会让前端显示一堆假改动。
    """


@dataclass(frozen=True, slots=True)
class SummarySyncResult:
    """回填结果。**摘要本体 + 便于前端做 diff 与排查的元信息**。"""

    summary: dict[str, Any]
    """更新后的完整摘要（已按 schema 过滤、已按规则保住原值）。"""

    changed: tuple[str, ...]
    """内容发生变化的**顶层字段名**（前端可直接高亮这几项）。"""

    dropped_keys: tuple[str, ...]
    """模型自己发明、被我们丢掉的键（规则 3 的硬保证）。正常应为空，非空说明提示词没被遵守。"""

    truncated: bool
    """输出是否撞上单次上限被截断。截断时 JSON 多半也不完整，会先抛 `SummarySyncError`。"""

    finish_reason: str | None
    """厂商原话（`stop` / `length` …），排查用。"""


def _is_blank(value: Any) -> bool:
    """空值判定：`None` / 空串 / 空数组 / 空对象都算空。

    `False` 与 `0` **不算空** —— 虽然本摘要里几乎没有布尔与数字，但这符合直觉且不会被误判。
    """
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, dict, tuple)):
        return len(value) == 0
    return False


def _extract_json_object(text: str) -> dict[str, Any]:
    """从模型输出里抠出**第一个完整的 JSON 对象**。

    只做两件事，都不猜内容：
    1. 去掉 ```` ```json ```` 围栏（模型很爱加，而围栏会让整体 `json.loads` 失败）；
    2. 从第一个 `{` 起用 `raw_decode` 解析 —— 它能**在 JSON 结束处停下**，
       所以后面的解释文字不会影响解析（比"找最后一个 `}`"稳得多，正文里出现 `}` 也不会崩）。

    Raises:
        SummarySyncError: 找不到 `{`，或解析失败。错误信息带上输出开头，便于定位。
    """
    stripped = text.strip()
    if stripped.startswith("```"):
        # ```json\n{...}\n``` → 取第一行之后到最后一个 ``` 之前
        lines = stripped.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()

    start = stripped.find("{")
    if start < 0:
        raise SummarySyncError(f"模型输出里没有 JSON 对象，开头是：{stripped[:120]!r}")

    try:
        parsed, _end = json.JSONDecoder().raw_decode(stripped[start:])
    except json.JSONDecodeError as exc:
        raise SummarySyncError(
            f"模型输出不是合法 JSON：{exc.msg}（位置 {exc.pos}），开头是：{stripped[start:start + 120]!r}"
        ) from exc

    if not isinstance(parsed, dict):
        raise SummarySyncError(f"模型输出的 JSON 不是对象，而是 {type(parsed).__name__}")
    return parsed


def _allowed_keys(schema: Mapping[str, Any]) -> tuple[set[str], dict[str, set[str]]]:
    """从 `field-schema.json` 取出**顶层允许的键**，以及**数组元素允许的键**。

    元素级也要取：`mvp_features` / `ui_pages` 的元素写的是 `additionalProperties: false`，
    模型很可能顺手加个 `user_value`、`modules` 之类的键 —— 那些会让这份 JSON 不再符合 schema。

    返回 `(顶层键集合, {顶层键: 元素允许键集合})`；解析不出结构的项不进第二个字典（即不限制）。
    """
    top = set((schema.get("properties") or {}).keys())
    element_keys: dict[str, set[str]] = {}
    for name, spec in (schema.get("properties") or {}).items():
        if not isinstance(spec, Mapping):
            continue
        items = spec.get("items")
        if not isinstance(items, Mapping):
            continue
        # 元素是 anyOf（字符串或对象）时，取其中带 properties 的那一支
        candidates = items.get("anyOf") if isinstance(items.get("anyOf"), list) else [items]
        for candidate in candidates:
            if isinstance(candidate, Mapping) and isinstance(candidate.get("properties"), Mapping):
                element_keys[name] = set(candidate["properties"].keys())
                break
    return top, element_keys


def _filter_by_schema(
    candidate: Mapping[str, Any], top: set[str], element_keys: Mapping[str, set[str]]
) -> tuple[dict[str, Any], list[str]]:
    """按 schema 过滤模型输出：丢掉 schema 之外的顶层键与元素键（规则 3 的硬保证）。

    **不碰值**，只删键 —— 值的取舍由 `_merge_summary()` 负责。
    """
    kept: dict[str, Any] = {}
    dropped: list[str] = []
    for key, value in candidate.items():
        if key not in top:
            dropped.append(key)
            continue
        allowed = element_keys.get(key)
        if allowed and isinstance(value, list):
            cleaned: list[Any] = []
            for index, item in enumerate(value):
                if not isinstance(item, Mapping):
                    cleaned.append(item)
                    continue
                extra = [k for k in item if k not in allowed]
                if extra:
                    dropped.extend(f"{key}[{index}].{k}" for k in extra)
                cleaned.append({k: v for k, v in item.items() if k in allowed})
            kept[key] = cleaned
            continue
        kept[key] = value
    return kept, dropped


def _merge_summary(
    current: Mapping[str, Any], updated: Mapping[str, Any]
) -> tuple[dict[str, Any], list[str]]:
    """把模型给的新值合并进当前摘要，返回 `(合并结果, 改动字段名)`。

    两条规则**在这里落地**（不指望模型永远听话）：

    1. **空值一律不写进结果**（规则 2「不确定保持原值」的硬保证）：
       模型把某个字段返回成空（空串 / 空数组 / 空对象）→ 既**不覆盖**原有的非空值，
       也**不会**把原本不存在的键补成一个空值 —— 后者会在前端 diff 里显示成一条假改动
       （"v2_features：无 → []"），实测踩到过。
    2. **模型没返回的键保持原值**（规则 1/2）：所以这是**合并**，不是替换。

    注意：模型**返回了非空的、但内容不同的值**时是接受的 —— 那正是它认为"用户明确改了"的情况，
    是否真的对由用户在前端看 diff 决定（本函数的职责是给出完整结构，不做业务判断）。
    """
    merged: dict[str, Any] = dict(current)
    changed: list[str] = []

    for key, value in updated.items():
        if _is_blank(value):
            continue  # 空值一律不写（见上面第 1 条）
        if current.get(key) != value:
            changed.append(key)
        merged[key] = value

    # 模型明确说了"这个字段现在没有了"（例如清空 v2_features）时**不改**：
    # 空值与"没返回"在 JSON 里长得一样，这里选择保守 —— 删字段属于人工操作，
    # 不该由一次回填顺手做掉。
    return merged, changed


def _render_conversation(history: Sequence[Mapping[str, str]]) -> str:
    """把对话历史渲染成"人读得懂"的文本。

    ⚠️ AI 侧的原始 `content` 是**一个 JSON 信封**（含 `message` / `questions` / `stage_status`）。
    直接把它整段喂给模型，噪音很大且容易被照着模仿成"也输出 JSON 信封"。
    所以这里尽量取信封里的 `message` 正文（取不到就原样用），与前端 `extractStreamingMessage()`
    的判据保持一致。
    """
    if not history:
        return "（没有对话历史）"

    lines: list[str] = []
    for turn in history:
        role = (turn.get("role") or "").strip().lower()
        content = (turn.get("content") or "").strip()
        if role == "ai":
            try:
                envelope = json.loads(content)
            except (json.JSONDecodeError, TypeError):
                pass
            else:
                if isinstance(envelope, dict) and isinstance(envelope.get("message"), str):
                    content = envelope["message"].strip()
        speaker = "用户" if role == "user" else "助手"
        lines.append(f"【{speaker}】{content or '（空）'}")
    return "\n\n".join(lines)


def _noted_bundle(artifact: str, *, paths: Sequence[str] | None = None) -> SkillBundle:
    """取技能包，并把**实际注入的清单**登记到本次 run 的汇总里。

    为什么登记放在这一层而不是各个调用点：三份产物 × 生成与优化，取 bundle 的地方有六七处，
    在每个调用点手写一遍 `record_injected_skills(...)` 一定会漏 —— 而漏掉的表现是
    `run_summary.injected_skills` 少一项，没有任何报错，几周后才会有人发现。

    Args:
        paths: 只登记这几份（给"只注入了其中一份"的场景，例如摘要回填只注入 `role: schema`）。
    """
    bundle = load_skill_bundle(artifact)
    record_injected_skills(bundle_to_injected_meta(bundle, paths=paths))
    return bundle


def _skill_system_prompt(artifact: str = SKILL_PRD_ARTIFACT) -> str:
    """按 `config/skills.yaml` 的绑定，把技能包的文件拼成一份完整 system prompt。

    每个文件前加一行来源标题（由 `skill_loader.compose_prompt_sections()` 加），
    便于排查"这句话到底来自哪份文件"—— 技能包是多文件拼装，缺了这行，出问题时只能靠猜。

    ⚠️ **不要在这里重排 section 顺序**：`compose_prompt_sections()` 返回的字典顺序
    **就是**提示词顺序（instructions → template → reference → schema），
    它逐字对应改造前那份 `SKILL_PRD_ARTIFACTS` 的顺序 —— 改了就等于偷偷改了提示词。

    Raises:
        SkillConfigError: 注册表 / skill.yaml 有问题，或该产物一件可注入的文件都没有。
            后者**必须报错而不是返回空 prompt**：那等于让模型在不带任何规范的情况下裸奔，
            而产物结构、编号规则全在里面。
    """
    bundle = _noted_bundle(artifact)
    sections = compose_prompt_sections(bundle)
    if not sections:
        raise SkillConfigError(
            f"artifact {artifact!r} 没有可注入的技能文件（绑定的 skill：{list(bundle.skills) or '无'}）——"
            "检查 config/skills.yaml 的 bindings，以及 skills/*/skill.yaml 的 enabled 与 artifacts"
        )
    header = (
        "===== 技能包 "
        + "、".join(f"skills/{skill_id}/" for skill_id in bundle.skills)
        + "，按顺序阅读并遵守 ====="
    )
    return "\n\n".join([header, *sections.values()])


#: `DocKind` → 它**优化路径**的 Job artifact。
#:
#: ⚠️ 由 `job_models.ARTIFACT_DOC_KIND`（**唯一的映射处**）反查得到，
#: **不要手拼 `f"optimize-{kind}"`** —— 接口文档那一份叫 `optimize-api-docs`（带 `docs`），
#: 手拼出来的 `optimize-api` 在注册表里根本不存在，会在优化时抛 `SkillConfigError`
#: （实测踩到：浏览器里点「优化这一节」直接失败）。
_OPTIMIZE_ARTIFACT_BY_KIND: dict[str, str] = {
    kind: artifact
    for artifact, kind in ARTIFACT_DOC_KIND.items()
    if is_optimize_artifact(artifact)
}

#: 角色 → 注入块里那一段的小标题（给人/给模型都更清楚"这是规范还是示例"）。
_SKILL_ROLE_TITLES: dict[str, str] = {
    "instructions": "本技能包的工作流",
    "template": "结构模板",
    "reference": "团队规范与写作要点",
    "schema": "字段契约",
    "example": "参考示例（学写法，不要照抄业务内容）",
}


def _skill_human_block(artifact: str) -> str:
    """把技能包拼成一段**附加在 human message 末尾**的注入块。

    与 `_skill_system_prompt()` 的分工：那个把技能包当 system prompt（PRD 那条路，
    技能包就是完整规范）；这个只是**补充**到 human message 上 —— 接口文档与提示词套件的
    结构规范仍在 system prompt（`gen_api.md` / `gen_prompts.md`），技能包补的是
    团队约定与真实示例（原来由 RAG 检索 + 人工勾选提供的那部分）。

    Returns:
        空字符串 = 该产物没有绑定技能（正常状态，调用方直接不加这一段）。
    """
    bundle = _noted_bundle(artifact)
    sections = compose_prompt_sections(bundle)
    if not sections:
        return ""
    file_count = len(bundle.artifacts)
    header = (
        f"===== 本产物注入的技能包规范与示例"
        f"（skills/{'、'.join(bundle.skills)}/，共 {file_count} 份文件，全量给出）====="
    )
    blocks = [header]
    for role, text in sections.items():
        blocks.append(f"【{_SKILL_ROLE_TITLES.get(role, role)}】\n{text}")
    return "\n\n".join(blocks)


def _skill_schema_text(artifact: str = SKILL_PRD_ARTIFACT) -> str:
    """取技能包里 `role: schema` 那份文件的**原文**（技能包的输入契约）。

    字段集合的**唯一真相**在 `field-schema.json`：这里读它的原文注入 human message，
    不在 Python 里再列一遍字段 —— 两处必然漂移（`HANDOFF.md` §4 坑 #10）。

    Raises:
        SkillConfigError: 绑定的技能里没有 `role: schema` 的文件。
            摘要回填拿不到字段定义就只能瞎编键名，属于必须尽早暴露的配置错误。
    """
    bundle = load_skill_bundle(artifact)
    for item in bundle.artifacts:
        if item.role == "schema":
            # 只登记真正注进去的那一份：整包登记会让"注入量"虚高（这条路上只用 schema）
            record_injected_skills(bundle_to_injected_meta(bundle, paths=[item.path]))
            return item.content
    raise SkillConfigError(
        f"artifact {artifact!r} 绑定的技能里没有 role: schema 的文件"
        f"（绑定的 skill：{list(bundle.skills) or '无'}）—— 摘要回填缺字段定义"
    )


# 产物名（人读）。给 human message 与日志用。
DOC_TITLES: dict[DocKind, str] = {
    "prd": "PRD（产品需求文档）",
    "api": "接口文档",
    "prompts": "提示词套件",
}


@dataclass(frozen=True, slots=True)
class DocumentScope:
    """一次生成调用的**分片范围**，对应三个占位符。

    | 字段 | 占位符 | 含义 |
    | --- | --- | --- |
    | `outline` | `{doc_outline}` | 完整结构清单（章节标题 + 风格 + 输入来源） |
    | `scope` | `{generate_scope}` | **本次**要生成的部分 |
    | `spec` | `{scope_spec}` | 本次范围内每一章 / 每一步的详细规格：要点、风格、字数上限 |

    存在理由是 `gen_common.md` 的三条硬性规则（只输出 `{generate_scope}` 指定的部分、
    不为未生成的章节留占位标题、严格按 `{scope_spec}` 的字数上限）**全靠这三个占位符成立**。
    把它们做成显式参数而不是可选字符串，是为了让"忘了传"变成**类型上不可能**，
    而不是变成一句静默留在 prompt 里的 `{generate_scope}`（坑 #11）。
    """

    outline: str
    scope: str
    spec: str

    @classmethod
    def whole_document(cls, note: str = "") -> DocumentScope:
        """不设限的单次生成：一次调用产出整份产物。

        章节清单**故意不在这里重复** —— 它已经内嵌在 `gen_*.md` 里，
        这里只负责告诉模型"这次不切片"以及"按提示词内嵌的模板执行"。

        ⚠️ **能不能装下要看是第几份产物**（端到端实测，`HANDOFF.md` §2）：
        PRD 整份 6723 字符**装得下、不截断**；接口文档 17257 字符、提示词套件 18354 字符
        **必然撞上 `llm_max_tokens` 被截断**（`finish_reason=length`）。
        所以对后两份来说这是**过渡用法**，正式流程应传具体的分片范围 ——
        那件事已从"待拍板"升级为必做（`HANDOFF.md` §6 第 8 项）。
        调用方**无论如何都该传 `outcome`**，把 `truncated` 告诉用户。
        """
        return cls(
            outline="完整文档（本次不分片）。章节清单以本提示词内嵌的模板为准。",
            scope="全文",
            spec=note or "按本提示词内嵌的模板与字数要求执行，不额外限制本次范围。",
        )


def missing_placeholders(kind: DocKind, values: Mapping[str, str]) -> list[str]:
    """该产物**声明**了、但本次没注入的占位符。

    这是坑 #11 的守卫：`str.replace` 对缺失键**静默跳过**，缺注入不会报错，
    只会让 `{generate_scope}` 之类的字面量留在 system prompt 里 —— 而模型看到这种字样
    照样能输出像样的结果，于是"看起来正常"掩盖了"输入根本没送到"。

    声明的集合从**提示词文件本身**解析（`core.prompts.declared_placeholders`），
    不在 Python 里维护第二份清单：两份来源手工同步的错误率是 100%（坑 #10）。

    ⚠️ `gen_common.md` 的清单表把 `{prd_content}` / `{api_content}` 标为「仅第 2、3 份产物有」，
    但声明集合是**并集**，所以对 PRD 也会报出这两项。判断时要知道这一点：
    本模块一律注入全部声明项（多注入的代价是零 —— 没出现的占位符替换是空操作），
    因此正常情况下这个函数**不会**报出 PRD 的 `prd_content`。
    """
    declared = set(declared_placeholders("gen_common")) | set(declared_placeholders(f"gen_{kind}"))
    return sorted(declared - set(values))


# 组装后的 system prompt 超过这个字符数就告警。
# ⚠️ **这不是模型上限**（不同 provider / 型号差别很大，配置里也没有窗口大小），
# 只是"该看一眼了"的经验阈值。触发时八成是同一个原因：
# **`str.replace` 会替换每一处**，而上游产物占位符在提示词里被引用多次 ——
# 实测 `{prd_content}` 在 `gen_common.md` + `gen_api.md` 里共出现 **5 次**，
# 于是整份 PRD 被内联 5 遍（2723 字的上游夹具 → 13615 字）。
# **真机实测**（端到端跑通时）：真实 PRD 6723 字符 → `gen_api` 的 system prompt 48670 字符，
# 告警如期触发。结论修正：不会"撑爆上下文"（当前模型装得下、生成成功），
# 真实代价是**白烧约 2 万输入 token + 同一份 PRD 在 5 个位置各说一遍**。
_PROMPT_WARN_CHARS = 30_000


class DocumentService:
    """三份产物的生成与优化。

    用法（**每次调用只产出一个分片**，切分由调用方决定）::

        service = DocumentService()
        async for chunk in service.generate_prd_stream(form=form, known_info=known):
            ...  # chunk 是裸文本片段，SSE 帧由 api 层包装

    ⚠️ 没做的见模块 docstring 顶部那张表。特别地：**这不是后台任务**，
    也**不落任何状态** —— 结果要由调用方（将来的任务层）负责持久化。
    """

    def __init__(
        self,
        model: BaseChatModel | None = None,
        settings: Settings | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._model = model
        # 审核智能体**默认复用同一个模型实例**（同一套 API/Key、同一个 model），
        # 只有显式配了 `PRD_REVIEW_LLM_*` 才会另外构造一个（见 `review_model`）。
        # 注入 `model=` 的测试也同样复用 —— 测试不该被这条分叉影响。
        self._review_model = model

    @property
    def model(self) -> BaseChatModel:
        """惰性构造模型客户端。

        惰性很重要：没配 Key 时只有**真正调用**才报错，
        所以导入链、健康检查不会被 Key 缺失阻断（与 `ConversationService` 一致）。
        """
        if self._model is None:
            self._model = get_llm(streaming=True, settings=self._settings)
        return self._model

    @property
    def review_model(self) -> BaseChatModel:
        """审核智能体（Reviewer）用的模型。

        **默认与写手是同一个模型**：同一套 API（同一个 Key、同一个 endpoint），
        只有提示词不同。但换个提示词改变不了这个模型的盲区 —— 它仍然是自己审自己。
        所以这里留了一个开关：配了 `PRD_REVIEW_LLM_PROVIDER` / `PRD_REVIEW_LLM_MODEL`
        就换成另一个模型（同厂换 model 名即可，**Key 不用多配一份**）。

        ⚠️ 配错（比如换了 provider 却没配那个 Key）**不能让生成失败**：
        审核是增强项，这里回落到写手的模型并写 warning。回落本身不改变流程，
        但意味着这一轮又变成自查 —— 所以必须留日志，别让配置错误静默降级。
        """
        if self._review_model is None:
            # 没配开关 → 就是写手那个模型（同一套 API/Key、**同一个实例**）。
            if (
                self._settings.review_llm_provider is None
                and self._settings.review_llm_model is None
            ):
                self._review_model = self.model
                return self._review_model
            try:
                self._review_model = get_llm(
                    streaming=False, settings=self._settings.review_llm_settings
                )
            except LlmConfigError as exc:
                logger.warning(
                    "审核智能体的模型不可用（%s），本轮回落到写手同一个模型（即自查）", exc
                )
                self._review_model = self.model
        return self._review_model

    # ------------------------------------------------------------ 模板加载与拼接

    def system_template(self, kind: DocKind) -> str:
        """取该产物的 system prompt **模板**（未渲染，占位符原样保留）。

        需要"未渲染版本"的场景（比如展示给维护者看、或做 diff）用这个；
        真正发给模型请用 `render_system_prompt()`。
        """
        try:
            return SYSTEM_TEMPLATES[kind]
        except KeyError as exc:  # pragma: no cover - DocKind 是 Literal，类型上就挡住了
            raise ValueError(f"未知产物类型：{kind!r}；可选 {sorted(SYSTEM_TEMPLATES)}") from exc

    def render_system_prompt(self, kind: DocKind, values: Mapping[str, str]) -> str:
        """渲染 system prompt：`gen_common + gen_{kind}`，**顺序不可颠倒**。

        组装与渲染都走 `core.prompts.build_system_prompt` —— 与
        `scripts/validate_prompts.py` 是同一份实现，不在服务层另拼一遍
        （提示词输入有两处实现就必然漂移，坑 #10）。

        渲染用 `str.replace`，**禁用 `str.format`**：提示词里除了运行时占位符，
        还有给人看的格式记号（`{模块缩写}{3位序号}`、`{id}`），`str.format` 会把后者
        也当占位符名 —— 实测在 `gen_api.md` 上抛 `KeyError`（坑 #1）。
        `build_system_prompt` 已经保证这一点，这里只是**不要绕过它**。
        """
        return build_system_prompt(f"gen_{kind}", values)

    def render_human_prompt(self, values: Mapping[str, str]) -> str:
        """渲染本次生成的 human message（同样是 `str.replace`）。"""
        return render_prompt_text(GEN_HUMAN_TEMPLATE, values)

    def generation_values(
        self,
        *,
        form: Mapping[str, str] | None = None,
        known_info: str = _EMPTY,
        open_questions: str = _EMPTY,
        conflicts: str = _EMPTY,
        scope: DocumentScope | None = None,
        prd_content: str = _EMPTY,
        api_content: str = _EMPTY,
    ) -> dict[str, str]:
        """组装提示词占位符的值 —— **该产物声明的占位符一个都不能少**。

        漏一个的后果不是报错，而是 `str.replace` 静默跳过（坑 #11）。
        所以这里一次性把 `gen_common.md` 声明的全部占位符给全（10 个），
        不适用的用 `_EMPTY` 占位。用完调 `missing_placeholders()` 兜底。

        Args:
            form: 表单作答，键 = 题目 **id**。空字典表示"没有表单输入"。
            known_info: 对话确认的结构化信息。**优先级高于 `form_summary`**
                （`gen_common.md`：用户明确修正过表单答案时以对话为准）。
                ⚠️ 缺它会直接导致生成阶段自己编 —— 实测缺 S3 优先级时 7 条功能全标 P0
                （坑 #4）。这是数据流要求，提示词救不了。
            open_questions: 用户跳过的项，**必须原样进开放问题章节**。
            conflicts: 已发现的矛盾及结论。
            scope: 分片范围。不传 = 单次生成整份（`DocumentScope.whole_document()`）。
            prd_content: 已通过审核的 PRD 全文。仅 api / prompts 用得上。
            api_content: 已通过审核的接口文档全文。仅 prompts 用得上。
        """
        resolved_form = form or {}
        questions = load_questions().all_questions
        product_name = (resolved_form.get("product_name") or "").strip() or "（未命名）"
        resolved_scope = scope or DocumentScope.whole_document()

        return {
            # 系统侧
            "product_name": product_name,
            "form_summary": format_form_data(resolved_form, questions) if resolved_form else _EMPTY,
            "known_info": known_info,
            "open_questions": open_questions,
            "conflicts": conflicts,
            # 分片三件套
            "doc_outline": resolved_scope.outline,
            "generate_scope": resolved_scope.scope,
            "scope_spec": resolved_scope.spec,
            # 上游产物
            "prd_content": prd_content,
            "api_content": api_content,
        }

    # ------------------------------------------------------------ 三个生成方法

    async def generate_prd_stream(
        self,
        *,
        form: Mapping[str, str],
        known_info: str = _EMPTY,
        open_questions: str = _EMPTY,
        conflicts: str = _EMPTY,
        scope: DocumentScope | None = None,
        outcome: StreamOutcome | None = None,
        use_skill: bool | None = None,
    ) -> AsyncIterator[str]:
        """生成 PRD（默认按 `skills/prd-generator/` 的 6 章结构）。

        逐段产出**裸文本片段**（不是 SSE 帧）—— 传输协议由 `api` 层包装，
        服务层不感知 HTTP（与 `ConversationService` 一致）。

        **整份能一次生成完**（实测几千字符，无截断，见
        `backend/validation_out/prd_skill.txt`）。撞上限的是**接口文档与提示词套件**，
        所以真正要分片的是那两份，不是 PRD（`HANDOFF.md` §4 坑 #18）。

        Args:
            outcome: 传进来就会在流结束时填入 `finish_reason`，
                调用方据此判断**是否被输出上限截断**。不传则什么都不记。
            use_skill: 不传则取 `settings.prd_use_skill`（**默认 True**，走技能包）。
                传 `False` 才走 `gen_prd.md` 的老结构（15 章 + 2 附录）——
                那条路只作回退保留，两套结构的章节号互不兼容，不要混着用。
        """
        values = self.generation_values(
            form=form,
            known_info=known_info,
            open_questions=open_questions,
            conflicts=conflicts,
            scope=scope,
        )

        system, human_template = self.prd_prompts(values, use_skill)
        async for chunk in self._stream_document(
            "prd",
            values,
            system=system,
            human=render_prompt_text(human_template, values),
            outcome=outcome,
        ):
            yield chunk

    def prd_prompts(
        self, values: Mapping[str, str], use_skill: bool | None = None
    ) -> tuple[str, str]:
        """PRD 该用哪套提示词：返回 `(system_prompt, human_template)`。

        **分支只在这一处判断**（以前写在 `generate_prd_stream` 的循环里）：
        分片计划、校验脚本、前端文案都要知道"这次到底走哪套结构"，
        各判一次就必然漂移 —— 实测过的坑是"文档说 15 章、产物是 6 章"。

        Args:
            values: 本次生成的占位符取值。回退路径要用它渲染 `gen_common + gen_prd`；
                技能包路径的 system prompt 是静态文件，用不到它（human template 仍需渲染）。
            use_skill: 不传取 `settings.prd_use_skill`。

        注意返回的 human template **还没有渲染**，调用方要自己过 `render_prompt_text`
        （技能包与内置模板的占位符集合不同，不能互换）。
        """
        resolved = self._settings.prd_use_skill if use_skill is None else use_skill
        if resolved:
            return _skill_system_prompt(SKILL_PRD_ARTIFACT), SKILL_PRD_HUMAN_TEMPLATE
        return self.render_system_prompt("prd", values), GEN_HUMAN_TEMPLATE

    async def generate_api_docs_stream(
        self,
        *,
        form: Mapping[str, str],
        prd_content: str,
        known_info: str = _EMPTY,
        open_questions: str = _EMPTY,
        conflicts: str = _EMPTY,
        scope: DocumentScope | None = None,
        outcome: StreamOutcome | None = None,
    ) -> AsyncIterator[str]:
        """从**已通过审核的 PRD** 推导接口文档。

        **`prd_content` 必填且不能是空白。** 这不是形式主义：链条约束要求
        「接口文档的每个接口都必须能追溯到 PRD 的功能需求」（`gen_common.md` §三份产物），
        没有 PRD 可读，模型只能凭空造接口 —— 而造出来的东西**看起来完全像真的**。
        与其生成一份不可追溯的文档，不如在这里就失败。

        ⚠️ **不传 `scope` 时（整份生成）实测必然被截断**：输出撞上 `LLM_MAX_TOKENS=8192`，
        末尾停在半行表格（`finish_reason=length`，`HANDOFF.md` §4 坑 #18）。
        所以调用方**必须**传 `outcome` 并把 `truncated` 告诉用户，
        或者按章传 `scope` 分片生成。
        """
        _require_text(prd_content, "prd_content", "接口文档必须从 PRD 推导")

        values = self.generation_values(
            form=form,
            known_info=known_info,
            open_questions=open_questions,
            conflicts=conflicts,
            scope=scope,
            prd_content=prd_content,
        )
        async for chunk in self._stream_document(
            "api", values, skill_artifact="api-docs", outcome=outcome
        ):
            yield chunk

    async def generate_prompts_stream(
        self,
        *,
        form: Mapping[str, str],
        prd_content: str,
        api_content: str = _EMPTY,
        known_info: str = _EMPTY,
        open_questions: str = _EMPTY,
        conflicts: str = _EMPTY,
        scope: DocumentScope | None = None,
        outcome: StreamOutcome | None = None,
    ) -> AsyncIterator[str]:
        """生成提示词套件（消费 PRD + 接口文档）。

        产出是**多文件**格式，用 `=== FILE: <相对路径> ===` 分隔各文件
        （`docs/提示词套件模板.md`）—— 调用方负责按这个分隔行切分。

        `api_content` 允许为空：`scripts/validate_prompts.py` 验证时用的就是
        `api_content or "（尚无）"`，保持同一行为。但 `prd_content` **不能为空**，
        理由与 `generate_api_docs_stream` 相同。

        ⚠️ 提示词套件的每一步都必须能追溯到 PRD 的 FR 编号；`prd_content` 里
        没有 FR 编号时（比如只生成了前几章），产物会大量 `[待确认]`。
        """
        _require_text(prd_content, "prd_content", "提示词套件必须从 PRD 推导")

        values = self.generation_values(
            form=form,
            known_info=known_info,
            open_questions=open_questions,
            conflicts=conflicts,
            scope=scope,
            prd_content=prd_content,
            api_content=api_content or _EMPTY,
        )
        async for chunk in self._stream_document(
            "prompts", values, skill_artifact="prompts", outcome=outcome
        ):
            yield chunk

    # ------------------------------------------------------------ 对话 → 结构化摘要回填

    async def sync_requirements_summary(
        self,
        *,
        summary: Mapping[str, Any],
        history: Sequence[Mapping[str, str]],
    ) -> SummarySyncResult:
        """把对话里用户**明确说过**的补充与修正回填进结构化摘要。

        这是**非流式**的一次调用（与三个 `generate_*_stream` 不同），三个理由：

        1. 输出是一小段 JSON，分片/流式没有意义；
        2. 调用方要的是**整份结构**做 diff，半截 JSON 没法用；
        3. 出错要能干净地报错（流式接口的异常会变成"200 + 流里一个 error"，见坑 #18 那类形状）。

        Args:
            summary: 当前结构化摘要，形状以 `field-schema.json` 为准。
            history: 对话历史，按时间正序，元素形如 `{"role": "user"|"ai", "content": "…"}`。
                AI 侧既可以是原始 JSON 信封，也可以只给 `message` 正文 —— `_render_conversation()`
                会把信封剥出来。

        Returns:
            `SummarySyncResult`：更新后的完整摘要 + 改动字段 + 被丢掉的越界键 + 截断信息。

        Raises:
            SummarySyncError: 模型没给出可解析的 JSON 对象（含截断导致的残缺 JSON）。
            LlmConfigError: 没配 Key（由 `model` 属性抛出，路由层转 503）。
        """
        schema_text = _skill_schema_text()
        try:
            schema = json.loads(schema_text)
        except json.JSONDecodeError as exc:  # pragma: no cover - schema 是仓库内的静态文件
            raise SummarySyncError(f"技能包的 field-schema.json 不是合法 JSON：{exc}") from exc

        values = {
            "field_schema": schema_text,
            "current_summary": json.dumps(summary, ensure_ascii=False, indent=2),
            "conversation": _render_conversation(history),
        }
        messages: list[BaseMessage] = [
            SystemMessage(content=load_prompt(SYNC_SUMMARY_PROMPT)),
            HumanMessage(content=render_prompt_text(SYNC_SUMMARY_HUMAN_TEMPLATE, values)),
        ]

        # 摘要回填单独一个 step，便于在 run_summary 里和生成类调用区分
        response = await track(self.model, LlmStep.SUMMARY_SYNC).ainvoke(messages)
        text = message_text(response)
        reason = finish_reason_of(response)
        truncated = is_truncated(reason)

        candidate = _extract_json_object(text)
        top_keys, element_keys = _allowed_keys(schema)
        filtered, dropped = _filter_by_schema(candidate, top_keys, element_keys)
        merged, changed = _merge_summary(summary, filtered)

        if dropped:
            # 越界键说明模型没守规则 3。不报错（结果仍可用），但**必须留痕** ——
            # 静默丢弃会让提示词退化到没人发现。
            logger.warning(
                "回填摘要时模型返回了 schema 之外的键，已丢弃：%s", "、".join(dropped)
            )

        return SummarySyncResult(
            summary=merged,
            changed=tuple(changed),
            dropped_keys=tuple(dropped),
            truncated=truncated,
            finish_reason=reason,
        )

    # ------------------------------------------------------------ 从结构化摘要生成 PRD

    async def generate_prd_from_summary_stream(
        self,
        *,
        summary: Mapping[str, Any],
        history: Sequence[Mapping[str, str]] = (),
        outcome: StreamOutcome | None = None,
    ) -> AsyncIterator[str]:
        """**直接从结构化摘要生成 PRD**，流式产出裸文本片段。

        与 `generate_prd_stream()` 的区别（两条路径并存，别混用）：

        | | 输入 | 前置环节 |
        | --- | --- | --- |
        | `generate_prd_stream()` | 20 题表单作答 + `known_info` | 表单 → 对话澄清 |
        | 本方法 | 技能包 8 字段的结构化摘要 | 结构化录入（→ 可选回填） |

        本方法的输入**与技能包的输入契约同形**，所以：
        - 技能包第一步「校验输入」第一次真正可执行（缺必填项在摘要里一眼可见）；
        - 少了一层"从表单文本里猜字段"的有损转换。

        代价：`[待确认]` 会变多 —— 原来由对话澄清补齐的细节，这条路径没有那一步。
        所以约束里明确要求"缺失就地标注"，而不是让模型拿邻近内容顶替（坑 #3）。

        system prompt 仍走技能包那四份文件（`_skill_system_prompt()`）；生成约束在
        human message 里（见 `PRD_FROM_SUMMARY_HUMAN_TEMPLATE`）。
        """
        if not summary:
            raise ValueError("summary 不能为空 —— 这条路径的唯一输入就是结构化摘要")

        system, _ = self.prd_prompts(self.generation_values(form={}), use_skill=True)
        async for chunk in self._stream_document(
            "prd",
            self.generation_values(form={}),
            system=system,
            human=_build_prd_from_summary_prompt(summary, history),
            outcome=outcome,
        ):
            yield chunk

    # ------------------------------------------------------------ PRD 双智能体

    async def generate_prd_with_review_events(
        self,
        *,
        summary: Mapping[str, Any],
        history: Sequence[Mapping[str, str]] = (),
        outcome: StreamOutcome | None = None,
    ):
        """**双智能体**：Writer 写初稿 → Reviewer 审 → 有问题就重写（上限 `PRD_MAX_REWRITES`）。

        产出的是**结构化事件**，不是裸文本：

        - `("stage", {"stage": "writing"|"reviewing"|"rewriting"|"done", "round": n, ...})`
        - `("chunk", "文本片段")`

        ⚠️ **刻意不复用 `generate_prd_from_summary_stream()` 的裸文本契约** —— 那个被
        `split_check.py` / `skill_prd_check.py` / `validate_prompts.py` 三个脚本依赖，
        把它改成事件流会一次弄坏三处。两条并存，由路由选择用哪条。

        三个刻意的取舍（默认值）：
        1. **重写上限 1 次**：不设上限就是死循环烧钱；实测一次重写能修掉明显问题。
        2. **达到上限仍不合格 → 照样输出终稿**，并把审核意见带在 `done` 事件里（不静默吞掉）。
        3. **审核输出解析不了 → 按"通过"处理**（`_review_prd()` 返回 `None`）：
           宁可漏审一次，也不要因为判不出来就把用户的稿子重写一遍。

        **实测**（deepseek-chat，同一份输入）：审核通过 = 2 次调用 / 5.6 秒；
        触发重写 = 3 次调用 / 10.5 秒（原来单次生成是 1 次 / 约 5.7 秒）。
        """
        if not summary:
            raise ValueError("summary 不能为空 —— 这条路径的唯一输入就是结构化摘要")

        values = self.generation_values(form={})
        system, _ = self.prd_prompts(values, use_skill=True)
        base_human = _build_prd_from_summary_prompt(summary, history)
        # 透给前端：这一稿是**哪个模型**审的。这条开关是环境变量，
        # 服务换个方式重启就会静默回落成"自己审自己" —— 界面上写着模型名，
        # 用户一眼就能发现第二双眼睛没了，不必翻日志。
        review_model_name = self._settings.review_llm_active_model

        round_no = 0
        issues: list[dict[str, Any]] | None = None
        while True:
            round_no += 1
            first = round_no == 1
            yield (
                "stage",
                {
                    "stage": "writing" if first else "rewriting",
                    "round": round_no,
                    # 重写时把上一轮的审核意见一并带出去：前端可以显示"在改什么"
                    "issues": [] if first else (issues or []),
                    "detail": "正在撰写初稿…" if first else f"审核发现问题，正在重写第 {round_no} 稿…",
                },
            )

            human = base_human
            if not first and issues:
                human = base_human + "\n\n" + _render_review_feedback(issues)

            parts: list[str] = []
            async for chunk in self._stream_document(
                "prd",
                values,
                system=system,
                human=human,
                outcome=outcome,
                step=LlmStep.PRD_WRITER if first else LlmStep.PRD_REWRITE,
            ):
                parts.append(chunk)
                yield ("chunk", chunk)
            draft = "".join(parts)

            if round_no > PRD_MAX_REWRITES:
                # 预算用完：不再审、也不再改，直接把稿子交出去并附上已知问题
                yield (
                    "stage",
                    {
                        "stage": "done",
                        "round": round_no,
                        "issues": issues or [],
                        "review_model": review_model_name,
                    },
                )
                return

            yield ("stage", {"stage": "reviewing", "round": round_no, "detail": "正在审核初稿…"})
            issues = await self._review_prd(summary=summary, draft=draft, history=history)

            if issues is None:
                # 审核输出解析不了 —— 按通过处理（见 docstring 第 3 条）
                yield ("stage", {"stage": "done", "round": round_no, "issues": [], "review_skipped": True, "review_model": review_model_name})
                return
            if not issues:
                yield (
                    "stage",
                    {
                        "stage": "done",
                        "round": round_no,
                        "issues": [],
                        "review_model": review_model_name,
                    },
                )
                return

    async def _review_prd(
        self,
        *,
        summary: Mapping[str, Any],
        draft: str,
        history: Sequence[Mapping[str, str]] = (),
    ) -> list[dict[str, Any]] | None:
        """审一遍初稿。返回**待修问题清单**；返回 `None` 表示"判定不可用，当通过处理"。

        `[]` = 审过了、没问题；`None` = 没审出来（解析失败）。两者**必须分开** ——
        合并成一个空列表的话，"解析失败"就会被当成"通过"，而调用方无从察觉。
        """
        messages: list[BaseMessage] = [
            SystemMessage(content=load_prompt(PRD_REVIEW_PROMPT)),
            HumanMessage(
                content=render_prompt_text(
                    PRD_REVIEW_HUMAN_TEMPLATE,
                    {
                        "summary_json": json.dumps(summary, ensure_ascii=False, indent=2),
                        "draft": draft,
                        "conversation": _render_conversation(history),
                    },
                )
            ),
        ]
        # 走 `review_model`（默认就是写手那个模型；配了开关才是第二双眼睛）
        response = await track(self.review_model, LlmStep.PRD_REVIEW).ainvoke(messages)
        try:
            verdict = _extract_json_object(message_text(response))
        except SummarySyncError as exc:
            logger.warning("PRD 审核输出解析失败，本次按通过处理：%s", exc)
            return None

        issues = verdict.get("issues")
        if verdict.get("ok") is True:
            return []
        if not isinstance(issues, list) or not issues:
            # `ok: false` 却没给问题清单 —— 无法据此重写，同样按"没审出来"处理
            logger.warning("PRD 审核判了不通过但没给 issues，按通过处理")
            return None
        return [issue for issue in issues if isinstance(issue, dict)]

    # ------------------------------------------------- 生成后审查（05 篇：接口文档 / 提示词）

    async def review_api_docs(self, *, prd_content: str, content: str) -> dict[str, Any]:
        """审一遍**刚生成好的接口文档**。返回归一化结论（**一定会返回**，不返回 None）。

        与 PRD 审查的三处刻意差异：
        1. **不重写**（产品决定）：结论只展示，正文照交 —— 所以这里没有"issues 非空则改写"
           那条回路，调用方拿到什么就展示什么；
        2. 解析失败 / 调用失败 → `review_skipped=True` + `passed=True`，**不判不通过**：
           一个跑挂的审查器没有资格说产物不合格（与 `_review_prd` 的既有约定一致）；
        3. 返回值里**自带 `review_model` 与 `review_skipped`**，省得调用方去摸 `_settings`。
        """
        return await self._review_generated(
            build=lambda: build_api_docs_review_messages(prd_content=prd_content, content=content),
            step=LlmStep.API_DOCS_REVIEW,
        )

    async def review_prompts(
        self, *, prd_content: str, content: str, api_docs_content: str = ""
    ) -> dict[str, Any]:
        """审一遍**刚生成好的提示词套件**。约定同上。

        `api_docs_content` 为空是合法状态（用户跳过了接口文档），审核员会按
        "没有接口文档可比对"来审 —— 见 `prompts_review_agent` 的说明。
        """
        return await self._review_generated(
            build=lambda: build_prompts_review_messages(
                prd_content=prd_content, content=content, api_docs_content=api_docs_content
            ),
            step=LlmStep.PROMPTS_REVIEW,
        )

    async def _review_generated(
        self, *, build: Callable[[], list[BaseMessage]], step: LlmStep
    ) -> dict[str, Any]:
        """生成后审查的**公共实现**（两个产物共用，省得两份解析各写一遍）。

        异常**不往上抛**：审查是"附加结论"，它失败不该让一份已经写好的正文作废
        （正文已经落库、也已经推给前端了）。失败与解析失败走同一条路：标 `review_skipped`。

        `build` 收成一个**惰性函数**是为了把它也罩进同一个 try：提示词文件缺失之类的
        构建期错误（`load_prompt` 抛 `FileNotFoundError`）同样要被降级成"未审核"，
        而不是把一个跑完的任务判成失败。
        """
        review_model = self._settings.review_llm_active_model
        try:
            messages = build()
            response = await track(self.review_model, step).ainvoke(messages)
            verdict = _extract_json_object(message_text(response))
        except Exception as exc:  # noqa: BLE001 - 见 docstring
            logger.warning("%s 审查没跑成（%s），本次按未审核处理", step.value, exc)
            return {
                "passed": True,
                "issues": [],
                "summary": None,
                "review_model": review_model,
                "review_skipped": True,
            }
        normalized = normalize_review_verdict(verdict)
        return {**normalized, "review_model": review_model, "review_skipped": False}

    # ------------------------------------------------------------ 文档修订

    async def optimize_document_stream(
        self,
        *,
        kind: DocKind,
        doc_title: str | None = None,
        section: str,
        feedback: str,
        current_content: str,
        form: Mapping[str, str] | None = None,
        known_info: str = _EMPTY,
        open_questions: str = _EMPTY,
        conflicts: str = _EMPTY,
        outline: str = "",
        scope_spec: str = _EMPTY,
        prd_content: str = _EMPTY,
        api_content: str = _EMPTY,
        outcome: StreamOutcome | None = None,
    ) -> AsyncIterator[str]:
        """按用户反馈**只修订一节**（F8.6「针对不合格项一键重生成对应章节」）。

        system prompt 仍用该产物自己的 `gen_*`（含 `gen_common` 基线）—— 修订不是另一种
        文档类型，规则完全一样；变的只是 human message（换成
        `services.prompts.OPTIMIZE_DOCUMENT_PROMPT_TEMPLATE`）。

        ⚠️ **`generate_scope` 强制取 `section`，不接受调用方覆盖。** 理由：
        `gen_common.md` 的硬性规则 #1 是"只输出 `{generate_scope}` 指定的部分"，
        而 human message 说的是"只输出修订后的「{section}」"。两者若不一致，
        模型收到的是两条互相矛盾的指令 —— 结果通常是把整篇重写一遍。
        所以这里让 `generate_scope` **跟着 `section` 走**，从结构上消除这种可能。

        Args:
            kind: 修的是哪份产物 —— 决定 system prompt 与 `prd_content` 是否必需。
            doc_title: 文档名，进 human message。不传则用 `DOC_TITLES[kind]`。
            section: 要修订的章节 / 小节名，必须与文档里的标题**逐字一致**。
            feedback: 用户反馈（F4.8 打回时给的）。空的话没什么可改的。
            current_content: **该节的现有正文**。⚠️ 必填：
                `OPTIMIZE_DOCUMENT_PROMPT_TEMPLATE` 原模板漏了这一块，只给反馈的话
                模型会从零重写，用户手改过的内容全丢（`会话持久化方案` §7.4 明确要求
                重生成前对已手改内容二次确认）。
            outline / scope_spec: 原生成时的分片信息，用于让修订保持同一套结构约束。
            prd_content / api_content: 修 prompts 类产物时可能需要（同生成方法）。

        Note:
            ✅ **三个变体都用真实模型验证过**（deepseek-chat，均未截断、且返回正文只含
            被要求的那一节）：`prd` 2.9 秒 / 668 字符；`api` 3.5 秒 / 1245 字符（按反馈
            补了「PRD 来源」列，回指到 FR 编号）；`prompts` 7.0 秒 / 1638 字符（只回
            `=== FILE: 05-verify.md ===` 一个文件）。实测表见 `api/conversation.py`。
        """
        _require_text(section, "section", "必须指明修订哪一节")
        _require_text(current_content, "current_content", "必须给出该节的现有正文")
        # 修 api / prompts 时要能回指 PRD；理由同 generate_*_stream
        if kind in ("api", "prompts"):
            _require_text(prd_content, "prd_content", "该产物必须能追溯到 PRD")

        values = self.generation_values(
            form=form,
            known_info=known_info,
            open_questions=open_questions,
            conflicts=conflicts,
            # 强制对齐：见上面 docstring 的说明
            scope=DocumentScope(
                outline=outline or "（沿用原生成时的结构清单）",
                scope=section,
                spec=scope_spec,
            ),
            prd_content=prd_content,
            api_content=api_content,
        )
        human = render_prompt_text(
            OPTIMIZE_DOCUMENT_PROMPT_TEMPLATE,
            {
                "doc_title": doc_title or DOC_TITLES[kind],
                "section": section,
                "feedback": feedback.strip() or "（用户没有额外说明，按 M8 质检的不合格项修订）",
                "current_content": current_content,
            },
        )
        async for chunk in self._stream_document(
            kind,
            values,
            human=human,
            # 优化也注入规范：用户要的是"按团队规范改这一节"，只给反馈与原文，
            # 模型会按自己的习惯重写（与生成结果风格不一致）。
            # `optimize-prd` 有意**不在**这里：PRD 的优化路径仍用 `gen_prd` 那套 system prompt，
            # 结构规范与技能包不是同一套，两边同时注入会给出互相矛盾的章节结构（见 README 待办）。
            skill_artifact=(
                _OPTIMIZE_ARTIFACT_BY_KIND.get(kind) if kind in ("api", "prompts") else None
            ),
            outcome=outcome,
            step=LlmStep.DOCUMENT_OPTIMIZE,
        ):
            yield chunk

    # ------------------------------------------------------------ 会话级入口（未实现）

    async def generate(self, session_id: str, kind: DocKind) -> None:
        """派发一份产物的生成任务。**仍未实现。**

        **必须立即返回**（派发后台任务后不等待），状态先落成 `*_generating`，
        客户端靠轮询或 SSE 跟进；返回后由 `apply_event` 更新 state。

        依赖 `SessionStore` / `apply_event`（都还没实现），所以这里继续抛。
        实现时应复用上面的 `generate_*_stream`，不要在任务层再写一套提示词组装。
        """
        raise NotImplementedError("DocumentService.generate 尚未实现（需要 SessionStore）")

    async def stream_generation(self, session_id: str, kind: DocKind) -> AsyncIterator[str]:
        """按会话 id 流式产出生成结果。**仍未实现。**

        这是**会话级门面**：先从会话状态里取出 `form` / `known_info` /
        `prd_content` 等输入，再转调 `generate_*_stream`。
        没有会话状态层就取不到输入 —— 而让调用方自己传这些，等于把状态机又搬回路由层。
        """
        raise NotImplementedError(
            "DocumentService.stream_generation 尚未实现（需要 SessionStore 提供输入）"
        )
        yield ""  # pragma: no cover - 让类型签名成立；实现时删除

    async def check(self, session_id: str, kind: DocKind) -> list[str]:
        """产物质检（M8，F8.1–F8.6）。**仍未实现。**

        **只校验不改写** —— 不合格项回到生成走重生成（F8.6），
        修复动作绝不在这里做，否则"质检通过"与"实际内容"会各说各话。
        """
        raise NotImplementedError("DocumentService.check 尚未实现（M8 质检规则未落地）")

    # ------------------------------------------------------------ 内部

    async def _stream_document(
        self,
        kind: DocKind,
        values: Mapping[str, str],
        *,
        human: str | None = None,
        system: str | None = None,
        skill_artifact: str | None = None,
        outcome: StreamOutcome | None = None,
        step: LlmStep | None = None,
    ) -> AsyncIterator[str]:
        """组装 system + human，用 `astream` 逐段产出文本。

        human 不传时用生成指令模板（`GEN_HUMAN_TEMPLATE`）；修订场景由调用方传渲染好的。

        `system` 传进来时**直接用它**，不再从 `gen_*.md` 渲染 —— 技能包路径走这条
        （见 `_skill_system_prompt`）。两个告警因此只在渲染路径上跑：它们诊断的是
        "占位符有没有注入"和"上游产物被重复内联"，而那两件事只与 `gen_*.md` 有关，
        对技能包的提示词说出来只会误导。

        `skill_artifact` 传进来时，把该产物绑定的技能包（团队规范 + 参考示例）
        **附在 human message 末尾**（见 `_skill_human_block`）。接口文档与提示词套件走这条：
        它们的结构规范在 system prompt，技能包补的是原来由 RAG 检索提供的那部分。

        `outcome` 传进来时，顺手把厂商的结束原因记进去 —— **这是判断"输出被上限截断"
        的唯一可靠依据**。注意它在**最后一个分片**上，所以逐个分片地看、取最后一个非空的。
        上游若中途抛错，`outcome` 不会更新（此时 `api` 层发的是 `error` 帧，不需要它）。
        """
        if system is None:
            self._warn_missing_placeholders(kind, values)
            system_prompt = self.render_system_prompt(kind, values)
            if len(system_prompt) > _PROMPT_WARN_CHARS:
                logger.warning(
                    "gen_%s 的 system prompt 有 %d 字符，超过经验阈值 %d。"
                    "常见原因是上游产物被重复内联（本次 prd_content 长 %d 字符，"
                    "而它在提示词里被引用多次）。确认一下没有把整份 PRD / 接口文档"
                    "塞进去好几遍。",
                    kind,
                    len(system_prompt),
                    _PROMPT_WARN_CHARS,
                    len(values.get("prd_content", "")),
                )
        else:
            system_prompt = system

        resolved_human = human if human is not None else self.render_human_prompt(values)
        if skill_artifact:
            block = _skill_human_block(skill_artifact)
            if block:
                resolved_human = f"{resolved_human}\n\n{block}"

        messages: list[BaseMessage] = [
            SystemMessage(content=system_prompt),
            # 顺序固定：SystemMessage → HumanMessage。反了模型会读成"用户先说完 AI 再复述"。
            HumanMessage(content=resolved_human),
        ]
        # 缺省按产物类型推断；只有「改写」与「文档优化」显式传入不同的 step
        if step is None:
            step = _DEFAULT_STEP_BY_KIND[kind]
        async for chunk in track(self.model, step).astream(messages):
            if outcome is not None:
                reason = finish_reason_of(chunk)
                # 只有拿到才覆盖：中间分片的 metadata 里这个键通常不存在（是 None），
                # 用 `or` 赋值会把最后那个真值又抹掉。
                if reason is not None:
                    outcome.finish_reason = reason
            text = message_text(chunk)
            if text:
                # 空白片段不产出 —— 否则前端会收到空帧（与 ConversationService 一致）
                yield text

    def _warn_missing_placeholders(self, kind: DocKind, values: Mapping[str, str]) -> None:
        """把"漏注入"从静默变成一条日志（坑 #11）。

        只告警不抛错：`gen_common.md` 的声明集合是各产物的**并集**，
        单份产物跑起来本就可能有"本产物用不上"的项，硬失败会误伤。
        """
        missing = missing_placeholders(kind, values)
        if missing:
            logger.warning(
                "gen_%s 有已声明但未注入的占位符，它们会以字面量留在 prompt 里：%s",
                kind,
                "、".join(missing),
            )


def _require_text(value: str, name: str, reason: str) -> None:
    """`value` 必须是有内容的文本，否则抛 `ValueError`。

    刻意用 `ValueError` 而不是自定义异常：这是**调用方传错了参数**，
    不是运行环境问题（环境问题用 `LlmConfigError`，路由层要据此返 503）。
    """
    if not value or not value.strip():
        raise ValueError(f"{name} 不能为空 —— {reason}")
