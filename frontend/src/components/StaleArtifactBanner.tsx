/**
 * 「上游已更新，这一份可能过期」横幅（05 篇）。挂在接口文档 / 提示词审核页顶部。
 *
 * ## 为什么是 amber 而不是红色，为什么按钮是"建议"
 *
 * 过期**不是错误**：PRD 改了两行、接口文档大体重合，用户完全可能选择"先不重新生成，
 * 我自己核对一遍"。所以这里只提示 + 给一个出口，不拦任何操作
 * （工单 §二 明确写了 inform only，不阻断编辑与导出）。
 *
 * 真正会拦住下游生成的是**需求基线**（`BaselineConfirmBanner`）：那一条是"你还没认可
 * 这一版需求"，这一条只是"你认可了 v2，而手上这份是按 v1 生成的"。
 *
 * 文案**不在前端拼**：整句由后端 `artifact-lineage` 给（与交付包 README / manifest
 * 里写的是同一句）。前端只负责画 —— 于是"界面上说的话"与"包里写的话"不会分叉。
 */

import { AlertTriangle, RefreshCw } from 'lucide-react'

export interface StaleArtifactBannerProps {
  /** 后端给的整句提示（如「PRD 已更新至 v3，当前接口文档基于 v2。建议重新生成或手动核对。」） */
  message: string
  /** 重新生成（走现有的 Job 流程）。不传则只显示提示 */
  onRegenerate?: () => void
  busy?: boolean
  /** 按钮文案里的产物名（如「接口文档」） */
  artifactLabel: string
}

export default function StaleArtifactBanner({
  message,
  onRegenerate,
  busy = false,
  artifactLabel,
}: StaleArtifactBannerProps) {
  return (
    <div
      data-testid="stale-artifact-banner"
      className="flex flex-wrap items-center gap-x-3 gap-y-2 rounded-lg border border-amber-200 bg-amber-50 px-3 py-2"
    >
      <AlertTriangle className="h-3.5 w-3.5 shrink-0 text-amber-600" aria-hidden />
      <span className="min-w-0 flex-1 text-xs text-amber-900">{message}</span>
      {onRegenerate && (
        <button
          type="button"
          data-testid="stale-regenerate"
          onClick={onRegenerate}
          disabled={busy}
          className="inline-flex shrink-0 items-center gap-1.5 rounded border border-amber-300 bg-white px-2 py-1 text-xs font-medium text-amber-800 transition hover:bg-amber-100 disabled:cursor-not-allowed disabled:text-slate-400"
        >
          <RefreshCw className={`h-3 w-3 ${busy ? 'animate-spin' : ''}`} aria-hidden />
          重新生成{artifactLabel}
        </button>
      )}
    </div>
  )
}
