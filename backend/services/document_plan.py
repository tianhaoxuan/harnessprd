"""分片计划：把一份长产物切成若干次调用，**切分清单从提示词文件里解析出来**。

## 为什么要有这个模块

整份生成接口文档（17257 字符）与提示词套件（18354 字符）**必然撞上单次输出上限**
被截断，末尾停在半行表格 / 半个文件（`HANDOFF.md` §4 坑 #18）。修法只有一个：
**分多次调用再拼装**（`docs/PRD模板.md` §4.3 的 R4）。

## 为什么清单是"解析"出来的，而不是写在这里

`HANDOFF.md` §4 坑 #10：章节清单**已经内嵌在 `gen_*.md` 里**，本项目历史上手工同步
这份清单的错误率是 100%。所以这里**只解析、不抄写**：

| 产物 | 清单来源（唯一真相） | 解析目标 |
| --- | --- | --- |
| `api` | `gen_api.md` 的「## 章节清单」表 | 每章的「字数上限」列 → 用它做分批预算 |
| `prompts` | `gen_prompts.md` 的「## 目录结构」代码块 | 文件名清单（含 `04-step-NN-<模块名>` 这类可变项） |
| `prd` | 不分片（见 `_prd_plan`） | — |

改提示词里的章节/文件清单，计划**自动跟着变**；这份模块不需要动。

## 分批规则

- **预算**：累计「声明字数上限」不超过 `_PART_BUDGET_CHARS`（一个保守值，见常量注释）。
  单章超过预算时**独占一个分片**（不能再切，再切就不是"一章"了）。
- 套件按目录结构的**结构**分批：可变组（`04-step-*`）自己一片 —— 它与前后的固定文件
  边界清晰，而且它是唯一会随项目规模增长的部分。

## 没做的事（明确留白）

- 套件的可变组**没有按步骤数再切**：步骤数由 PRD 的功能条数决定，而本模块是**无状态纯函数**
  （不读 PRD）。步骤特别多的项目，那一片仍可能超上限 —— 届时会由截断提示暴露出来，
  再引入"按步骤分批"（需要把 PRD 摘要传进来）也不算返工。
- 不做拼接：分片结果由调用方（前端）拼，服务端不持有状态。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from core.config import get_settings
from core.prompts import load_prompt
from services.state import DocKind

logger = logging.getLogger(__name__)

# 单个分片允许的「声明字数上限」累计值。
# 怎么定的：`gen_api.md` 全章字数上限合计 10300，而实测整份输出 17257 字符
# （**实际约为声明的 1.7 倍**）。输出上限 8192 token ≈ 14000 字符（中文约 1.7 字符/token），
# 按 1.7 倍系数反推：单片声明量应 ≲ 14000 / 1.7 / 1.7 ≈ 4800。取 4500 留一点余量。
_PART_BUDGET_CHARS = 4500

# 「字数上限」单元格：纯数字，或 `1200 / 步` 这类带单位的写法（套件用）
_NUMBER_RE = re.compile(r"\d+")

# `04-step-01-<模块名>.md` 里的 `<...>` 表示"这里由模型按项目展开"
_PLACEHOLDER_RE = re.compile(r"[<＜].+?[>＞]")

# 技能包 PRD 模板里的产物章节标题：`## 1. 产品概述`。
# 刻意只认"数字 + 点"，好把 `## 使用说明` 排除掉（它是模板自身的说明，不是产物章节）。
_SKILL_CHAPTER_RE = re.compile(r"^##\s+(\d+\.[^\n]*)$", re.MULTILINE)


@dataclass(frozen=True)
class PlanPart:
    """一个分片：一次生成调用要产出的那一段。"""

    index: int
    """从 1 起的序号（给人看的进度用）。"""

    label: str
    """一句话说明这一片是什么，如「第 1–6 章 文档信息…接口清单」。"""

    scope: str
    """进 `{generate_scope}` —— **只列本片的范围**，提示词的硬性规则 #1 靠它约束。"""

    spec: str
    """进 `{scope_spec}` —— 本片的详细规格（含每章字数上限与跨片禁忌）。"""

    outline: str
    """进 `{doc_outline}` —— **整份**的结构清单（每片都一样），让模型知道上下文的边界。"""


@dataclass(frozen=True)
class DocumentPlan:
    """一份产物的完整分片计划。"""

    kind: DocKind
    parts: tuple[PlanPart, ...]
    source: str
    """清单解析自哪个提示词文件（留痕，便于人核对"清单到底从哪来"）。"""

    @property
    def multi_part(self) -> bool:
        return len(self.parts) > 1


