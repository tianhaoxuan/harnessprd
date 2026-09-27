/**
 * 流失败的提示条（纯展示）。
 *
 * 给用户两样东西：**人话的失败原因**，以及**可复制的请求 ID** ——
 * 后者是他能带给维护者的唯一线索（"生成失败了"没法排查）。
 * 不给：原始 API 错误体、堆栈、日志链接。
 */

interface StreamErrorBannerProps {
  message: string
  requestId?: string | null
  onCopyRequestId?: () => void
  /**
   * 是否渲染 `message`，默认 `true`。
   *
   * ⚠️ 存在的理由：`DocumentReview` 的 `failed` 分支**已经**把失败原因渲染成 `role="alert"`
   * 了，那里只缺"复制请求 ID"这一颗按钮。整条横幅再塞进去会把同一句话显示两遍、
   * 而且两层 rose 边框套在一起很难看。所以那条路径传 `showMessage={false}`。
   */
  showMessage?: boolean
}

export default function StreamErrorBanner({
  message,
  requestId,
  onCopyRequestId,
  showMessage = true,
}: StreamErrorBannerProps) {
  const copyButton = requestId ? (
    <button
      type="button"
      data-testid="copy-error-request-id"
      onClick={onCopyRequestId}
      className="shrink-0 rounded border border-rose-300 px-1.5 py-0.5 text-xs text-rose-700 transition hover:bg-rose-100"
    >
      复制请求 ID
    </button>
  ) : null

  // 只补按钮的形态：原因正文由调用方的失败横幅负责
  if (!showMessage) return copyButton
  if (!message) return null

  return (
    <p
      role="alert"
      data-testid="stream-error"
      className="flex flex-wrap items-center gap-2 rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-xs text-rose-800"
    >
      <span className="flex-1">{message}</span>
      {copyButton}
    </p>
  )
}
