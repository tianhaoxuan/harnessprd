/**
 * 步骤条的定义与映射（**纯函数，无 React 依赖**）。
 *
 * 为什么单独一个文件：步骤文案、phase→步骤的映射、以及"收了 run_summary 之后怎么收束"
 * 是**同一件事的三面**，散在页面里就会各写一份 —— 而它们必须一致，否则会出现
 * "步骤条说在修订、统计说没改过"的矛盾。
 *
 * ⚠️ 本仓库后端的阶段名是 `writing / review / rewriting / done`（不是 `*_started`）。
 */

import type { RunSummary } from '../types'
import type { PrdGenerationStage } from '../services/api'

export type StepStatus = 'pending' | 'active' | 'done'

export interface StepItem {
  id: string
  label: string
  status: StepStatus
}

export const PRD_STEP_DEFS = [
  { id: 'writer', label: '起草 PRD' },
  { id: 'review', label: '机器审查' },
  { id: 'rewrite', label: '修订 PRD' },
] as const

/**
 * 接口文档与提示词套件**两步**：先分片生成正文，再机器审查一次。
 *
 * ⚠️ 这里**曾经写过第二步又被删掉**，05 篇又加回来 —— 两次都不是反复：
 * 删是因为当时后端这两条流**只有一次模型调用、没有审查阶段**，那一步永远停在"待办"，
 * 直到 `done` 时两步同时打勾，等于当着用户的面声称"做过机器审查"；
 * 加回来是因为 05 篇真的给这两条接上了审查（`api_docs_review_agent` /
 * `prompts_review_agent`，各一次、不 Rewrite），现在它是**真实发生**的一步。
 */
export const API_DOCS_STEP_DEFS = [
  { id: 'generate', label: '生成接口文档' },
  { id: 'review', label: '机器审查' },
] as const

export const PROMPTS_STEP_DEFS = [
  { id: 'generate', label: '生成提示词套件' },
  { id: 'review', label: '机器审查' },
] as const

/** 审查阶段的说明：没有正文输出，不解释用户会以为卡死（实测踩过）。 */
export const REVIEW_STEP_HINT = '正在对照需求审查文档，通常需要 10–30 秒…'

function build(
  defs: readonly { id: string; label: string }[],
  active: number,
  doneCount: number,
): StepItem[] {
  return defs.map((def, index) => ({
    id: def.id,
    label: def.label,
    status: index < doneCount ? 'done' : index === active ? 'active' : 'pending',
  }))
}

/** 后端 phase → PRD 三步。 */
export function mapPrdPhaseToSteps(stage: PrdGenerationStage['stage']): StepItem[] {
  switch (stage) {
    case 'writing':
      return build(PRD_STEP_DEFS, 0, 0)
    case 'reviewing':
      return build(PRD_STEP_DEFS, 1, 1)
    case 'rewriting':
      return build(PRD_STEP_DEFS, 2, 2)
    case 'done':
      // 终态由 `finalizePrdStepsFromSummary` 收束 —— 这里先给"全部完成"的保守样子，
      // 真正是否跳过第 3 步要看 `revision_applied`
      return build(PRD_STEP_DEFS, -1, 3)
    default:
      return build(PRD_STEP_DEFS, 0, 0)
  }
}

/**
 * 收到 `run_summary` 后收束 PRD 三步。
 *
 * **必须有这一步**：审查通过时后端**不会**发 `rewriting`，只看 phase 的话第三步会永远
 * 停在"待办"；而 `revision_applied` 才是权威答案。
 */
export function finalizePrdStepsFromSummary(summary: RunSummary | null): StepItem[] {
  const revised = summary?.revision_applied === true
  return build(PRD_STEP_DEFS, -1, revised ? 3 : 2)
}

export function createApiDocsGeneratingSteps(): StepItem[] {
  return build(API_DOCS_STEP_DEFS, 0, 0)
}

/** 05 篇：进入"机器审查"这一格（第一格生成已完成）。 */
export function markApiDocsReviewing(): StepItem[] {
  return build(API_DOCS_STEP_DEFS, 1, 1)
}

export function markApiDocsStepsDone(): StepItem[] {
  return build(API_DOCS_STEP_DEFS, -1, API_DOCS_STEP_DEFS.length)
}

export function createPromptsGeneratingSteps(): StepItem[] {
  return build(PROMPTS_STEP_DEFS, 0, 0)
}

/** 05 篇：提示词套件进入"机器审查"这一格。 */
export function markPromptsReviewing(): StepItem[] {
  return build(PROMPTS_STEP_DEFS, 1, 1)
}

export function markPromptsStepDone(): StepItem[] {
  return build(PROMPTS_STEP_DEFS, -1, PROMPTS_STEP_DEFS.length)
}

/** 失败文案里"卡在哪一步"的人话（用当前步骤条里那个 active 项）。 */
export function getStepFailureLabel(steps: StepItem[]): string {
  return steps.find((step) => step.status === 'active')?.label ?? '生成'
}
