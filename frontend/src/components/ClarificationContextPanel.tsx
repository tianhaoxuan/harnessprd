/**
 * 澄清上下文的分轮统计（折叠，纯展示）。
 *
 * 收起时一行：「澄清 context：3 轮 · 最新 约 60k / 约 60k tokens」；
 * 展开后每轮一行：第 N 轮 · 输入 约 X / 约 Y tokens。
 * （`formatTokens` 自己带「约」字，所以这里不再重复写「约」。）
 *
 * 刻意**不显示百分比、不显示 budget_source / hardware_cap** —— 那些是调参用的，
 * 展示给用户只会被当成 bug（规格里明确"不给用户"）。
 */

import { useState } from 'react'

import type { ClarificationRound } from '../hooks/useGenerationObservability'
import { formatTokens } from './RunSummaryPanel'

interface ClarificationContextPanelProps {
  rounds: ClarificationRound[]
  defaultCollapsed?: boolean
}

export default function ClarificationContextPanel({
  rounds,
  defaultCollapsed = true,
}: ClarificationContextPanelProps) {
  const [collapsed, setCollapsed] = useState(defaultCollapsed)
  if (rounds.length === 0) return null

  const latest = rounds[rounds.length - 1]

  return (
    <div
      data-testid="clarification-context"
      className="rounded-lg border border-slate-200 bg-slate-50 px-3 py-2 text-xs text-slate-600"
    >
      <button
        type="button"
        onClick={() => setCollapsed((prev) => !prev)}
        className="flex w-full items-center gap-2 text-left"
      >
        <span aria-hidden>{collapsed ? '▸' : '▾'}</span>
        <span>
          澄清 context：{rounds.length} 轮 · 最新{' '}
          {formatTokens(latest.usage.estimated_input_tokens)} /{' '}
          {formatTokens(latest.usage.budget_tokens)} tokens
        </span>
      </button>

      {!collapsed && (
        <ul className="mt-2 flex flex-col gap-1">
          {rounds.map((round) => (
            <li key={round.index} className="flex flex-wrap gap-2 text-slate-500">
              <span>第 {round.index} 轮</span>
              <span className="text-slate-700">
                输入 {formatTokens(round.usage.estimated_input_tokens)} /{' '}
                {formatTokens(round.usage.budget_tokens)} tokens
              </span>
              <span>
                {round.usage.level === 'ok'
                  ? '正常'
                  : round.usage.level === 'warn'
                    ? '偏多'
                    : '超限'}
              </span>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
