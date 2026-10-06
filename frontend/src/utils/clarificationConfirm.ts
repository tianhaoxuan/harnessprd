/**
 * 生成 PRD 前的**澄清二次确认**（纯文案 + 一次 `window.confirm`）。
 *
 * 为什么单独一个模块而不是写在 `App.tsx` 里：文案要能被断言、也要能被将来的
 * 「导出待确认清单」之类的入口复用；而 `App.tsx` 只该留一行调用。
 *
 * ⚠️ **取消 ≠ 禁止**：用户取消只是"先不生成"，不是"不许生成"。
 * 真正的硬拦截只有一条 —— 没有结构化摘要（见 `handleGeneratePrdWithSync`）。
 */

import type { ClarificationState } from '../types'

/**
 * 拼确认弹窗的正文。
 *
 * 用 `window.confirm` 而不是自绘弹窗：与「跳过接口文档」那颗按钮同一套做法，
 * 原生 dialog 不会被误当成页面内容、也不引入弹窗组件与焦点管理。
 */
export function buildClarificationConfirmText(state: ClarificationState): string {
  const lines: string[] = ['澄清对话还没收口 —— 现在生成，下面这些内容可能写不进 PRD：']

  if (state.openQuestions.length > 0) {
    lines.push('', '待确认的问题：')
    for (const [index, question] of state.openQuestions.entries()) {
      lines.push(`${index + 1}. ${question}`)
    }
  }
  if (state.warnings.length > 0) {
    lines.push('', '提醒：')
    for (const warning of state.warnings) lines.push(`· ${warning}`)
  }

  lines.push(
    '',
    '点「取消」可以回到对话里补一轮再生成；已经想清楚了就点「确定」。',
    '确定现在生成 PRD 吗？',
  )
  return lines.join('\n')
}

/**
 * 需要确认时弹窗；**不需要确认时直接放行**（调用方因此可以写一行 `if (!... ) return`）。
 *
 * @returns `true` = 继续生成
 */
export function confirmGenerateDespiteClarificationWarning(state: ClarificationState): boolean {
  if (!state.needsConfirmBeforeGenerate) return true
  return window.confirm(buildClarificationConfirmText(state))
}
