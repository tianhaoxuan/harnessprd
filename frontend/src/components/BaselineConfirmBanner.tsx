/**
 * 需求基线横幅（05 篇）。挂在 PRD 审核页顶部（`DocumentReview` 的 `warnSlot`）。
 *
 * ## 两种样子，两种用意
 *
 * | 状态 | 样子 | 用意 |
 * | --- | --- | --- |
 * | 未确认 | amber 方块 + 说明 + 「确认需求基线」按钮 | 教用户**怎么做**，并说清确认后解锁什么 |
 * | 已确认 | 一行绿色短提示（基于 PRD v2 · 时间） | 只做**凭据**：让人知道当前这份 PRD 是过了目的 |
 *
 * ⚠️ **它不自带确认逻辑**（不自己去调接口）：确认要绑定的"当前 PRD 版本 id"只有上层
 * 拿得到（要走 `artifact-lineage`），而且确认之后要顺手把下游按钮解锁 ——
 * 那是编排层的事。这里只管画。
 */

import { AlertTriangle, BadgeCheck } from 'lucide-react'

export interface BaselineConfirmBannerProps {
  confirmed: boolean
  /** 已确认时的一行凭据文案（`describeBaseline().label` 给的，别在这里再拼一遍） */
  label: string
  /** Review 有 high 级问题时给个数：确认等于接受风险，要说出来 */
  highIssueCount?: number
  /** 确认按钮在途（拉版本号 + 存快照） */
  busy?: boolean
  onConfirm: () => void
}

export default function BaselineConfirmBanner({
  confirmed,
  label,
  highIssueCount = 0,
  busy = false,
  onConfirm,
}: BaselineConfirmBannerProps) {
  if (confirmed) {
    return (
      <p
        data-testid="baseline-confirmed"
        className="flex items-center gap-1.5 rounded-lg border border-emerald-200 bg-emerald-50 px-3 py-2 text-xs text-emerald-800"
      >
        <BadgeCheck className="h-3.5 w-3.5 shrink-0" aria-hidden />
        {label}
      </p>
    )
  }

  return (
    <div
      data-testid="baseline-confirm-banner"
      className="flex flex-wrap items-start gap-x-3 gap-y-2 rounded-lg border border-amber-300 bg-amber-50 px-3 py-2.5"
    >
      <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-amber-600" aria-hidden />
      <div className="min-w-0 flex-1 text-xs text-amber-900">
        <p className="font-medium">需求基线尚未确认</p>
        <p className="mt-0.5 text-amber-800">
          请通读 PRD。确认后将解锁接口文档与提示词生成 —— 它们都从这份 PRD 推导，
          确认过再生成，下游产物才对得上你认可的那一版需求。
        </p>
        {highIssueCount > 0 && (
          <p data-testid="baseline-confirm-risk" className="mt-1 text-amber-700">
            仍有 {highIssueCount} 个审查问题，确认即表示接受风险。
          </p>
        )}
      </div>
      <button
        type="button"
        data-testid="baseline-confirm"
        onClick={onConfirm}
        disabled={busy}
        className="inline-flex shrink-0 items-center gap-1.5 rounded-lg bg-amber-500 px-3 py-1.5 text-xs font-medium text-white transition hover:bg-amber-600 disabled:cursor-not-allowed disabled:bg-slate-300"
      >
        {busy ? '正在确认…' : '确认需求基线'}
      </button>
    </div>
  )
}
