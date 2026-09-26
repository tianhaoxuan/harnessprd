"""由表单配置生成「表单提交数据」的 JSON Schema。

单一来源原则：``core/questions_config.json`` 是唯一契约，本脚本把它的形状翻译成
JSON Schema，供前端预校验与后端入参校验共用。**改题目请改配置再重跑本脚本，
不要手改生成的 schema** —— 手改就会变成第二份契约，两边迟早对不上。

生成的约束与配置的对应关系：

===========================  ==========================================
配置字段                      生成的 JSON Schema 约束
===========================  ==========================================
``required: true``          进 ``required`` 数组；文本题再加 ``minLength: 1``
``type: select / radio``    ``enum`` 取自 ``options``（选填题额外允许 ``""``）
``type: text / textarea``   ``maxLength`` 取自 ``maxLength``
（固定）                      ``additionalProperties: false``，拼错 key 直接报错
===========================  ==========================================

注意：``covers_dimensions``（F3.12 的追问维度去重）**不**在本 schema 里表达。
它要求「某维度至少有一个字段已填写且非空」，属于跨字段业务逻辑，
JSON Schema 表达起来晦涩且难维护，放在服务层实现。

运行（在 backend/ 目录下）：

    .venv\\Scripts\\python scripts\\gen_form_schema.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

CORE_DIR = BACKEND_DIR / "core"
CONFIG_PATH = CORE_DIR / "questions_config.json"
SCHEMA_PATH = CORE_DIR / "form_submission.schema.json"

SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"

CHOICE_TYPES = {"select", "radio"}
TEXT_TYPES = {"text", "textarea"}


def _build_property(question: dict[str, Any]) -> dict[str, Any]:
    """把一道题翻译成一个 JSON Schema 属性定义。"""
    qtype = question["type"]
    prop: dict[str, Any] = {
        "title": question["label"],
        "description": question["description"],
        "type": "string",
    }

    if qtype in CHOICE_TYPES:
        branches: list[dict[str, Any]] = [{"enum": list(question["options"])}]
        if not question["required"]:
            # 选填的选择题：控件未选择时前端常直接提交空串，允许它通过，
            # 语义等同「未作答」，与省略该 key 一致。
            branches.append({"const": ""})
        prop["anyOf"] = branches
    elif qtype in TEXT_TYPES:
        prop["maxLength"] = question["maxLength"]
        if question["required"]:
            # 必填题不允许空串：仅靠 required 只能保证「key 存在」
            prop["minLength"] = 1
    else:  # pragma: no cover - 配置里出现新类型时应当直接失败
        raise ValueError(f"未知的题目类型：{qtype!r}（题目 id={question['id']}）")

    return prop


def load_config() -> dict[str, Any]:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def build_schema(config: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """返回 (schema, 全部题目)。题目顺序保留配置中的顺序。"""
    questions = [*config["base_questions"], *config["advanced_questions"]]

    ids = [question["id"] for question in questions]
    duplicates = sorted({qid for qid in ids if ids.count(qid) > 1})
    if duplicates:
        raise ValueError(f"题目 id 重复：{'、'.join(duplicates)}")

    schema: dict[str, Any] = {
        "$schema": SCHEMA_DIALECT,
        "title": "产品需求表单提交数据",
        "description": (
            "用户填完表单后提交给后端的数据。对象顶层直接以题目 id 为 key、"
            "用户作答为 value。未作答的选填题可以省略该 key（或传空串）。"
            f"由 scripts/gen_form_schema.py 从 core/questions_config.json 生成，"
            f"对应表单版本 {config['version']}，请勿手改。"
        ),
        "type": "object",
        "properties": {question["id"]: _build_property(question) for question in questions},
        "required": [question["id"] for question in questions if question["required"]],
        # 拼错的 key 必须报错，否则用户的答案会被静默丢掉
        "additionalProperties": False,
        # 非标准扩展字段：记录这份 schema 对应的表单版本与来源
        "x-form-version": config["version"],
        "x-generated-from": "core/questions_config.json",
    }
    return schema, questions


def main() -> int:
    config = load_config()
    schema, questions = build_schema(config)

    SCHEMA_PATH.write_text(
        json.dumps(schema, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    required = schema["required"]
    choice = [q["id"] for q in questions if q["type"] in CHOICE_TYPES]
    print(f"已生成 {SCHEMA_PATH.relative_to(BACKEND_DIR)}")
    print(f"  表单版本：{config['version']}")
    print(f"  题目总数：{len(questions)}（必填 {len(required)}）")
    print(f"  选择类：{len(choice)} 题；文本类：{len(questions) - len(choice)} 题")
    print(f"  必填项：{'、'.join(required)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
