"""表单题目配置的加载与形状。

`core/questions_config.json` 是**唯一来源** —— `docs/功能清单.md` F2.6 明确要求
「题目、选项、必填、长度上限全部外置，**禁止在代码里再硬编码一份**」
（上一版就出现过两份表单定义并存）。

本模块只做两件事：读它、给它一个类型。**不在这里做业务校验**
（去重、维度覆盖判定等属于服务层；schema 校验由 `core/form_submission.schema.json`
配合 `scripts/gen_form_schema.py` 承担 —— 那也是从同一份配置生成的）。

⚠️ 改了 `questions_config.json` 必须重跑 `scripts/gen_form_schema.py`，
并且按 `docs/状态数据设计.md` §6.2 的纪律升版本：加题 = minor，删题 / 改 `id` = **major**。
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

CORE_DIR = Path(__file__).resolve().parent
QUESTIONS_PATH = CORE_DIR / "questions_config.json"

QuestionType = Literal["text", "textarea", "select", "radio"]


class Question(BaseModel):
    """一道题。字段与 `questions_config.json` 一一对应。"""

    id: str
    label: str
    question: str
    description: str
    type: QuestionType
    options: list[str] = Field(
        default_factory=list, description="text / textarea 固定为空数组，消费方不必分支"
    )
    required: bool
    maxLength: int | None = Field(default=None, description="仅 text / textarea 有意义")
    covers_dimensions: list[str] = Field(
        default_factory=list,
        description="本题覆盖 F3.8 的哪些追问维度（用于 F3.12 已知信息去重）",
    )


class QuestionsConfig(BaseModel):
    """`/conversation/questions` 的响应体。

    `*_note` 字段是给维护者看的说明（维度判定规则、maxLength 口径），
    会一并返回 —— 前端可忽略。要精简响应体的话，在这里加 `exclude` 即可。
    """

    version: str = Field(description="表单版本。会话按创建时的 form_version 解释作答")
    title: str
    description: str
    covers_dimensions_note: str = ""
    maxLength_note: str = ""
    base_questions: list[Question] = Field(description="基础题，所有用户都填")
    advanced_questions: list[Question] = Field(description="选填题，界面建议默认折叠")

    @property
    def all_questions(self) -> list[Question]:
        """按配置顺序展开的全部题目（基础题在前）。"""
        return [*self.base_questions, *self.advanced_questions]


@lru_cache(maxsize=1)
def load_questions() -> QuestionsConfig:
    """读取并校验表单配置（按路径缓存）。

    配置是静态资源，进程内不会变 —— 缓存是安全的。
    测试里要重读用 ``load_questions.cache_clear()``。
    """
    raw = json.loads(QUESTIONS_PATH.read_text(encoding="utf-8"))
    return QuestionsConfig.model_validate(raw)


def form_version() -> str:
    """当前表单版本。会话创建时要把它写进 `form_version` 锚点。"""
    return load_questions().version
