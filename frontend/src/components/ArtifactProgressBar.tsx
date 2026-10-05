import { Check } from 'lucide-react'

import type { ArtifactNode, ArtifactStatus } from '../utils/artifactProgress'

interface ArtifactProgressBarProps {
  /**
   * 要渲染的格子。**由调用方算好**（`utils/artifactProgress.buildArtifactNodes`）——
   * 本组件不认识 `ViewState` / `EntryMode`，也不判断"这一份有没有正文"，
   * 它只负责画。状态由内容推导这件事因此可以离线断言，不必先渲染出页面。
   */
  nodes: ArtifactNode[]
}

/**
 * 圆点那一档样式。**必须写完整类名**（不能拼 `bg-${color}-600`）：
 * Tailwind 是扫描源码里的完整类名生成 CSS 的，拼出来的它扫不到，样式会静默消失。
 *
 * | 状态 | 长相 |
 * | --- | --- |
 * | `done` | 绿底 + 勾 |
 * | `active` | 深色底白字（"你在这儿"） |
 * | `pending` | 灰底，且**不可点** |
 */
const CIRCLE_CLASS: Record<ArtifactStatus, string> = {
  done: 'bg-emerald-600 text-white',
  active: 'bg-slate-800 text-white',
  pending: 'bg-slate-200 text-slate-500',
}

/** 文案那一档样式，与圆点同一套三档。 */
const LABEL_CLASS: Record<ArtifactStatus, string> = {
  done: 'text-slate-600',
  active: 'font-medium text-slate-900',
  pending: 'text-slate-400',
}

/**
 * 顶部「产出物」进度条。
 *
 * ## 与它替换掉的 `StepProgress` 的差别
 *
 * | | `StepProgress`（流程进度） | 本组件（产出物） |
 * | --- | --- | --- |
 * | 一格是什么 | 一个流程步骤 | 一份产出物 |
 * | 打勾的判据 | "走到过这一步"（序号比大小） | **这份产物有没有正文** |
 * | 能不能点 | 每格都能点 | **只有已完成的能点** |
 * | 点去哪 | 那一屏（可能是空的） | 对应的 `review-*` |
 *
 * ## 为什么 `pending` 必须挂 `title`
 *
 * "点不动"本身不是问题，**不知道为什么点不动**才是。`pending` 的格子是
 * `disabled` 的，不给一句说明，用户只会以为进度条坏了（提示词 §四 点名的坑）。
 * 顺带一条：**绝不许让 `pending` 的格子"跳一个空页"** —— 所以这里 `disabled`
 * 与 `onClick` 是互斥的（见 `ArtifactNode.clickable`），不是"点了没反应"。
 */
export default function ArtifactProgressBar({ nodes }: ArtifactProgressBarProps) {
  return (
    <nav
      aria-label="产出物"
      data-testid="artifact-progress"
      className="flex items-center gap-3"
    >
      {/* 左侧文案：与 `aria-label` 用同一个词。屏幕上只此一处"这是产出物进度"的说明。 */}
      <span className="shrink-0 text-xs font-medium text-slate-500">产出物</span>

      <ol className="flex min-w-0 flex-1 items-center gap-2">
        {nodes.map((node, index) => (
          <li key={node.id} className="flex min-w-0 flex-1 items-center gap-2">
            <button
              type="button"
              data-testid={`artifact-${node.id}`}
              data-status={node.status}
              aria-current={node.status === 'active' ? 'step' : undefined}
              onClick={node.onClick}
              disabled={!node.clickable}
              title={node.title}
              className="flex min-w-0 items-center gap-2 text-left disabled:cursor-default"
            >
              <span
                className={[
                  'flex h-6 w-6 shrink-0 items-center justify-center rounded-full text-xs font-medium transition',
                  CIRCLE_CLASS[node.status],
                ].join(' ')}
              >
                {node.status === 'done' ? <Check className="h-3.5 w-3.5" aria-hidden /> : index + 1}
              </span>
              <span
                className={[
                  'hidden truncate text-xs sm:inline',
                  LABEL_CLASS[node.status],
                ].join(' ')}
              >
                {node.label}
              </span>
            </button>

            {index < nodes.length - 1 && (
              <span
                aria-hidden
                className={[
                  'h-px flex-1 transition',
                  node.status === 'done' ? 'bg-emerald-400' : 'bg-slate-200',
                ].join(' ')}
              />
            )}
          </li>
        ))}
      </ol>
    </nav>
  )
}