class DocumentPlanError(RuntimeError):
    """清单解析失败（提示词文件被改动到认不出来）。

    **刻意失败而不是回退成"单次整份"**：静默回退会让人以为分片生效了，
    而产物其实是残的 —— 那正是坑 #18 的形态。解析规则见各 `_parse_*`。
    """


# ---------------------------------------------------------------- 对外入口


def build_plan(kind: DocKind, *, use_skill: bool | None = None) -> DocumentPlan:
    """按产物类型给出分片计划。

    `use_skill` 只对 `prd` 有意义：它决定 PRD 的章节清单从哪份模板来
    （技能包 6 章 / 老结构 15 章 + 2 附录）。不传就取 `settings.prd_use_skill`，
    与 `DocumentService.prd_prompts()` 取的是**同一个开关** ——
    两边各判一次就会漂移成"界面说 15 章、产物是 6 章"。
    """
    if kind == "prd":
        return _prd_plan(use_skill)
    if kind == "api":
        return _api_plan()
    if kind == "prompts":
        return _prompts_plan()
    raise ValueError(f"未知产物类型：{kind!r}")  # pragma: no cover - DocKind 是 Literal


# ---------------------------------------------------------------- PRD


def _prd_plan(use_skill: bool | None = None) -> DocumentPlan:
    """PRD **刻意不分片**，但章节清单跟着开关走。

    实测整份 PRD 装得下、不截断（技能包 6 章路径见 `validation_out/prd_skill.txt`；
    老结构见 `PRD模板.md` §4.3）。分片只会把调用次数、拼接风险、跨片不一致都乘上去，
    换不来任何东西 —— 所以这里是一个**有依据的产品决定**，不是"还没做"。
    要按章重生成走 `optimize_document_stream`（F8.6）那条路。

    ⚠️ 标签与 `outline` **必须反映真实结构**：它们会显示在生成进度里
    （前端 `docProgress.label`），写错了就是"界面说 15 章、产物 6 章"。
    所以章节清单同样**只解析、不抄写**（坑 #10）：
    技能包解析 `references/prd-template.md` 的 `## N. 标题`，老结构解析 `gen_prd.md`。
    """
    resolved = get_settings().prd_use_skill if use_skill is None else use_skill
    if resolved:
        return _skill_prd_plan()

    return DocumentPlan(
        kind="prd",
        parts=(
            PlanPart(
                index=1,
                label="整份（15 章 + 2 附录）",
                scope="全文",
                spec="按本提示词内嵌的模板与字数要求输出完整文档，不额外限制本次范围。",
                outline=_outline_or_default("gen_prd", "完整文档（本次不分片）。"),
            ),
        ),
        source="gen_prd.md（不分片，回退路径）",
    )


def _skill_prd_plan() -> DocumentPlan:
    """技能包路径的 PRD 计划：标签与 outline 都从技能包模板解析出来。

    「哪个 skill 的哪份文件是模板」不再写在这里，而是问 `services/skill_loader`
    （**不在这里再写一份 `skills/prd-generator`**）—— 抄一份路径的下场是模板搬家后
    这里还指着旧路径。延迟导入是为了不让 `document_plan` 在模块加载期就拖上整个服务层。
    """
    from services.skill_loader import load_skill_bundle  # noqa: PLC0415

    chapters: list[str] = []
    template = None
    try:
        bundle = load_skill_bundle("prd")
        template = next((item for item in bundle.artifacts if item.role == "template"), None)
    except Exception as exc:  # noqa: BLE001 - 计划接口不该因为技能包缺失整个挂掉
        logger.warning("读不到 PRD 技能包：%s，改用兜底标签", exc)

    if template is not None:
        # 只认 `## <数字>. <标题>`：同一份文件里还有 `## 使用说明`（模板自身的说明，
        # 不属于产物），用宽松规则解析会把标签写成 7 章。
        chapters = [m.group(1).strip() for m in _SKILL_CHAPTER_RE.finditer(template.content)]

    return DocumentPlan(
        kind="prd",
        parts=(
            PlanPart(
                index=1,
                label=f"整份（{len(chapters)} 章）" if chapters else "整份（技能包）",
                scope="全文",
                spec=(
                    "按技能包内嵌的模板输出完整的 6 章文档，含第 2 章的 FR-xx 功能编号与"
                    "AC-xx 验收标准；不额外限制本次范围。"
                ),
                outline=(
                    "PRD 的完整结构（本次不分片，一次产出全部章节）：\n"
                    + "\n".join(f"- {name}" for name in chapters)
                    if chapters
                    else "完整文档（本次不分片）。"
                ),
            ),
        ),
        # 来源用**模板那一份自己的** skill id 与路径拼（`template.skill_id`），
        # 不猜、不拼第一个 skill —— 多 skill 绑同一产物时也能指对。
        source=(
            f"skills/{template.skill_id}/{template.path}（不分片）"
            if template is not None
            else "技能包不可用（读不到模板，见日志）（不分片）"
        ),
    )


