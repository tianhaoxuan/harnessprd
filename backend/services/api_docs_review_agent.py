"""接口文档审查员（05 篇）：系统提示词 + human 消息组装。

## 为什么单独一个模块，而不是写进 `document_service`

审查有**两层**，分开是为了各自可测：

| 这一层 | 内容 | 怎么验 |
| --- | --- | --- |
| 本模块 | 提示词、human 消息怎么拼、输出怎么解析 | 纯函数，自检脚本直接喂字符串 |
| `document_service` | 真去调模型（`track(model, LlmStep.API_DOCS_REVIEW)`） | 走既有假模型路径 |

⚠️ **不是 `prd_review_agent.py` 的复制**：本仓库从来没有那个文件（PRD 的审查在
`document_service._review_prd` + `core/prompts/prd_review.md`）。所以"对齐"是**对齐那套做法**
（独立的 system 提示词 + human 模板 + `_extract_json_object` 解析 + 解析失败按"未审核"处理），
不是对齐一个不存在的文件。

## 输出契约（与 PRD 审查的差异）

PRD 审查输出 `{ok, issues}`；本审查输出 **`{passed, summary, issues:[{severity, section, problem,
suggestion}]}`**，多出 `summary` 与 `severity`。解析层把两者**归一化**成同一个形状
（`services/review_verdict.py`），于是版本 metadata 里的 `review` 只有一种结构，
前端的 `ReviewResultPanel` 不需要为两份产物写两套。
"""

from __future__ import annotations

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage

from core.prompts import load_prompt, render_prompt_text

#: `core/prompts/api_docs_review.md` 的 stem。
API_DOCS_REVIEW_PROMPT = "api_docs_review"

#: human 消息模板。占位符走 `render_prompt_text`（str.replace），**不是** str.format。
API_DOCS_REVIEW_HUMAN_TEMPLATE = """===== PRD 正文（接口文档的唯一依据）=====
{prd_content}

===== 待审接口文档 =====
{content}
"""


def build_api_docs_review_messages(*, prd_content: str, content: str) -> list[BaseMessage]:
    """组装"审一遍接口文档"的两条消息。

    ⚠️ 两份正文**都要给**：只给接口文档的话，"有没有编造"根本判不了 ——
    审核员必须能对照 PRD 才知道哪个字段是凭空的。
    """
    return [
        SystemMessage(content=load_prompt(API_DOCS_REVIEW_PROMPT)),
        HumanMessage(
            content=render_prompt_text(
                API_DOCS_REVIEW_HUMAN_TEMPLATE,
                {"prd_content": prd_content, "content": content},
            )
        ),
    ]
