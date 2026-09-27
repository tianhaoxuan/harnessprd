/**
 * 流错误的**友好文案**与 `request_id` 提取（纯函数，无 React 依赖）。
 *
 * 展示原则（规格）：给用户「怎么失败、等了多久、可复制的 ID」，不给原始 API 错误、
 * 不给 token 百分比、不给 context window 之类的术语。
 */

import { StreamError } from '../services/api'

export const BUDGET_EXCEEDED_CODE = 'TOKEN_BUDGET_EXCEEDED'

export function isBudgetExceeded(error: unknown): boolean {
  return error instanceof StreamError && error.code === BUDGET_EXCEEDED_CODE
}

/**
 * 取给用户看的错误文案。
 *
 * ⚠️ **超预算时不加 fallback 前缀**：后端那句中文已经解释清楚（"估算 input … 超过上限 …"），
 * 前面再套一层"生成失败："只会淹没重点。
 */
export function getErrorMessage(error: unknown, fallback: string): string {
  if (error instanceof StreamError) {
    return isBudgetExceeded(error) ? error.message : `${fallback}：${error.message}`
  }
  if (error instanceof Error && error.message) return `${fallback}：${error.message}`
  return fallback
}

/** 失败时给用户复制的请求 ID（没有就返回 null，UI 据此不显示复制按钮）。 */
export function getErrorRequestId(error: unknown): string | null {
  return error instanceof StreamError ? error.requestId : null
}

/** 「生成在「机器审查」阶段失败 · 已等待 27s」——失败文案里带上卡住的步骤与耗时。 */
export function formatGenerationFailure(
  stepLabel: string,
  elapsedSeconds: number,
  error: unknown,
): string {
  const reason = getErrorMessage(error, '生成失败')
  return `生成在「${stepLabel}」阶段失败 · 已等待 ${Math.max(0, Math.round(elapsedSeconds))}s · ${reason}`
}
