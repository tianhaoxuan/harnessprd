import { Check } from 'lucide-react'

import { STEPS, VIEW_TO_STEP, type ViewState } from '../types'

interface StepProgressProps {
  viewState: ViewState
}

/**
 * 顶部步骤条。
 *
 * ⚠️ 进度**不是**前端自己判断出来的"我觉得走到哪了"，而是
 * `viewState`（将来由服务端 `snapshot.state` 推导）的一个投影 ——
 * 见 `types/index.ts` 里 `ViewState` 的说明。
 */
export default function StepProgress({ viewState }: StepProgressProps) {
  const current = VIEW_TO_STEP[viewState]

  return (
    <nav aria-label="流程进度">
      <ol className="flex items-center gap-2">
        {STEPS.map((step, index) => {
          const isDone = index < current
          const isActive = index === current

          return (
            <li key={step.id} className="flex min-w-0 flex-1 items-center gap-2">
              <div className="flex min-w-0 items-center gap-2">
                <span
                  aria-current={isActive ? 'step' : undefined}
                  className={[
                    'flex h-6 w-6 shrink-0 items-center justify-center rounded-full text-xs font-medium transition',
                    isDone
                      ? 'bg-primary-600 text-white'
                      : isActive
                        ? 'bg-primary-100 text-primary-800 ring-2 ring-primary-300'
                        : 'bg-slate-200 text-slate-500',
                  ].join(' ')}
                >
                  {isDone ? <Check className="h-3.5 w-3.5" aria-hidden /> : index + 1}
                </span>
                <span
                  className={[
                    'hidden truncate text-xs sm:inline',
                    isActive
                      ? 'font-medium text-primary-800'
                      : isDone
                        ? 'text-slate-600'
                        : 'text-slate-400',
                  ].join(' ')}
                >
                  {step.label}
                </span>
              </div>

              {index < STEPS.length - 1 && (
                <span
                  aria-hidden
                  className={[
                    'h-px flex-1 transition',
                    index < current ? 'bg-primary-400' : 'bg-slate-200',
                  ].join(' ')}
                />
              )}
            </li>
          )
        })}
      </ol>
    </nav>
  )
}
