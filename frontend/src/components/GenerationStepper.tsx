/**
 * 生成步骤条（纯展示）。给用户：「当前在第几步」+「已等待多久」。
 *
 * ⚠️ **不给百分比进度条**：步骤数固定但每一步耗时不可预测，百分比只能靠编 ——
 * 编出来的进度条会让用户以为"快好了"，实际还在审查。
 */

import { Check, Loader2 } from 'lucide-react'

import type { StepItem } from '../utils/generationSteps'

interface GenerationStepperProps {
  steps: StepItem[]
  elapsedSeconds: number
  hint?: string
}

export default function GenerationStepper({ steps, elapsedSeconds, hint }: GenerationStepperProps) {
  if (steps.length === 0) return null

  return (
    <div
      data-testid="generation-stepper"
      className="flex flex-wrap items-center gap-3 rounded-lg border border-slate-200 bg-white px-3 py-2"
    >
      <ol className="flex flex-wrap items-center gap-2">
        {steps.map((step, index) => (
          <li key={step.id} className="flex items-center gap-2">
            <span
              className={[
                'flex h-5 w-5 shrink-0 items-center justify-center rounded-full text-xs',
                step.status === 'done'
                  ? 'bg-emerald-100 text-emerald-700'
                  : step.status === 'active'
                    ? 'bg-primary-100 text-primary-700 ring-2 ring-primary-300'
                    : 'bg-slate-100 text-slate-400',
              ].join(' ')}
            >
              {step.status === 'done' ? (
                <Check className="h-3 w-3" aria-hidden />
              ) : step.status === 'active' ? (
                <Loader2 className="h-3 w-3 animate-spin" aria-hidden />
              ) : (
                <span aria-hidden>○</span>
              )}
            </span>
            <span
              className={[
                'text-xs',
                step.status === 'active'
                  ? 'font-medium text-primary-800'
                  : step.status === 'done'
                    ? 'text-slate-600'
                    : 'text-slate-400',
              ].join(' ')}
            >
              {step.label}
            </span>
            {index < steps.length - 1 && <span aria-hidden className="text-slate-300">→</span>}
          </li>
        ))}
      </ol>
      <span className="ml-auto text-xs tabular-nums text-slate-500">已等待 {elapsedSeconds}s</span>
      {hint && <span className="w-full text-xs text-slate-400">{hint}</span>}
    </div>
  )
}
