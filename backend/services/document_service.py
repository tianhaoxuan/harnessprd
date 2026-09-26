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

import logging
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage

from core.config import Settings, get_settings
from core.prompts import build_system_prompt, declared_placeholders, render_prompt_text
from core.questions import load_questions
from services.conversation_service import format_form_data, message_text
from services.llm import StreamOutcome, finish_reason_of
from services.llm_factory import get_llm
from services.prompts import (
    API_DOCS_GENERATION_PROMPT,
    OPTIMIZE_DOCUMENT_PROMPT_TEMPLATE,
    PRD_GENERATION_PROMPT,
    PROMPTS_GENERATION_PROMPT,
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

# 仓库根目录下的 skills/。
# ⚠️ 用 `__file__` 定位，**不要用 cwd** —— 服务可能从任意工作目录启动
# （Docker 里是 /app，本机是 backend/，脚本里是别处），按 cwd 找必然时灵时不灵。
SKILL_ROOT = Path(__file__).resolve().parents[2] / "skills"

# PRD 技能包相对 `SKILL_ROOT` 的目录名。
SKILL_PRD_DIR = "prd-generator"

# 技能包里的章节目录（PRD 模板）。`document_plan` 解析它得出"整份几章"的标签 ——
# 常量放这里，那边 import 过去，避免同一个路径写两遍（改一处漏一处的老问题）。
SKILL_PRD_TEMPLATE = "references/prd-template.md"

# 组装 PRD 技能 system prompt 时读取的文件，**顺序即拼接顺序**：
# 先工作流（做什么、按什么顺序做），再模板（输出什么结构），再写作规则（怎么写），
# 最后输入契约（字段定义）。与 `gen_common + gen_*` 的"基线在前、产物专属在后"同一条道理。
SKILL_PRD_ARTIFACTS: tuple[str, ...] = (
    "instructions.md",
    SKILL_PRD_TEMPLATE,
    "references/writing-rules.md",
    "references/field-schema.json",
)

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


@lru_cache(maxsize=None)
def _load_skill_artifact(name: str, skill: str = SKILL_PRD_DIR) -> str:
    """读技能包里的一个文件，参数是**相对技能目录的路径**（如 `references/prd-template.md`）。

    结果按 `(skill, name)` 缓存：技能包是静态文件，一次进程读一次就够，
    而 PRD 生成可能被反复调用。

    Raises:
        ValueError: 路径逃出技能目录（`../` 之类）。这是**调用方传错了**，
            不是文件缺失，所以不跟 FileNotFoundError 混在一起。
        FileNotFoundError: 文件确实不存在。错误信息里列出该技能包现有的文件，
            省得为了知道"有什么"再去翻目录。
    """
    root = SKILL_ROOT.resolve()
    skill_dir = (root / skill).resolve()
    path = (skill_dir / name).resolve()

    # 目录穿越检查：解析后的路径必须仍在技能目录内。
    # 不检查的话 `_load_skill_artifact("../../backend/.env")` 会把密钥读进 system prompt。
    if path != skill_dir and skill_dir not in path.parents:
        raise ValueError(f"技能文件路径越界：{skill}/{name} —— 只允许读技能目录内的文件")

    if not path.is_file():
        available = "、".join(
            sorted(str(p.relative_to(skill_dir)) for p in skill_dir.rglob("*") if p.is_file())
        )
        raise FileNotFoundError(
            f"技能文件不存在：{skill}/{name}；该技能包现有文件：{available or '（空）'}"
        )

    return path.read_text(encoding="utf-8").strip()


def _skill_system_prompt(
    skill: str = SKILL_PRD_DIR,
    artifacts: Sequence[str] = SKILL_PRD_ARTIFACTS,
) -> str:
    """把技能包的几份文件拼成一份完整 system prompt。

    每个文件前加一行来源标题，便于排查"这句话到底来自哪份文件"——
    技能包是多文件拼装，缺了这行，出问题时只能靠猜。
    """
    blocks = [
        f"===== 技能包 skills/{skill}/，按顺序阅读并遵守 =====",
    ]
    for name in artifacts:
        blocks.append(f"----- 文件：{name} -----\n\n{_load_skill_artifact(name, skill)}")
    return "\n\n".join(blocks)


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

    @property
    def model(self) -> BaseChatModel:
        """惰性构造模型客户端。

        惰性很重要：没配 Key 时只有**真正调用**才报错，
        所以导入链、健康检查不会被 Key 缺失阻断（与 `ConversationService` 一致）。
        """
        if self._model is None:
            self._model = get_llm(streaming=True, settings=self._settings)
        return self._model

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
            return _skill_system_prompt(), SKILL_PRD_HUMAN_TEMPLATE
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
        async for chunk in self._stream_document("api", values, outcome=outcome):
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
        async for chunk in self._stream_document("prompts", values, outcome=outcome):
            yield chunk

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
            ⚠️ **未经真实 LLM 验证。** `OPTIMIZE_DOCUMENT_PROMPT_TEMPLATE` 的
            docstring 已经声明过这一点，而本轮又给它补了 `{current_content}`，
            所以**更该重跑一次验证**才能声称"文档优化可用"。
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
        async for chunk in self._stream_document(kind, values, human=human, outcome=outcome):
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
        outcome: StreamOutcome | None = None,
    ) -> AsyncIterator[str]:
        """组装 system + human，用 `astream` 逐段产出文本。

        human 不传时用生成指令模板（`GEN_HUMAN_TEMPLATE`）；修订场景由调用方传渲染好的。

        `system` 传进来时**直接用它**，不再从 `gen_*.md` 渲染 —— 技能包路径走这条
        （见 `_skill_system_prompt`）。两个告警因此只在渲染路径上跑：它们诊断的是
        "占位符有没有注入"和"上游产物被重复内联"，而那两件事只与 `gen_*.md` 有关，
        对技能包的提示词说出来只会误导。

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

        messages: list[BaseMessage] = [
            SystemMessage(content=system_prompt),
            # 顺序固定：SystemMessage → HumanMessage。反了模型会读成"用户先说完 AI 再复述"。
            HumanMessage(content=human if human is not None else self.render_human_prompt(values)),
        ]
        async for chunk in self.model.astream(messages):
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
