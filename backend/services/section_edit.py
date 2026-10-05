"""按标题替换文档里的一节 —— `frontend/src/components/DocumentReview.tsx` 的
`replaceSection()` 的**后端镜像**。

## 为什么后端也需要它

「AI 优化」的模型输出**只有被修订的那一节**（`optimize_document_stream` 的契约：
`generate_scope` 被强制对齐到 `section`，所以它不会重写整篇）。而两处要的都是**整篇**：

1. **会话快照**：`documents.prd.content` 是整篇 —— 把一节片段写进去等于把用户的文档
   替换成一段话（数据丢失）；
2. **刷新重连**：任务表的 `draft_content` 是"模型这一节写了多少"，前端要把它拼回整篇
   才能显示（它能拼，是因为 snapshot 里另外给了 `section` 和整篇原文）。

拼接发生在**收尾**（`job_runner._finish_optimize`），所以任务跑到一半时
`draft_content` 存的是**节的原文**，`result_json` 存拼好的全文。

## ⚠️ 这是**跨语言的两份实现**，行为必须一致

TS 那份在 `DocumentReview.tsx`（有浏览器测试盯着），这份在 Python。两者分叉的症状很隐蔽：
"服务端存下来的整篇"与"界面上显示的整篇"不一样，而两边看起来都正常。
所以下面的规则**逐条对应** TS 的实现，注释里点明每条的原因：

- 标题**逐字**匹配（模型给的 `section` 就是正文里那一行）；
- 找不到标题 → **原样返回**（宁可什么都不改，也不要把新内容追加到末尾 ——
  那看起来像"优化成功"，实际是文档里多了一节重复内容）；
- 新内容**自带同级或更高级标题**时才认为它替代了原标题，否则把原标题补回去；
- 边界是"下一个同级或更高级标题"，更深层的小节归本节所有；
- 围栏代码块里的 `#` **不算标题**（产物里大量出现 ```bash 与 `# 注释`）。

`scripts/job_check.py` 的「section_edit」一组断言覆盖上面每一条 —— 两份实现改一处忘
另一处时，至少有一边会红。
"""

from __future__ import annotations

import re

# ATX 标题：行首 1–6 个 `#` + 空白 + **至少一个非空白字符**
# ⚠️ 末尾那一段与 TS 的 `/^(#{1,6})\s+(\S.*)$/` 逐字对应：`## `（只有记号、没标题）
# **不算**标题 —— 算的话它会切出一个空标题的假小节，替换时把下一节吞掉。
_ATX_HEADING = re.compile(r"^(#{1,6})\s+(\S.*)$")


def replace_section(markdown: str, heading: str, replacement: str) -> str:
    """把 `heading` 那一节换成 `replacement`，返回整篇。

    Args:
        markdown: 优化前的**整篇**文档。
        heading: 要替换的节标题行（含 `##` 之类的记号），**逐字**。
        replacement: 模型给出的新内容（可能含标题，也可能只有正文）。

    Returns:
        替换后的整篇；`heading` 找不到时**原样返回**。
    """
    lines = markdown.split("\n")
    headings = _scan_headings(lines)
    position = next(
        (index for index, item in enumerate(headings) if lines[item[0]].rstrip() == heading.rstrip()),
        -1,
    )
    if position == -1:
        return markdown

    target_index, target_level = headings[position]
    end = next(
        (item[0] for item in headings[position + 1 :] if item[1] <= target_level),
        len(lines),
    )

    original_heading = lines[target_index].rstrip()
    polished = replacement.strip()
    first_line = polished.split("\n")[0] if polished else ""
    matched = _ATX_HEADING.match(first_line)
    # ⚠️ 判据是**层级**而不是"有没有标题"：模型常给出比本节低一级的子节标题
    # （`### 接口清单` 之于 `## 第 6 章 接口清单`），只看"有标题"会把本节标题整行删掉。
    carries_heading = matched is not None and len(matched.group(1)) <= target_level
    new_section = polished if carries_heading else f"{original_heading}\n\n{polished}"

    head = lines[:target_index]
    tail = lines[end:]
    joined = [*head, *new_section.split("\n"), *([""] if tail else []), *tail]
    return "\n".join(joined)


def _scan_headings(lines: list[str]) -> list[tuple[int, int]]:
    """扫出所有真正的标题，返回 `[(行号, 层级)]`。

    ⚠️ **必须跳过围栏代码块**：产物里大量出现 ```bash，而 bash 的注释就是 `#` ——
    不跳过的话 `# 安装依赖` 会被当成一级标题，切出一个假小节（TS 那份是同一条注释里
    写的实测结论）。
    """
    headings: list[tuple[int, int]] = []
    in_fence = False
    fence_marker = ""
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            marker = stripped[:3]
            if not in_fence:
                in_fence = True
                fence_marker = marker
            elif marker == fence_marker:
                in_fence = False
                fence_marker = ""
            continue
        if in_fence:
            continue
        matched = _ATX_HEADING.match(line)
        if matched:
            headings.append((index, len(matched.group(1))))
    return headings
