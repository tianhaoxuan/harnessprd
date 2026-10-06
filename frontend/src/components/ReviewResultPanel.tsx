/**
 * 审查意见侧栏（需求 §6.5）—— 只给 PRD 挂。
 *
 * ## 展示规则（需求 §6.5 的表，判据是那一版的 `source_kind`）
 *
 * | 版本来源 | 是否显示 |
 * | --- | --- |
 * | `generate`（含重新生成） | 显示该次生成的 review |
 * | `optimize` / `auto_save` | 显示（是**生成时**那份，优化/手改后不刷新） |
 * | `checkpoint` / `restore` | **不显示** |
 *
 * 前两条合起来其实是同一件事：**只要这一版的正文是模型生成的那一份，就显示它那次审查**。
 * `checkpoint` / `restore` 出来的新版不是一个"被审过的稿子"（`checkpoint` 根本不继承
 * review，`restore` 继承的那份针对的是历史正文），所以不显示。
 *
 * ⚠️ 有一个后端事实会影响观感，值得写下来：`optimize` / `auto_save` **不会留在任何一行上**
 * （后端对这两种只就地改 current 的正文，不改那一行出生时的 `source_kind`）。
 * 所以"优化之后"看到的是那一版**出生时**的来源标签 —— 这正是需求要的效果
 * （"优化后仍显示生成时 review"），不是 bug。
 *
 * ## 副标题是**固定文案**
 *
 * 「基于生成时审查，修改后仅供参考」—— 不能省。用户改了正文之后审查结论就已经过期了，
 * 不说这句话会让"审核通过"看起来像对**现在**这份稿子的结论。
 */

import { useState } from 'react'
import { AlertCircle, CheckCircle2, ChevronDown, ChevronRight, ShieldQuestion } from 'lucide-react'

import type { JobReview } from '../types/job'

export interface ReviewResultPanelProps {
  review: JobReview
  className?: string
}

/** 固定的免责副标题（需求 §6.5 指定，别改写）。 */
export const REVIEW_DISCLAIMER = '基于生成时审查，修改后仅供参考'

/** 一条意见给人看的文案（`problem` 缺失时退回 `section`，都没有就说"未描述"）。 */
function issueText(issue: { section?: string; problem?: string; suggestion?: string }): {
  head: string
  suggestion?: string
} {
  const head = issue.problem ?? issue.section ?? '未描述'
  const prefix = issue.problem && issue.section ? `${issue.section}：` : ''
  return { head: `${prefix}${head}`, suggestion: issue.suggestion }
}

export default function ReviewResultPanel({ review, className = '' }: ReviewResultPanelProps) {
  const [open, setOpen] = useState(true)
  const count = review.issues?.length ?? 0
  const skipped = Boolean(review.review_skipped)

  return (
    <div data-testid="review-result-panel" className={className}>
      <button
        type="button"
        data-testid="review-toggle"
        onClick={() => setOpen((prev) => !prev)}
        aria-expanded={open}
        className="flex w-full items-center gap-1.5 text-left text-xs font-medium text-slate-700"
      >
        {open ? (
          <ChevronDown className="h-3.5 w-3.5 shrink-0" aria-hidden />
        ) : (
          <ChevronRight className="h-3.5 w-3.5 shrink-0" aria-hidden />
        )}
        审查意见
        <span
          className={[
            'rounded px-1 py-0.5 text-[10px]',
            skipped
              ? 'bg-amber-100 text-amber-800'
              : count === 0
                ? 'bg-emerald-100 text-emerald-800'
                : 'bg-amber-100 text-amber-800',
          ].join(' ')}
        >
          {skipped ? '未审核' : count === 0 ? '通过' : `${count} 条`}
        </span>
      </button>
      <p className="mt-0.5 text-[11px] text-slate-400">{REVIEW_DISCLAIMER}</p>

      {open && (
        <div className="mt-1.5 flex flex-col gap-1.5">
          {skipped ? (
            <p className="flex items-start gap-1.5 rounded border border-amber-200 bg-amber-50 px-2 py-1.5 text-[11px] text-amber-800">
              <ShieldQuestion className="mt-0.5 h-3 w-3 shrink-0" aria-hidden />
              <span>
                这次审核没跑成：审核员的输出解析不了，服务端按通过处理。
                {review.review_model ? `（原本要用的审核模型：${review.review_model}）` : ''}
                也就是说这一稿实际上没经过质检，请自己过一遍。
              </span>
            </p>
          ) : count === 0 ? (
            <p className="flex items-start gap-1.5 text-[11px] text-emerald-700">
              <CheckCircle2 className="mt-0.5 h-3 w-3 shrink-0" aria-hidden />
              <span>
                审核通过，没有发现问题。
                {review.review_model ? `（审核模型：${review.review_model}）` : ''}
              </span>
            </p>
          ) : (
            <ul className="flex flex-col gap-1.5">
              {(review.issues ?? []).map((issue, index) => {
                const text = issueText(issue)
                return (
                  <li
                    key={`${text.head}-${index}`}
                    className="rounded border border-amber-200 bg-amber-50/70 px-2 py-1.5 text-[11px] text-amber-900"
                  >
                    <span className="flex items-start gap-1.5">
                      <AlertCircle className="mt-0.5 h-3 w-3 shrink-0" aria-hidden />
                      <span className="flex-1">{text.head}</span>
                    </span>
                    {text.suggestion && (
                      <span className="mt-0.5 block pl-4 text-amber-800">{text.suggestion}</span>
                    )}
                  </li>
                )
              })}
            </ul>
          )}
          {count > 0 && (
            <p className="text-[11px] text-slate-400">
              重写有上限，改满一轮就停（避免无限循环），所以可能仍有未改完的意见。
            </p>
          )}
          {review.summary && (
            // 05 篇：接口文档 / 提示词的审查会给一句话总结（PRD 那条没有这个字段）。
            // 放在最下面当"结论一句"：有 issues 时用户先看清单，没 issues 时它就是全部内容。
            <p data-testid="review-summary" className="text-[11px] text-slate-500">
              {review.summary}
            </p>
          )}
        </div>
      )}
    </div>
  )
}
