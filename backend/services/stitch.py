"""分片拼接：把"按计划逐片生成"的多段正文拼成一份产物。

## 为什么它必须是一份实现

"接口文档 / 提示词套件整份生成会被输出上限截断"（`HANDOFF.md` §4 坑 #18）→
必须分片；而分片之后**一定要有人把它们拼起来**。目前有三个调用方：

| 调用方 | 场景 |
| --- | --- |
| `frontend/src/services/stitch.ts` | 浏览器逐片调用后拼接（前台流式那套） |
| `services/job_runner.py` | 后台任务逐片生成后拼接（本次） |
| `scripts/split_check.py` | 分片验收脚本，证明"按计划分片后不再截断" |

前端的 TypeScript 版无法与后端共用（不同语言），但**后端这两处必须共用一份** ——
两份 Python 实现会在某次"顺手改一下空片处理"之后悄悄分叉，而症状是
"验收脚本说完整、任务里的产物却少一段"，极难联想到拼接逻辑。

## 拼接规则（刻意保守）

只做两件**不会改变文档语义**的清理：

1. 丢掉**空片段**（模型偶尔回一段纯空白）；
2. 丢掉与首片重复的**一级标题**（`# ` 开头且与第一片的一级标题逐字相同的行）。

刻意**不**做更聪明的事（补全半截表格、重排章节号、修复 Markdown 结构）：
那是"猜模型想写什么"，猜错的产物看起来完全正常。宁可留一点粗糙，
也不要一份被程序改过的、与模型输出对不上的文档。
"""

from __future__ import annotations

from collections.abc import Iterable


def stitch_parts(pieces: Iterable[str]) -> str:
    """把各片按顺序拼成一份。片段之间用空行连接（Markdown 的段落分隔）。

    与前端 `stitchParts()` 同逻辑：**首片的一级标题为准**，后续片子里重复出现同一个
    一级标题时把它删掉 —— 分片提示词会要求每片"从某个章节继续"，
    但模型经常仍然从文档标题开始写一遍。
    """
    kept: list[str] = []
    first_heading: str | None = None
    for piece in pieces:
        text = piece.strip()
        if not text:
            continue
        lines = text.split("\n")
        first_index = next((i for i, line in enumerate(lines) if line.strip()), -1)
        first_line = lines[first_index].strip() if first_index >= 0 else ""
        if first_line.startswith("# "):
            if first_heading is None:
                first_heading = first_line
            elif first_line == first_heading:
                del lines[first_index]
                text = "\n".join(lines).strip()
                if not text:
                    continue
        kept.append(text)
    return "\n\n".join(kept)
