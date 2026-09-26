"""生成 / 澄清提示词的**命名入口** —— 只有名字，没有正文。

⚠️ **本模块不含任何提示词正文。** 每个常量都是从 `core/prompts/*.md`
**组装出来的**（`core.prompts.assemble_prompt_template`）。这不是洁癖，是三条硬约束：

1. `docs/功能清单.md` F6.3：提示词模板外置可改 —— **改文案不用读代码**
2. `docs/功能清单.md` §非功能：提示词与表单定义**均外置**
3. `HANDOFF.md` §4 坑 #10：模板有两份来源时，**手工同步的错误率是 100%**

把正文复制进 Python 常量会同时违反这三条，并且让 `validation_out/` 那 7 份真实 LLM
留档失去意义（它们验证的是 `.md` 文件本身）。

**本模块只解决一件事**：给"哪份提示词"起稳定的名字。正文改了不用动这里；
哪天换了底层文件，也只改这里一处。

⚠️ 以下常量是**未渲染的模板** —— 里面还留着 `{form_summary}`、`{generate_scope}`
这类占位符。实际调用请用 `core.prompts.build_system_prompt(name, values)` 渲染；
**不要拿常量去 `str.format`**，那正是坑 #1（花括号格式记号会被当占位符）。
"""

from __future__ import annotations

from core.prompts import assemble_prompt_template

# ---------------------------------------------------------------- 角色基线
# ⚠️ 设计里有**两份**角色基线，职责不同，不能合并成一份通用的"资深产品经理"：
#   - clarify_common.md  澄清阶段：26 条引导技巧 + JSON 输出契约 + 硬性禁令
#   - gen_common.md      生成阶段：输入说明 + 输入缺失时的行为 + 分片生成规则
# 出处：docs/对话阶段设计.md §6.1、HANDOFF.md §3.3

SYSTEM_PROMPT = assemble_prompt_template("gen_common")
"""生成阶段的角色基线：AI 作为资深产品经理，产出三份文档。

注意它**只是基线**，不是完整 system prompt —— 真正给模型的是
`SYSTEM_PROMPT + 对应产物提示词`，顺序不可颠倒（`gen_common.md` §组装方式）。
"""

CLARIFY_SYSTEM_PROMPT = assemble_prompt_template("clarify_common")
"""澄清阶段的角色基线。

与 `SYSTEM_PROMPT` 是**两份**，不要互相替代：澄清侧要的是"先给判断再提问、
每问必带建议答案"，生成侧要的是分片规则与禁止顶替。用错一份，行为会明显不对。

各阶段（S0–S5）的完整 system prompt 用
`build_system_prompt(f"clarify_s{n}", values)` 组装。
"""

# ---------------------------------------------------------------- 三份产物
# 本节常量**已含基线**（`gen_common` + `gen_<产物>`），直接就是完整的 system prompt 模板。

PRD_GENERATION_PROMPT = assemble_prompt_template("gen_prd")
"""PRD 生成的**回退路径**：15 章 + 2 附录的结构见 `docs/PRD模板.md`。

产品默认走 `skills/prd-generator/` 技能包（6 章 + FR/AC 编号），本常量只在
`PRD_USE_SKILL=false` 时被用到（`DocumentService.prd_prompts()`）。两套结构互不兼容。"""

API_DOCS_GENERATION_PROMPT = assemble_prompt_template("gen_api")
"""接口文档生成。11 章结构 + 错误码约定见 `docs/接口文档模板.md`。"""

PROMPTS_GENERATION_PROMPT = assemble_prompt_template("gen_prompts")
"""提示词套件生成。五类文件 + `=== FILE: ===` 多文件契约见 `docs/提示词套件模板.md`。"""

# ---------------------------------------------------------------- 文档修订
# ⚠️ 这是**用户消息**模板，不是 system prompt —— 用法：把它渲染后作为 HumanMessage，
# system prompt 仍用对应的 `*_GENERATION_PROMPT`。
#
# 语义出处：
#   - F8.6「针对不合格项一键重生成对应章节」（M8 只校验不改写，修复动作回到生成）
#   - F4.8「打回时用户给的反馈」→ `DocumentState.feedback`
#   - `gen_common.md` 分片硬性规则：「只输出 {generate_scope} 指定的部分，不多不少」
#     且「不要为未生成的章节留占位标题」
#
# ⚠️ **尚未经真实 LLM 验证。** 按 `HANDOFF.md` §8 跑一次 `validate_prompts.py`
# 才算数；在那之前不要把它当成已验证能力，也不要据此声称支持"文档优化"。
#
# ⚠️ **`{current_content}` 是后来补上的。** 原模板只说了"修订「{section}」"和
# "用户没提到的内容保持原样"，却**没有把该节的现有正文给模型**。两个后果：
# 模型要么凭反馈从零重写这一段（用户手改过的内容全丢，而 `会话持久化方案` §7.4
# 恰恰要求重生成前对已手改内容二次确认），要么因为缺少依据而反问。
# 所以补一个显式的当前正文块。**补了之后更该重跑一次验证。**

OPTIMIZE_DOCUMENT_PROMPT_TEMPLATE = (
    "请只修订《{doc_title}》的「{section}」部分。\n\n"
    "用户反馈：\n{feedback}\n\n"
    "「{section}」的当前内容：\n{current_content}\n\n"
    "要求：\n"
    "1. 只输出修订后的「{section}」，不要重复输出其它章节；\n"
    "2. 不要为未修订的章节留占位标题；\n"
    "3. 用户没提到的内容保持原样；\n"
    "4. 若反馈与原文冲突，以反馈为准，并在末尾用一行说明改了什么。\n"
)