# ---------------------------------------------------------------- 接口文档


def _api_plan() -> DocumentPlan:
    """接口文档：解析 `gen_api.md` 的「## 章节清单」表，按字数上限贪心分批。"""
    rows = _parse_chapter_table(load_prompt("gen_api"))
    if not rows:
        raise DocumentPlanError(
            "gen_api.md 里没找到「## 章节清单」表 —— 分片清单依赖它。"
            "如果表格被改名或换了格式，请同步 _parse_chapter_table()。"
        )

    outline = _chapter_outline("gen_api", rows)
    parts: list[PlanPart] = []
    batch: list[tuple[str, str, int]] = []  # (编号, 章节名, 字数上限)
    used = 0

    def flush() -> None:
        nonlocal batch, used
        if not batch:
            return
        parts.append(_chapter_part(len(parts) + 1, batch, outline))
        batch = []
        used = 0

    for number, name, limit in rows:
        # 单章就超预算 → 独占一片（再切就不是"一章"了）
        if limit >= _PART_BUDGET_CHARS:
            flush()
            batch = [(number, name, limit)]
            flush()
            continue
        if used + limit > _PART_BUDGET_CHARS:
            flush()
        batch.append((number, name, limit))
        used += limit
    flush()

    return DocumentPlan(kind="api", parts=tuple(parts), source="gen_api.md §章节清单")


def _chapter_part(index: int, batch: list[tuple[str, str, int]], outline: str) -> PlanPart:
    """把一批章节合成一个分片。"""
    labels = "、".join(f"第 {number} 章 {name}" for number, name, _ in batch)
    span = _span_label([number for number, _, _ in batch])
    limits = "\n".join(f"- 第 {number} 章 {name}：字数上限 {limit}" for number, name, limit in batch)
    return PlanPart(
        index=index,
        label=f"{span}（{len(batch)} 章）",
        scope=labels,
        spec=(
            f"本次只输出这些章节：{labels}。\n\n"
            f"各章字数上限：\n{limits}\n\n"
            "跨片要求：**不要重复文档标题**、不要写前言 / 目录 / 结语 / 变更记录，"
            "只输出本次范围内的章节（标题层级与模板一致）。"
        ),
        outline=outline,
    )


def _span_label(numbers: list[str]) -> str:
    """给人看的范围标签：连续数字压缩成「第 1–6 章」，非数字（附 A）原样列。"""
    if all(number.isdigit() for number in numbers) and len(numbers) > 1:
        return f"第 {numbers[0]}–{numbers[-1]} 章"
    if len(numbers) == 1:
        return f"第 {numbers[0]} 章"
    return "第 " + "、".join(numbers) + " 章"


# ---------------------------------------------------------------- 提示词套件


