/**
 * 结构校验（quality_gate）的返回形状 —— 与后端 `services/quality_gate.py` 的
 * `run_quality_gate()` 逐字对齐（后端"未产生的字段一律省略"，所以可选字段用 `?`）。
 *
 * ## 与 `JobReview` 的分工（UI 文案也按这个说）
 *
 * | | 谁产出 | 看什么 |
 * | --- | --- | --- |
 * | `JobReview`（审查意见） | LLM Review Agent，**只有 PRD** | 语义：有没有编造、漏项、自相矛盾 |
 * | `QualityGateResult`（质量报告） | 纯代码规则，**三份产物都有** | 结构：章节齐不齐、MVP 是不是表格、分区/前缀/编号 |
 *
 * ## `passed` 只看 `high`
 *
 * `passed === true` 的含义是"**所有 high 检查都过**"，`medium` / `low` 失败只扣 `score`。
 * 所以界面上不能把 `passed === false` 简化成"不合格"——要连同"哪几条 high 没过"一起说。
 */

/** 一条检查项的严重度。`high` 进 `passed` 判定，另两档只影响分数。 */
export type QualityGateSeverity = 'high' | 'medium' | 'low'

export interface QualityGateCheck {
  /** 稳定 id，如 `prd.section.scope` / `api.section.inventory` / `prompts.section.verify` */
  id: string
  /** 给人看的一句话（后端给的中文，别在前端重写） */
  label: string
  passed: boolean
  severity: QualityGateSeverity
  /** 失败原因（通过时通常为 `null`） */
  detail?: string | null
}

export interface QualityGateResult {
  /** 所有 `severity === 'high'` 的检查都通过 */
  passed: boolean
  /** 加权通过率 0–100（high 权重 2，medium/low 权重 1） */
  score: number
  /** 校验时刻，带 `+08:00` 偏移 */
  checked_at: string
  /** 校验的是哪份产物 */
  doc_type: 'prd' | 'api-docs' | 'prompts'
  checks: QualityGateCheck[]
}
