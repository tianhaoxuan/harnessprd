"""对话（澄清阶段）编排服务。

**业务逻辑的归属地**：路由只做参数解析，阶段推进 / 信息合并 / LLM 调用都在这一层。

⚠️ 本层**不 import `api.*`** —— 依赖方向 `api → services → core` 单向
（`docs/功能清单.md` §非功能、`backend/README.md`）。

⚠️ **用哪份角色基线**：澄清阶段用 `clarify_common`（`CLARIFY_SYSTEM_PROMPT`），
**不是**生成阶段的 `SYSTEM_PROMPT`（`gen_common`）。两份东西：
澄清侧要"先给判断再提问、每问必带建议答案"，生成侧要分片规则与禁止顶替。
用错一份，行为会明显不对（见 `services/prompts.py` 的说明）。

---

## 还没做的（写在这里，免得被当成已完成）

| 缺口 | 设计出处 |
| --- | --- |
| **输出不是 JSON 契约**。设计要求每轮返回 `{message, questions[].suggested_answer, conflicts, stage_status, open_questions}`，前端才能渲染「一键采纳建议」按钮 | `docs/对话阶段设计.md` §6.2 |
| **阶段不由服务端推进**。`stage` 目前是调用方传进来的参数，不是状态机推进的结果 | 同上 §8 第 5 项 |
| **没有 `known_info` 合并**。缺了它，生成阶段拿不到信息只能自己编 —— 实测缺 S4 默认值时合规小节会被权限内容填满 | `docs/状态数据设计.md` §6.4、HANDOFF §4 坑 #4 |
| **没有会话状态**。`SessionStore` / `apply_event` 未实现，对话历史只存在于调用方 | `状态数据设计` §4.1 / §4.2 |

⚠️ **一个未解决的张力**：F3.14 要求「单轮追问上限 15s，期间界面不能是空白」→ 要么流式、
要么轮询；而 §6.2 要求输出是**结构化 JSON**。**逐字流式输出原始 JSON，前端在生成过程中
只能看到半截 JSON 语法。** 这个矛盾需要拍板，见 `continue_conversation_stream` 的说明。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Literal

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage

from core.config import Settings, get_settings
from core.prompts import build_system_prompt
from core.questions import Question, load_questions
from services.llm import StreamOutcome, finish_reason_of
from services.llm_factory import get_llm
from services.state import DialogueStage

logger = logging.getLogger(__name__)

MessageKind = Literal["say", "question", "answer", "summary"]

# 强制收敛的轮次上限。出处：`docs/对话阶段设计.md` §5.4。
# ⚠️ 取 4 还是 5 仍是该文档 §7 的 D5 待确认项，这里先用 4。
DEFAULT_MAX_ROUNDS = 4

# 空占位。提示词里这些位置没内容时要显式写「尚无」，
# **不能留空** —— 留空等于告诉模型"这个维度没输入"，而它就会拿邻近内容顶替（坑 #3）。
_EMPTY = "（尚无）"

# 开场轮的触发语。与 `scripts/validate_prompts.py` 当初验证时用的一致 ——
# 那 7 份留档就是在这个输入下产出的，换掉就等于换了验证条件。
START_HUMAN_TRIGGER = "请按上述要求开始。（这是本轮用户输入）"


# ---------------------------------------------------------------- 文本工具


def format_form_data(
    form: Mapping[str, str],
    questions: Sequence[Question] | None = None,
) -> str:
    """把表单作答转成 `{form_summary}` 用的易读文本。

    输出格式（与 `scripts/validate_prompts.py` 验证过的那份**逐字一致**）::

        - 产品名称（`product_name`）：群聊周报助手
        - 成功指标（`success_metrics`）：**未填写**

    **未答的题必须标「未填写」，不能省略。** `gen_common.md` 明确要求
    「未答的题标「未填写」，视为该维度无输入」；省掉这一行，模型就会拿邻近内容顶替
    （`HANDOFF.md` §4 坑 #3：`compliance` 为空时小节被权限内容填满，**看起来还像真的**）。

    Args:
        form: 键 = 题目 id，值 = 作答（扁平，与后端 `form: dict[str, str]` 同形）。
        questions: 不传则取 `core/questions_config.json` 的当前配置。

    Note:
        `form` 里若有**当前配置已不存在**的 id（题目被删过），按
        `docs/状态数据设计.md` §6.2 的规则**不参与**这里的输出：
        「当前配置删除了题 → 数据保留在 `form` 里，但不参与 `known_info` 与生成」。
    """
    resolved = list(questions) if questions is not None else load_questions().all_questions

    lines: list[str] = []
    for question in resolved:
        value = (form.get(question.id) or "").strip()
        lines.append(f"- {question.label}（`{question.id}`）：{value or '**未填写**'}")
    return "\n".join(lines)


def covered_dimensions(
    form: Mapping[str, str],
    questions: Sequence[Question] | None = None,
) -> list[str]:
    """算出表单**已经覆盖**了 F3.8 的哪些追问维度。

    规则来自 `core/questions_config.json` 的 `covers_dimensions_note`：
    「某维度被覆盖 = 至少有一个声明了该维度的问题**已填写且非空**；
    选填题留空时不算覆盖，对话仍须追问该维度」。

    这是 `状态数据设计` §3 推导规则的第一段（另一段来自 `known_info`，待会话层补）。
    """
    resolved = list(questions) if questions is not None else load_questions().all_questions

    covered: list[str] = []
    for question in resolved:
        if not (form.get(question.id) or "").strip():
            continue  # 留空 = 未覆盖
        for dimension in question.covers_dimensions:
            if dimension not in covered:
                covered.append(dimension)
    return covered


def message_text(message: BaseMessage) -> str:
    """把消息的 `content` 归一成纯文本。

    不同厂商 / 不同版本可能返回 `str`，也可能返回 content block 列表。
    统一在这里处理，**不要依赖某个版本才有的 `message.text`**。

    顺带一个作用：思考模式（如 `deepseek-flash` 默认开）会额外产出
    `reasoning_content`，那不是给用户看的正文 —— 这里只取 text block，天然排除它。
    """
    content = message.content
    if isinstance(content, str):
        return content

    chunks: list[str] = []
    for block in content:
        if isinstance(block, str):
            chunks.append(block)
        elif isinstance(block, dict) and block.get("type") == "text":
            chunks.append(str(block.get("text", "")))
    return "".join(chunks)


def _format_history(history: Sequence[Mapping[str, Any]]) -> str:
    """把前端消息历史渲染成 `{history}` 占位符要的文本。"""
    if not history:
        return "（这是第一轮）"

    lines: list[str] = []
    for item in history:
        role = "用户" if item.get("role") == "user" else "AI"
        content = str(item.get("content", "")).strip()
        if content:
            lines.append(f"[{role}] {content}")
    return "\n".join(lines) if lines else "（这是第一轮）"


# ---------------------------------------------------------------- 消息转换


def _build_lc_messages(
    system_prompt: str,
    history: Sequence[Mapping[str, Any]] = (),
    user_input: str | None = None,
) -> list[BaseMessage]:
    """前端消息 → LangChain 消息列表。

    顺序固定为：`SystemMessage` → 历史（按时间正序）→ 当前用户输入。
    顺序错了模型会读成"用户先说完 AI 再复述"，所以这里不接受调用方自行拼装。

    `history` 的每一项是前端 / `api.schemas.Message` 的形状：
    ``{"role": "user" | "ai", "content": str, ...}``。
    """
    messages: list[BaseMessage] = [SystemMessage(content=system_prompt)]

    for item in history:
        content = str(item.get("content", ""))
        if not content.strip():
            continue
        # 认三种写法：user / ai（设计里的取值）/ assistant（OpenAI 的叫法）
        if item.get("role") == "user":
            messages.append(HumanMessage(content=content))
        elif item.get("role") in ("ai", "assistant"):
            messages.append(AIMessage(content=content))
        else:
            logger.warning("历史消息的 role 无法识别，已跳过：%r", item.get("role"))

    if user_input and user_input.strip():
        messages.append(HumanMessage(content=user_input))

    return messages


def _serialize_messages(
    messages: Sequence[BaseMessage],
    *,
    stage: DialogueStage = "S0",
    kind: MessageKind = "say",
) -> list[dict[str, Any]]:
    """LangChain 消息 → 前端消息格式（`docs/状态数据设计.md` §2.2 的 `Message`）。

    ⚠️ 返回 **plain dict** 而不是复用 `api.schemas.Message`：服务层不能 import `api.*`。
    形状必须与那个模型保持一致 —— 改这里要同步改那边（`api/schemas.py` 的 `Message`）。

    ⚠️ **这个转换有损**：LangChain 消息只带 role + content，而设计的 `Message` 还要
    `stage` 与 `kind`。它们由参数统一给出（同一轮内所有消息的 stage/kind 相同）。
    真实的 `id` 与 `created_at` 应由会话层分配，这里用序号 + 当前时间占位。
    """
    now = datetime.now(UTC).isoformat()
    serialized: list[dict[str, Any]] = []

    for index, message in enumerate(messages):
        if isinstance(message, HumanMessage):
            role = "user"
        elif isinstance(message, AIMessage):
            role = "ai"
        else:
            # SystemMessage 不进对话历史（它是给模型的指令，不是对话内容）
            continue

        serialized.append(
            {
                "id": f"msg-{index}",
                "role": role,
                "stage": stage,
                "kind": kind,
                "content": message_text(message),
                "created_at": now,
            }
        )

    return serialized


# ---------------------------------------------------------------- 服务


class ConversationService:
    """澄清阶段（S0–S5）的对话编排。

    用法::

        service = ConversationService()
        async for chunk in service.start_conversation_stream(form):
            ...  # chunk 是裸文本片段，SSE 帧由 api 层包装

    ⚠️ 还没做的见模块 docstring 顶部那张表：JSON 契约、阶段推进、`known_info` 合并、会话状态。
    """

    def __init__(
        self,
        model: BaseChatModel | None = None,
        settings: Settings | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._model = model

    @property
    def model(self) -> BaseChatModel:
        """惰性构造模型客户端。

        惰性很重要：没配 Key 时只有**真正调用**才报错，
        所以导入链、健康检查不会被 Key 缺失阻断。
        """
        if self._model is None:
            self._model = get_llm(streaming=True, settings=self._settings)
        return self._model

    # ------------------------------------------------------------ 提示词

    def render_system_prompt(
        self,
        stage: DialogueStage,
        values: Mapping[str, str],
    ) -> str:
        """按设计的组装规则渲染本轮 system prompt。

        `system prompt = clarify_common + clarify_s{stage}`，**顺序不可颠倒**
        （`docs/对话阶段设计.md` §6.1）。组装与渲染都在 `core.prompts`，这里只调。
        """
        return build_system_prompt(f"clarify_{stage}", values)

    def clarify_values(
        self,
        *,
        stage: DialogueStage,
        form: Mapping[str, str] | None = None,
        round_index: int = 1,
        max_rounds: int = DEFAULT_MAX_ROUNDS,
        history: Sequence[Mapping[str, Any]] = (),
        user_input: str = "",
        known_info: str = _EMPTY,
        open_questions: str = _EMPTY,
        conflicts: str = _EMPTY,
        target_features: str = _EMPTY,
        candidate_features: str = _EMPTY,
        empty_fields: str = _EMPTY,
    ) -> dict[str, str]:
        """组装提示词占位符的值。

        **这里必须把该阶段声明的占位符全部给到。** 漏一个的后果不是报错，而是
        `str.replace` 静默跳过 —— 占位符以字面量留在 prompt 里，模型照样输出像样的结果，
        于是"看起来正常"掩盖了"输入根本没送到"（`HANDOFF.md` §4 坑 #11）。
        `scripts/smoke_check.py` 有一条断言专门盯这件事。
        """
        form = form or {}
        questions = load_questions().all_questions

        dimensions = covered_dimensions(form, questions)
        product_name = (form.get("product_name") or "").strip() or "（未命名）"

        return {
            "product_name": product_name,
            "form_summary": format_form_data(form, questions) if form else _EMPTY,
            "covered_dimensions": "、".join(dimensions) if dimensions else _EMPTY,
            "known_info": known_info,
            "open_questions": open_questions,
            "conflicts": conflicts,
            "history": _format_history(history),
            "user_reply": user_input.strip() or "（无）",
            "round_index": str(round_index),
            "max_rounds": str(max_rounds),
            # 下面三个是阶段专属的（S2/S3/S4），不用的阶段给了也无害
            "target_features": target_features,
            "candidate_features": candidate_features,
            "empty_fields": empty_fields,
        }

    # ------------------------------------------------------------ 流式对话

    async def start_conversation_stream(
        self,
        form: Mapping[str, str],
        *,
        stage: DialogueStage = "S0",
        round_index: int = 1,
        max_rounds: int = DEFAULT_MAX_ROUNDS,
        outcome: StreamOutcome | None = None,
    ) -> AsyncIterator[str]:
        """**首轮**：用表单作答开场（默认 S0 开场复盘）。

        逐段产出**裸文本片段**（不是 SSE 帧）—— 传输协议由 `api` 层包装，
        服务层不感知 HTTP。
        """
        values = self.clarify_values(
            stage=stage,
            form=form,
            round_index=round_index,
            max_rounds=max_rounds,
        )
        messages = _build_lc_messages(
            self.render_system_prompt(stage, values),
            history=(),
            user_input=START_HUMAN_TRIGGER,
        )
        async for chunk in self._astream_text(messages, outcome=outcome):
            yield chunk

    async def continue_conversation_stream(
        self,
        form: Mapping[str, str],
        history: Sequence[Mapping[str, Any]],
        user_input: str,
        *,
        stage: DialogueStage = "S0",
        round_index: int = 2,
        max_rounds: int = DEFAULT_MAX_ROUNDS,
        known_info: str = _EMPTY,
        open_questions: str = _EMPTY,
        conflicts: str = _EMPTY,
        outcome: StreamOutcome | None = None,
    ) -> AsyncIterator[str]:
        """**接续**：带上历史与用户本轮输入，继续这一阶段。

        ⚠️ **两个未解决的问题**（都需要拍板，不要当成已定）：

        1. **流式 vs JSON 契约**：设计要求输出结构化 JSON（§6.2），但 F3.14 要求
           15s 内界面不能空白。逐字流式输出**原始 JSON**，前端在过程中只能看到半截
           语法（`{"message": "我先把表单读回来…`）。可选方案：
           (a) 流式 + 结束后整体解析 JSON（过程中显示原始增量，体验一般）；
           (b) 让模型先只输出 `message` 正文、结构化部分后补（需要改提示词契约）；
           (c) 不流式，改用等待指示（骨架屏 / 进度文案）—— 与 §7.5 的轮询方案一致。
        2. **阶段推进不在服务端**：`stage` 由调用方传入，而不是状态机根据
           `stage_status` 推进（§8 第 5 项要求服务端推进、不让模型自判）。
           要等 `SessionStore` / `apply_event` 落地才能接。
        """
        values = self.clarify_values(
            stage=stage,
            form=form,
            round_index=round_index,
            max_rounds=max_rounds,
            history=history,
            user_input=user_input,
            known_info=known_info,
            open_questions=open_questions,
            conflicts=conflicts,
        )
        messages = _build_lc_messages(
            self.render_system_prompt(stage, values),
            history=history,
            user_input=user_input,
        )
        async for chunk in self._astream_text(messages, outcome=outcome):
            yield chunk

    # ------------------------------------------------------------ 内部

    async def _astream_text(
        self,
        messages: Sequence[BaseMessage],
        outcome: StreamOutcome | None = None,
    ) -> AsyncIterator[str]:
        """用 `astream` 逐段取文本；空白片段不产出（避免前端收到空帧）。

        `outcome` 传进来时顺手记下厂商的结束原因 —— 对话侧**同样会撞输出上限**
        （一段结构化 JSON 被切断，前端只能拿到半截语法），而这件事在文本里看不出来。
        判据与文档侧完全一致，用的是同一份 `finish_reason_of`。
        """
        async for chunk in self.model.astream(list(messages)):
            if outcome is not None:
                reason = finish_reason_of(chunk)
                # 只有拿到才覆盖：中间分片通常没有这个键，用 `or` 会把最后那个真值抹掉
                if reason is not None:
                    outcome.finish_reason = reason
            text = message_text(chunk)
            if text:
                yield text

    async def collect(self, chunks: AsyncIterator[str]) -> str:
        """把流收成全量文本。给"不需要流式"的调用方与测试用。"""
        parts: list[str] = []
        async for chunk in chunks:
            parts.append(chunk)
        return "".join(parts)