def _prompts_plan() -> DocumentPlan:
    """套件：解析 `gen_prompts.md` 的「## 目录结构」代码块，按结构分批。

    分法（由结构本身决定，不写死文件名）：
    1. 可变组**之前**的固定文件（00 / 01 / 02 / 03 …）
    2. 可变组本身（`04-step-NN-<模块名>` —— 随项目规模增长的那部分）
    3. 可变组**之后**的固定文件（05-verify …）
    """
    files = _parse_suite_files(load_prompt("gen_prompts"))
    if not files:
        raise DocumentPlanError(
            "gen_prompts.md 的「## 目录结构」代码块里没解析出文件清单 —— 分片清单依赖它。"
        )

    variable = [name for name in files if _PLACEHOLDER_RE.search(name)]
    if not variable:
        # 结构里没有可变组：那就按"每个文件一片"太碎，改成整份一片并**如实说明**
        return DocumentPlan(
            kind="prompts",
            parts=(
                PlanPart(
                    index=1,
                    label=f"整份（{len(files)} 个文件）",
                    scope="、".join(files),
                    spec="按本提示词内嵌的多文件输出约定，输出本次范围内的全部文件。",
                    outline=_file_outline(files),
                ),
            ),
            source="gen_prompts.md §目录结构（无可变组）",
        )

    first_variable = files.index(variable[0])
    last_variable = len(files) - 1 - files[::-1].index(variable[-1])
    groups: list[tuple[str, list[str]]] = [
        ("固定文件（前段）", files[:first_variable]),
        ("功能实现步骤（随项目规模增长）", files[first_variable : last_variable + 1]),
        ("固定文件（后段）", files[last_variable + 1 :]),
    ]
    groups = [(label, names) for label, names in groups if names]

    outline = _file_outline(files)
    parts: list[PlanPart] = []
    for label, names in groups:
        index = len(parts) + 1
        # ⚠️ 可变组**不能把示例文件名当成本次范围**：模板里写的是
        # `04-step-01-<模块名>.md` / `04-step-02-<模块名>.md` 后面跟一个省略号，
        # 那只是**示意**。照抄成"只输出这两个文件"会把步骤数卡死在 2 个 ——
        # 而真实项目按 PRD 的功能条数产出（实测同一条 PRD 出了 4 个步骤文件）。
        # 所以可变组只说"规格"，不枚举文件名。
        is_variable = any(_PLACEHOLDER_RE.search(name) for name in names)
        if is_variable:
            scope = (
                "功能实现步骤的全部文件（每个功能步骤一个文件，文件名沿用模板的目录结构："
                f"编号 + 模块名，例如 {names[0]}）。"
                "**步骤数量由 PRD 的功能需求决定**，按依赖顺序编号，不要为了凑数拆分或合并步骤。"
            )
        else:
            scope = "、".join(names)
        parts.append(
            PlanPart(
                index=index,
                label=f"{label}（{len(names)} 个文件）",
                scope=scope,
                spec=(
                    f"本次只输出这些文件：{scope}\n\n"
                    "每个文件都要以 `=== FILE: <相对路径> ===` 分隔行开头，"
                    "分隔行格式与模板完全一致；**不要输出本次范围之外的文件**"
                    "（尤其不要重复其它分片已经产出的文件）。"
                ),
                outline=outline,
            )
        )

    return DocumentPlan(kind="prompts", parts=tuple(parts), source="gen_prompts.md §目录结构")


# ---------------------------------------------------------------- 解析


def _parse_chapter_table(text: str) -> list[tuple[str, str, int]]:
    """从「## 章节清单」表里取 `(编号, 章节名, 字数上限)`。

    只认表头含「章节」和「字数上限」的那张表 —— 同一个文件里还有好几张表
    （输入映射、默认约定…），认错表会得到一堆噪音。
    """
    rows: list[tuple[str, str, int]] = []
    in_section = False
    header_seen = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            # 只在「章节清单」这一节里找表；遇到下一个标题就收工
            if in_section and rows:
                break
            in_section = "章节清单" in stripped
            header_seen = False
            continue
        if not in_section or not stripped.startswith("|"):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if len(cells) < 5:
            continue
        if not header_seen:
            header_seen = True
            continue  # 表头
        if set(cells[0]) <= {"-", " "}:  # 分隔行 | --- | --- |
            continue
        match = _NUMBER_RE.search(cells[4])
        if not match:
            continue
        rows.append((cells[0], cells[1], int(match.group())))
    return rows


def _parse_suite_files(text: str) -> list[str]:
    """从「## 目录结构」的代码块里取文件名（保持顺序，去掉 `...` 这类省略行）。"""
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
        # 形如 `├── 00-README.md` / `└── 05-verify.md`
        candidate = stripped.lstrip("│├└─ ").strip()
        if not candidate or candidate in {"...", "…"} or not candidate.endswith(".md"):
            continue
        if candidate not in files:
            files.append(candidate)
    return files


def _chapter_outline(name: str, rows: list[tuple[str, str, int]]) -> str:
    """整份章节清单（进 `{doc_outline}`）。"""
    listing = "\n".join(f"- 第 {number} 章 {title}（字数上限 {limit}）" for number, title, limit in rows)
    return f"{name} 的完整结构（本次只生成其中一部分，其余由别的分片产出）：\n{listing}"


def _file_outline(files: list[str]) -> str:
    listing = "\n".join(f"- {name}" for name in files)
    return f"套件的完整文件清单（本次只生成其中一部分，其余由别的分片产出）：\n{listing}"


def _outline_or_default(prompt_name: str, default: str) -> str:
    """PRD 用的兜底 outline：解析不出来时给一句人话，而不是让占位符空着。

    ⚠️ 这里**允许兜底**：`{doc_outline}` 只是给模型的上下文，缺失不会造成静默截断；
    而清单解析失败（上面两个 `_*_plan`）会**抛错** —— 因为那会让分片失效。
    """
    try:
        rows = _parse_chapter_table(load_prompt(prompt_name))
    except Exception:  # noqa: BLE001 - 兜底不该把生成打挂
        return default
    return _chapter_outline(prompt_name, rows) if rows else default
