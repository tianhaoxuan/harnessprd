/**
 * 质量报告侧栏（04 篇）—— **三份产物都挂**，展示确定性结构校验的结果。
 *
 * ## 它和「审查意见」不是一回事（面板上必须说清）
 *
 * - 审查意见（`ReviewResultPanel`）：**模型**做语义审查，只有 PRD 有，看的是"有没有编造/漏项"；
 * - 质量报告（这里）：**纯代码规则**做结构校验，三份产物都有，看的是"章节齐不齐、
 *   MVP 是不是表格、接口分区/路径前缀/编号在不在"。
 *
 * 两者叠在侧栏同一个槽位里，所以标题与副标题都写死"格式与结构"——
 * 不写的话用户会把两者当成同一套结论的不同显示。
 *
 * ## 展示规则
 *
 * | 情况 | 显示 |
 * | --- | --- |
 * | 这一版的 metadata 有 `quality_gate` | 总分 + high 未过计数 + 逐条清单 |
 * | 没有（老数据 / v1 迁移 / 校验没跑） | `暂无质量报告`，不报错 |
 *
 * ⚠️ 分数与 `passed` **都不是"能不能下发"的门**：04 篇明确"校验失败不触发 Rewrite、
 * 不阻止任何操作"，这里只报告。
 */

import { useState } from 'react'
import { AlertCircle, Check, ChevronDown, ChevronRight, MinusCircle } from 'lucide-react'

import type { QualityGateCheck, QualityGateResult } from '../types/qualityGate'

export interface QualityReportPanelProps {
  result: QualityGateResult
  className?: string
}

/** 固定的副标题：和「审查意见」划清界限（别改写）。 */
export const QUALITY_DISCLAIMER = '格式与结构规则检查（不调模型）'

const SEVERITY_LABEL: Record<QualityGateCheck['severity'], string> = {
  high: '必须',
  medium: '建议',
  low: '提示',
}

/** 失败项按严重度排前面（high → medium → low），同档内保持后端给的顺序。 */
export function sortChecks(checks: QualityGateCheck[]): QualityGateCheck[] {
  const order: Record<QualityGateCheck['severity'], number> = { high: 0, medium: 1, low: 2 }
  return [...checks].sort((left, right) => {
    if (left.passed !== right.passed) return left.passed ? 1 : -1
    return order[left.severity] - order[right.severity]
  })
}

/** 分数配色：全过=绿、有 high 没过=红、其它=琥珀。 */
function scoreTone(result: QualityGateResult): string {
  if (result.passed) return 'bg-emerald-100 text-emerald-800'
  return 'bg-rose-100 text-rose-800'
}

export default function QualityReportPanel({ result, className = '' }: QualityReportPanelProps) {
  // 有失败项时默认展开（用户需要立刻看到哪里没过）；全过时折起来不占地方
  const [open, setOpen] = useState(!result.passed)
  const failed = result.checks.filter((check) => !check.passed)
  const failedHigh = failed.filter((check) => check.severity === 'high')
  const ordered = sortChecks(result.checks)

  return (
    <div data-testid="quality-report-panel" className={className}>
      <button
        type="button"
        data-testid="quality-report-toggle"
        onClick={() => setOpen((prev) => !prev)}
        aria-expanded={open}
        className="flex w-full items-center gap-1.5 text-left text-xs font-medium text-slate-700"
      >
        {open ? (
          <ChevronDown className="h-3.5 w-3.5 shrink-0" aria-hidden />
        ) : (
          <ChevronRight className="h-3.5 w-3.5 shrink-0" aria-hidden />
        )}
        质量报告
        <span
          data-testid="quality-report-score"
          className={`rounded px-1 py-0.5 text-[10px] ${scoreTone(result)}`}
        >
          {result.score} 分
        </span>
        {failedHigh.length > 0 && (
          <span className="rounded bg-rose-100 px-1 py-0.5 text-[10px] text-rose-800">
            {failedHigh.length} 项必须项未过
          </span>
        )}
      </button>
      <p className="mt-0.5 text-[11px] text-slate-400">{QUALITY_DISCLAIMER}</p>

      {open && (
        <div className="mt-1.5 flex flex-col gap-1.5">
          {failed.length === 0 ? (
            <p className="flex items-start gap-1.5 text-[11px] text-emerald-700">
              <Check className="mt-0.5 h-3 w-3 shrink-0" aria-hidden />
              <span>
                结构检查全部通过（{result.checks.length} 项）。
                {result.checks.length > 0 ? '格式层没有问题。' : ''}
              </span>
            </p>
          ) : (
            <p className="flex items-start gap-1.5 text-[11px] text-slate-500">
              <AlertCircle className="mt-0.5 h-3 w-3 shrink-0" aria-hidden />
              <span>
                {failed.length} / {result.checks.length} 项未过
                {failedHigh.length > 0
                  ? `（其中 ${failedHigh.length} 项是必须项）`
                  : '（必须项都过了，剩下的只是建议）'}
                。校验只报告、不拦生成。
              </span>
            </p>
          )}

          <ul className="flex flex-col gap-1">
            {ordered.map((check) => (
              <li
                key={check.id}
                data-testid={`quality-check-${check.id}`}
                data-passed={check.passed ? 'true' : 'false'}
                className={[
                  'rounded border px-2 py-1.5 text-[11px]',
                  check.passed
                    ? 'border-slate-200 bg-white text-slate-500'
                    : check.severity === 'high'
                      ? 'border-rose-200 bg-rose-50 text-rose-900'
                      : 'border-amber-200 bg-amber-50/70 text-amber-900',
                ].join(' ')}
              >
                <span className="flex items-start gap-1.5">
                  {check.passed ? (
                    <Check className="mt-0.5 h-3 w-3 shrink-0 text-slate-400" aria-hidden />
                  ) : (
                    <AlertCircle className="mt-0.5 h-3 w-3 shrink-0" aria-hidden />
                  )}
                  <span className="flex-1">
                    {check.label}
                    <span className="ml-1 text-[10px] text-slate-400">
                      {SEVERITY_LABEL[check.severity]}
                    </span>
                  </span>
                </span>
                {!check.passed && check.detail && (
                  <span className="mt-0.5 block pl-4 opacity-80">{check.detail}</span>
                )}
              </li>
            ))}
          </ul>

          <p className="text-[10px] text-slate-400">
            校验时刻：{result.checked_at}
          </p>
        </div>
      )}
    </div>
  )
}

/**
 * 「暂无质量报告」占位 —— 老数据（04 篇之前生成的版本、v1 迁移导入）走这一条。
 *
 * 为什么不直接不渲染：侧栏那一块空着会让人以为"是不是坏了"；
 * 而一句"暂无"同时说明了"这份产物没被校验过"，比空白诚实。
 */
export function QualityReportEmpty({ className = '' }: { className?: string }) {
  return (
    <div data-testid="quality-report-empty" className={className}>
      <p className="flex items-center gap-1.5 text-xs font-medium text-slate-400">
        <MinusCircle className="h-3.5 w-3.5" aria-hidden />
        质量报告
      </p>
      <p className="mt-0.5 text-[11px] text-slate-400">暂无质量报告（这一版生成时还没做结构校验）</p>
    </div>
  )
}
