/**
 * 把分片结果拼成整份文档。
 *
 * 两件事：
 * 1. **空片直接跳过** —— 一片什么都没产出（模型拒答 / 只回了空白）不该在文档中段留下空行。
 * 2. **保守去掉重复的文档标题**：计划里的 `spec` 已经要求"不要重复文档标题"，但模型不一定听话。
 *    判据刻意收得很窄 —— 只有"这一片的**首个非空行**是 H1、且与已经拼好的**首个非空行**
 *    逐字相同"才丢掉它。宁可偶尔留下一行重复标题，也不要误删正文里一个正常的 `#` 标题。
 *
 * ⚠️ 这里**不做** Markdown 结构修复（补表格、补代码围栏）。分片边界落在哪由模型决定，
 * 想靠一个前端函数把它修好是不现实的；真正的保证是让每一片自己写完整（计划的 `spec` 负责）。
 *
 * 出处：原本定义在 `App.tsx` 里。AppV2 的分步流程也要用它，而"从 V2 反向 import App"
 * 会把整个旧界面拖进打包结果，所以挪到这个共享模块（`App.tsx` 改成从这里 import）。
 */
export function stitchParts(pieces: string[]): string {
  const kept: string[] = []
  let firstHeading: string | null = null

  for (const piece of pieces) {
    let text = piece.trim()
    if (!text) continue
    const lines = text.split('\n')
    const firstIndex = lines.findIndex((line) => line.trim().length > 0)
    const firstLine = firstIndex >= 0 ? lines[firstIndex].trim() : ''
    if (firstLine.startsWith('# ')) {
      if (firstHeading === null) firstHeading = firstLine
      else if (firstLine === firstHeading) {
        lines.splice(firstIndex, 1)
        text = lines.join('\n').trim()
        if (!text) continue
      }
    }
    kept.push(text)
  }
  return kept.join('\n\n')
}
