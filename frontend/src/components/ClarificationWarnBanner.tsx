/**
 * 澄清上下文接近上限时的提示条（amber，纯展示）。
 *
 * 文案刻意**不提 token、不提窗口大小、不给百分比**：用户要知道的是"聊得够多了，可以往下走"，
 * 不是"59,800 / 60,000 tokens"。技术术语只会让人以为出了问题。
 */

interface ClarificationWarnBannerProps {
  visible: boolean
  onGeneratePrd?: () => void
  /** 按钮文案（有的入口下没有"生成 PRD"这一步，就不传回调） */
  actionLabel?: string
}

export default function ClarificationWarnBanner({
  visible,
  onGeneratePrd,
  actionLabel = '生成 PRD',
}: ClarificationWarnBannerProps) {
  if (!visible) return null

  return (
    <div
      data-testid="clarification-warn"
      className="flex flex-wrap items-center gap-2 rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-800"
    >
      <span className="flex-1">已讨论较多内容。若无更多补充，可以结束澄清并生成 PRD。</span>
      {onGeneratePrd && (
        <button
          type="button"
          onClick={onGeneratePrd}
          className="shrink-0 rounded border border-amber-300 px-2 py-0.5 text-xs text-amber-800 transition hover:bg-amber-100"
        >
          {actionLabel}
        </button>
      )}
    </div>
  )
}
