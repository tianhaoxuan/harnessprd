import { AlertCircle, Check } from 'lucide-react'

import type { ClarificationState } from '../types'

/**
 * 澄清页顶栏的状态 badge：**对话收口了吗**。
 *
 * ## 它是什么、不是什么
 *
 * - ✅ **只读展示**：一句话说明当前状态，hover 能看到提醒（如果有）
 * - ❌ **不是折叠面板的入口**：待确认问题只在"点生成时"的确认弹窗里列出来，
 *   不在这里再开一个展开区（需求明确不做底栏折叠面板）
 * - ❌ **不是禁用按钮的理由**：状态是 `awaiting_user_reply` 时按钮照样可点，
 *   只是换成琥珀色 + ⚠ 并在点击时问一句（见 `clarificationConfirm.ts`）
 *
 * 三档配色与语义一一对应：绿 = 可以生成、琥珀 = 还有未回复的问题、灰 = 还在聊。
 */

const TONE: Record<ClarificationState['status'], string> = {
  ready: 'border-emerald-200 bg-emerald-50 text-emerald-800',
  awaiting_user_reply: 'border-amber-300 bg-amber-50 text-amber-800',
  collecting: 'border-slate-200 bg-slate-50 text-slate-600',
}

export default function ClarificationStatusBadge({ state }: { state: ClarificationState }) {
  return (
    <span
      data-testid="clarification-status"
      data-status={state.status}
      data-needs-confirm={state.needsConfirmBeforeGenerate ? 'true' : 'false'}
      // 悬停说明：badge 本身很短，提醒放在 title 里，不占版面
      title={state.warnings.length > 0 ? state.warnings.join('；') : undefined}
      className={[
        'inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-xs',
        TONE[state.status],
      ].join(' ')}
    >
      {state.status === 'ready' ? (
        <Check className="h-3 w-3" aria-hidden />
      ) : (
        <AlertCircle className="h-3 w-3" aria-hidden />
      )}
      {state.statusLabel}
    </span>
  )
}
