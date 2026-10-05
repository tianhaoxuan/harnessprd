/**
 * 「导入 PRD」子视图（需求 §五.3）—— 把 `App` 里原来那块 PRD 粘贴区搬进表单屏。
 *
 * ## 为什么它长得和改造前那块一模一样
 *
 * 需求原话是"从现有区块**抽出**，**交互不变**"。所以这里刻意**一个控件都没加**：
 * 一个 PRD textarea、两颗按钮（「作为基准并继续」/「载入当前会话的 PRD」）、
 * 一个字符数、一条"已作为基准"的反馈。`data-testid` 也**原样沿用**
 * （`prd-import` / `prd-import-textarea` / `prd-import-confirm` / `prd-import-load` /
 * `prd-baseline-state`）—— 换名字等于把外面可能存在的脚本化检查悄悄弄丢。
 *
 * ⚠️ 需求里写的四颗按钮（「进入 PRD 环节」「直接生成接口文档」「进入当前环节」
 * 「直接生成提示词」）在本仓库**不存在**，`enterPrdReviewFromInput` /
 * `startApiDocsFromPrdInput` / `enterPromptsStageFromInput` / `startPromptsFromInput`
 * 也都不存在。现有的两颗按钮里，「作为基准并继续」已经承担了"往下走"的语义
 * （它会按 `entryMode` 把 `viewState` 抬到 `review-api-docs` 或 `review-prompts`
 * 并自动起生成），所以不需要再加按钮。
 *
 * ## 纯展示
 *
 * 所有动作由 `App` 通过 props 传入（那一层才持有 `importedPrd` / `entryMode` /
 * `writeDoc` / `pendingAutoGenerate`）。本组件不 import 任何服务、不碰状态机。
 */

export interface ImportPrdPanelProps {
  /** 子视图标题（`import-prd` 与 `import-prompts` 只有文案不同）。 */
  title: string
  /** 标题下的一句说明：这个入口跳过什么、接下来会发生什么。 */
  description: string
  value: string
  onChange: (value: string) => void
  /** 「作为基准并继续」—— `App.handleImportPrdAsBaseline`。 */
  onConfirmBaseline: () => void
  /** 「载入当前会话的 PRD」—— 把会话里已有的 PRD 填进输入框。 */
  onLoadCurrentPrd: () => void
  /** 当前会话里有没有可载入的 PRD（没有则那颗按钮禁用）。 */
  canLoadCurrentPrd: boolean
  /** 已作为基准：确认按钮变成「已作为基准 ✓」并禁用，避免重复点。 */
  baselineAccepted: boolean
  /** 「← 返回路径选择」。 */
  onBack: () => void
  className?: string
}

export default function ImportPrdPanel({
  title,
  description,
  value,
  onChange,
  onConfirmBaseline,
  onLoadCurrentPrd,
  canLoadCurrentPrd,
  baselineAccepted,
  onBack,
  className = '',
}: ImportPrdPanelProps) {
  return (
    <section
      data-testid="prd-import"
      className={['rounded-xl border border-slate-200 bg-white p-4 shadow-sm', className].join(' ')}
    >
      <button
        type="button"
        data-testid="import-back-to-chooser"
        onClick={onBack}
        className="mb-2 inline-flex items-center gap-1 text-xs text-slate-500 transition hover:text-slate-800"
      >
        ← 返回路径选择
      </button>

      <h2 className="text-sm font-medium text-slate-800">{title}</h2>
      <p className="mt-1 text-xs text-slate-500">{description}</p>

      <textarea
        data-testid="prd-import-textarea"
        value={value}
        onChange={(event) => onChange(event.target.value)}
        rows={8}
        placeholder="把已有 PRD 的 Markdown 全文粘贴到这里…"
        className="mt-3 w-full rounded-lg border border-slate-200 p-3 font-mono text-xs text-slate-700 focus:outline-none focus-visible:ring-2 focus-visible:ring-primary-400"
      />

      <div className="mt-3 flex flex-wrap items-center gap-2">
        <button
          type="button"
          data-testid="prd-import-confirm"
          onClick={onConfirmBaseline}
          disabled={!value.trim() || baselineAccepted}
          className="rounded-lg bg-primary-600 px-3 py-1.5 text-xs font-medium text-white transition hover:bg-primary-700 disabled:cursor-not-allowed disabled:bg-slate-300"
        >
          {baselineAccepted ? '已作为基准 ✓' : '作为基准并继续'}
        </button>
        <button
          type="button"
          data-testid="prd-import-load"
          onClick={onLoadCurrentPrd}
          disabled={!canLoadCurrentPrd}
          className="rounded-lg border border-slate-200 px-3 py-1.5 text-xs text-slate-600 transition hover:text-slate-800 disabled:cursor-not-allowed disabled:text-slate-300"
        >
          载入当前会话的 PRD
        </button>
        <span className="text-xs text-slate-400">{value.trim().length} 字符</span>
        {baselineAccepted ? (
          <span data-testid="prd-baseline-state" className="text-xs text-emerald-600">
            已作为基准，正在按这个入口往下走（想改内容就再编辑一次上面的文本）。
          </span>
        ) : null}
      </div>
    </section>
  )
}
