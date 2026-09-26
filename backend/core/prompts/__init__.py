"""提示词资源：加载、组装、渲染。

放在 core/ 下而不是散落在 services/ 里，是为了让非开发同学也能直接改文案，
不需要读 Python 代码。

**组装规则**（`docs/对话阶段设计.md` §6.1、`gen_common.md` §组装方式）::

    system prompt = {族}_common.md + {族}_{阶段或产物}.md

顺序不可颠倒：角色基线与通用约定在前，阶段 / 产物专属规则在后。

**渲染规则**（`gen_common.md` §渲染方式警告、HANDOFF.md §4 坑 #1）::

    必须用 str.replace，禁止 str.format。
    提示词里除了运行时占位符（``{form_summary}``），还有给人看的格式记号
    （``{id}``、``{模块缩写}{3位序号}``）；str.format 会把后者也当占位符名 ——
    实测在 gen_api.md 上抛 ``KeyError``。

⚠️ ``str.replace`` 的另一面：对**没传进来**的键它是静默跳过、不报错。
缺注入的后果是占位符以字面量进入 prompt，而模型看到 ``{generate_scope}``
这类字样照样能输出像样的结果 —— 于是「验证通过」掩盖了「输入根本没送到」。
因此本模块提供 ``declared_placeholders()``，让调用方能把这类遗漏查出来。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path

PROMPT_DIR = Path(__file__).resolve().parent

# 「占位符清单」表格的行首形态：| `{name}` | ...
# 只匹配**行首紧跟反引号占位符**的行，因此不会误抓
# 「| **格式说明** | `{模块缩写}{3位序号}`、`{id}` | ... |」这类格式记号行。
_DECLARED_ROW = re.compile(r"^\|\s*`\{([a-z][a-z0-9_]*)\}`\s*\|", re.M)


@lru_cache(maxsize=None)
def load_prompt(name: str) -> str:
    """按名读取提示词（自动补 ``.md`` 后缀），结果按名缓存。"""
    filename = name if name.endswith(".md") else f"{name}.md"
    path = PROMPT_DIR / filename
    if not path.is_file():
        available = "、".join(available_prompts()) or "（无）"
        raise FileNotFoundError(f"提示词文件不存在：{filename}；当前可用：{available}")
    return path.read_text(encoding="utf-8").strip()


def available_prompts() -> list[str]:
    """列出全部提示词名（不含后缀），供健康检查展示。"""
    return sorted(path.stem for path in PROMPT_DIR.glob("*.md"))


def render_prompt(name: str, values: Mapping[str, str]) -> str:
    """把 ``{key}`` 逐个替换成 ``values[key]``。

    未出现在 values 里的占位符**原样保留**（这是 str.replace 的语义）。
    想知道漏了哪些，先用 ``declared_placeholders()`` 取声明清单再比对。
    """
    return render_prompt_text(load_prompt(name), values)


def render_prompt_text(text: str, values: Mapping[str, str]) -> str:
    """对已取到的文本做 ``{key}`` 替换。

    用 ``str.replace``，**禁用 ``str.format``**：提示词里既有运行时占位符
    （``{form_summary}``），也有给人看的格式记号（``{id}``、``{模块缩写}{3位序号}``），
    ``str.format`` 会把后者也当占位符名 —— 实测在 ``gen_api.md`` 上抛 ``KeyError``。
    """
    for key, value in values.items():
        text = text.replace("{" + key + "}", value)
    return text


def assemble_prompt_template(prompt_name: str) -> str:
    """按 ``{族}_common + {prompt_name}`` 组装，但**不渲染** —— 占位符原样保留。

    存在的理由：模块级常量不能渲染（渲染需要 ``values``），但常量也必须来自同一份
    ``.md``，否则就成了第二份提示词来源（`HANDOFF.md` §4 坑 #10）。
    给"需要一个具名常量"的场景留这个入口。

    族名取自下划线前的部分；该族没有 common 文件时只取自身。
    """
    family = prompt_name.split("_", 1)[0]
    common = f"{family}_common"
    parts = [load_prompt(prompt_name)]
    if common != prompt_name and (PROMPT_DIR / f"{common}.md").is_file():
        parts.insert(0, load_prompt(common))  # 顺序即契约：common 在前
    return "\n\n".join(parts)


def build_system_prompt(prompt_name: str, values: Mapping[str, str]) -> str:
    """按 ``{族}_common + {prompt_name}`` 组装并渲染 system prompt。

    - ``build_system_prompt("clarify_s2", ...)`` → ``clarify_common`` + ``clarify_s2``
    - ``build_system_prompt("gen_prd", ...)``    → ``gen_common`` + ``gen_prd``

    组装规则只有 ``assemble_prompt_template`` 一份实现 —— 不在这里再拼一次。
    """
    return render_prompt_text(assemble_prompt_template(prompt_name), values)


@lru_cache(maxsize=None)
def declared_placeholders(name: str) -> frozenset[str]:
    """解析提示词文件里的「占位符清单」表格，得到它声明的运行时占位符名。

    刻意**从文件解析**而不是在 Python 里维护一份列表：两份来源手工同步的
    错误率是 100%（HANDOFF.md §4 坑 #10）。

    注意：``clarify_common.md`` 的清单表把各阶段的占位符列在一起（第三列标注
    适用阶段），所以它声明的集合是**各阶段的并集**，比单个阶段实际需要的多。
    做「有没有漏注入」判断时要知道这一点。
    """
    return frozenset(_DECLARED_ROW.findall(load_prompt(name)))
