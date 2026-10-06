"""提示词套件审查员（05 篇）：系统提示词 + human 消息组装。

结构上与 `api_docs_review_agent.py` 对称（两层分工、纯函数可测），差异只有两处：

1. **human 消息多一段可选的接口文档** —— 套件里的接口实现步骤必须能在接口文档里找到出处，
   而"跳过接口文档直接生成提示词"时那一段就是空的（这时审核员要按"没有接口文档"来判断，
   不能把"接口路径对不上"当问题）。
2. **审查重点不同**：功能覆盖、**自包含**（不依赖"见上文"）、与 PRD 术语/范围一致、
   五类文件是否都有实质内容。
"""

from __future__ import annotations

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage

from core.prompts import load_prompt, render_prompt_text

#: `core/prompts/prompts_review.md` 的 stem。
PROMPTS_REVIEW_PROMPT = "prompts_review"

#: human 消息模板。`{api_docs_content}` 为空时那一段只剩标题 —— 审核员据此知道
#: "这次没有接口文档可比对"（提示词里写明了这一点）。
PROMPTS_REVIEW_HUMAN_TEMPLATE = """===== PRD 正文（套件的唯一依据）=====
{prd_content}

===== 接口文档正文（可能为空：用户跳过了接口文档）=====
{api_docs_content}

===== 待审提示词套件（`=== FILE: xxx ===` 分隔）=====
{content}
"""


def build_prompts_review_messages(
    *, prd_content: str, content: str, api_docs_content: str = ""
) -> list[BaseMessage]:
    """组装"审一遍提示词套件"的两条消息。

    ⚠️ `api_docs_content` 为空**不是错误**：产品允许"跳过接口文档，直接生成提示词"。
    空着传进去，审核员会按"没有接口文档可比对"来审 —— 比编一份假的给它好。
    """
    return [
        SystemMessage(content=load_prompt(PROMPTS_REVIEW_PROMPT)),
        HumanMessage(
            content=render_prompt_text(
                PROMPTS_REVIEW_HUMAN_TEMPLATE,
                {
                    "prd_content": prd_content,
                    "api_docs_content": api_docs_content.strip(),
                    "content": content,
                },
            )
        ),
    ]
