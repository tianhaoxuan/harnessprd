/**
 * 澄清状态的**启发式**评估：这段对话收口了吗？
 *
 * ## 为什么是启发式，而不是加一轮 LLM 调用
 *
 * 「对话还有没有未回复的问题」本该由服务端状态机回答（`docs/对话阶段设计.md` §8 第 5 项：
 * 阶段由服务端推进、不让模型自判）。现在没有 `SessionStore`，服务端不下发任何东西，
 * 所以只能**就地**从已有消息里推断。而单独加一次"评估完整度"的模型调用是更坏的选择：
 * 用户点一次生成要等两轮模型，且它的结论**同样**不可靠（模型自评）。
 *
 * ## 它只决定"要不要警示 + 问一句"，**不决定能不能生成**
 *
 * | 谁 | 负责什么 |
 * | --- | --- |
 * | 表单阶段 | 字段完整性与格式（必答项、长度、枚举） |
 * | **澄清阶段（这里）** | 对话对齐 —— 结论是**警告 + 确认放行**，不是禁用按钮 |
 * | 生成前兜底 | 只有"没有结构化摘要"才是硬拦截（`handleGeneratePrdWithSync` 里） |
 *
 * 刻意**不做百分制评分**：那会与 AI 实际的提问状态脱节（模型问了 3 个问题、
 * 评分说 80 分"可以生成"，用户只会更困惑）。
 *
 * ## 结构化优先，启发式兜底
 *
 * AI 的输出本来就是结构化 JSON（`{"message", "questions":[{question}], "open_questions", "stage_status"}`），
 * 能解析出来就用它 —— 比正则可靠得多。解析不出来（半截 JSON、模型没按格式输出）才退回
 * 文本启发式。两条路的产出同形，调用方不必区分。
 *
 * ⚠️ 本模块**只有 `import type`**（零运行时依赖）：于是它既能在应用里用，
 * 也能被一个临时 node 脚本直接 import 做离线断言，不必为它装测试框架。
 */

import type { ChatMessage, ClarificationState, ClarificationStatus } from '../types'

/**
 * AI 的**收口话术**。命中且没有待确认问题时，状态即可判 `ready`。
 *
 * ⚠️ 这几句与 `backend/core/prompts/clarify_s*.md` 里要求模型说的收口话对应 ——
 * 改那边的措辞要同步这里，否则顶栏会一直显示"进行中"（不致命，但很难查）。
 */
export const CLARIFY_CLOSING_PHRASES: readonly string[] = [
  '可以生成 PRD',
  '如果没有更多补充',
  '信息已经足够',
  '没有更多问题了',
  '可以开始生成',
]

/** 待确认问题的展示上限：弹窗里超过 5 条就没人读了（多的用"等"收尾）。 */
export const MAX_OPEN_QUESTIONS = 5

/** 列表项的行首标记（`1.` / `1、` / `-` / `*` / `•`）。 */
const LIST_ITEM = /^\s*(?:\d+[.、)]|[-*•])\s+\S/
/** 列表标记本身，剥掉它才是问题正文。 */
const LIST_MARKER = /^\s*(?:\d+[.、)]|[-*•])\s+/
/** 指向"这些还需要确认"的小标题。 */
const PENDING_SECTION = /(需要澄清|待确认|请确认|还需确认|待补充|需要你确认)/
const HEADING = /^#{1,6}\s/

/** AI 输出信封（能解析出来时用的那一份）。 */
interface ClarifyEnvelope {
  message: string
  /** `questions[].question` —— 它**这一轮**在问的 */
  questions: string[]
  /** `open_questions` —— 用户跳过、需要后续决策的 */
  openQuestions: string[]
  stageStatus: 'asking' | 'done' | null
}

/**
 * 去掉强调记号与**包裹**的引号：弹窗是纯文本，星号会原样显示。
 *
 * ⚠️ 只剥**最外层**的一对 —— 实测抓到的反例：模型写的是
 * `我判断「群聊消息怎么拿到」和「不漏项怎么验收」是最大的风险`，
 * 早先的实现把所有开引号都删了，于是内层的 `」` 全部失去配对，
 * 弹窗里显示成 `我判断群聊消息怎么拿到」和不漏项怎么验收」是…`。
 */
function clean(text: string): string {
  return text
    .replace(/\*\*/g, '')
    .replace(/^[「『"']+/, '')
    .replace(/[」』"']+$/, '')
    .trim()
}

function dedupe(items: readonly string[]): string[] {
  const seen = new Set<string>()
  const out: string[] = []
  for (const raw of items) {
    const text = clean(raw)
    if (!text) continue
    // 归一化后去重：模型常把同一个问题换个标点重问一遍
    const key = text.replace(/[\s。，,．.、?？!！]/g, '').toLowerCase()
    if (!key || seen.has(key)) continue
    seen.add(key)
    out.push(text)
  }
  return out
}

/**
 * 解析 AI 输出信封。**刻意不用裸 `JSON.parse` 抛异常**：
 * 流式过程中拿到的可能是半截 JSON，那时应当返回 `null` 让调用方退回启发式。
 */
export function parseClarifyEnvelope(raw: string): ClarifyEnvelope | null {
  const text = raw.trim()
  if (!text.startsWith('{')) return null
  let parsed: unknown
  try {
    parsed = JSON.parse(text)
  } catch {
    return null
  }
  if (typeof parsed !== 'object' || parsed === null) return null
  const record = parsed as Record<string, unknown>

  const questions: string[] = []
  if (Array.isArray(record.questions)) {
    for (const item of record.questions) {
      if (typeof item === 'string') {
        questions.push(item)
      } else if (typeof item === 'object' && item !== null) {
        const question = (item as Record<string, unknown>).question
        if (typeof question === 'string') questions.push(question)
      }
    }
  }
  const openQuestions: string[] = []
  if (Array.isArray(record.open_questions)) {
    for (const item of record.open_questions) {
      if (typeof item === 'string') openQuestions.push(item)
    }
  }
  const stage = record.stage_status
  return {
    message: typeof record.message === 'string' ? record.message : '',
    questions,
    openQuestions,
    stageStatus: stage === 'asking' || stage === 'done' ? stage : null,
  }
}

/**
 * 文本启发式：从 AI 正文里抠出"还需要确认的问题"。
 *
 * 判据（两条取并集）：
 * 1. 列表项且**行末是问号** —— 模型最常这么问；
 * 2. 「需要澄清 / 待确认 / 请确认」小标题**之后**的列表项 —— 那些常写成陈述句
 *    （"时间范围上限：31 天"），但语义上同样是待确认项。
 */
export function extractOpenQuestions(text: string): string[] {
  if (!text.trim()) return []
  const found: string[] = []
  let inPendingSection = false

  for (const raw of text.split('\n')) {
    const line = raw.trim()
    if (!line) continue
    if (HEADING.test(line)) {
      inPendingSection = PENDING_SECTION.test(line)
      continue
    }
    if (!LIST_ITEM.test(raw)) {
      // 小标题之外，遇到一整段正文就认为"待确认清单"结束了
      if (PENDING_SECTION.test(line)) {
        inPendingSection = true
        continue
      }
      inPendingSection = false
      continue
    }
    const body = line.replace(LIST_MARKER, '').trim()
    if (!body) continue
    if (/[?？]\s*$/.test(body) || inPendingSection) found.push(body)
  }
  return dedupe(found).slice(0, MAX_OPEN_QUESTIONS)
}

/** 最后一条**已完成**的 AI 消息（流式中的那条不算：它还是半截 JSON）。 */
function lastCompleteAi(messages: readonly ChatMessage[]): ChatMessage | undefined {
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const message = messages[index]
    if (message.role === 'ai' && !message.streaming) return message
  }
  return undefined
}

/**
 * 评估澄清状态。随 `messages` 在 `useMemo` 里重算（纯字符串处理，实测 < 1ms）。
 */
export function assessClarificationState(messages: readonly ChatMessage[]): ClarificationState {
  const warnings: string[] = []
  const last = messages.length > 0 ? messages[messages.length - 1] : undefined
  const userSpoke = messages.some((message) => message.role === 'user')
  const ai = lastCompleteAi(messages)

  if (messages.length === 0) {
    warnings.push('尚未开始澄清对话 —— 生成的 PRD 只会依据表单作答')
  } else if (!userSpoke) {
    warnings.push('尚未在对话中补充说明')
  }

  const envelope = ai ? parseClarifyEnvelope(ai.content) : null
  // `display` 是后端 JSON 里抠出来的正文；没有就用信封的 message，再没有就退回原文
  const body = ai ? ai.display || envelope?.message || ai.content : ''
  /**
   * ⚠️ 「末条是 AI」必须理解成「末条是那条**已完成**的 AI」。
   *
   * 实测踩到：AI 正在流式回复时，`last` 是那条 `streaming` 消息，而 `ai` 还是**上一轮**
   * 那条完整的 AI —— 如果只看 `last.role === 'ai'`，就会把上一轮（用户刚回答过的）
   * 问题当成"还在待回复"，于是用户在等回复的那几秒里点生成又被拦一次。
   */
  const lastIsAi = last !== undefined && last === ai
  const lastIsUser = last?.role === 'user'

  /**
   * **这一轮**在问的问题：只有"末条就是那条 AI 消息"时才算待回复。
   *
   * ⚠️ 这一条是实测逼出来的（离线断言里"末条是 user"那两条原本 FAIL）：
   * 用户回复之后，上一轮 AI 的问题**算已经被回应**，否则对话中途点生成会**永远**弹确认
   * —— 而中途点生成是很正常的动作（"我知道还差几个，先出个稿看看"）。
   * 验收口径也是这么写的：「用户回复后（末条 user），无新 AI 提问时不强制 confirm」。
   *
   * 信封能解析时以它为准（`questions[]` 是模型明确列出的问题），
   * 解析不出来才退回文本启发式 —— 两条都走"末条必须是 AI"这条门。
   */
  const roundQuestions = lastIsAi
    ? envelope
      ? dedupe(envelope.questions)
      : extractOpenQuestions(body)
    : []

  /**
   * 用户**跳过、需要后续决策**的项（`open_questions`）：不会因为一次回复就消失
   * —— 它们本来就是"这个先不答，回头再定"的东西。
   */
  const skippedQuestions = envelope ? dedupe(envelope.openQuestions) : []
  const openQuestions = dedupe([...roundQuestions, ...skippedQuestions]).slice(
    0,
    MAX_OPEN_QUESTIONS,
  )

  const stageStatus = envelope?.stageStatus ?? null
  const asking = openQuestions.length > 0
  const closed = stageStatus === 'done' || CLARIFY_CLOSING_PHRASES.some((p) => body.includes(p))

  let status: ClarificationStatus
  if (!asking && closed) {
    status = 'ready'
  } else if (!asking && lastIsUser) {
    // 用户刚回复、AI 还没开口（或在路上）：不逼他确认
    status = 'ready'
  } else if (lastIsAi && asking) {
    status = 'awaiting_user_reply'
  } else {
    status = 'collecting'
  }

  const statusLabel =
    status === 'ready'
      ? '澄清已收口 · 可生成 PRD'
      : asking
        ? `澄清进行中 · ${openQuestions.length} 个待确认`
        : '澄清进行中'

  return {
    status,
    openQuestions,
    warnings,
    statusLabel,
    /**
     * 确认的触发条件：**轮到用户回答时**（末条是那条完整的 AI 且有待确认），或有 warnings。
     *
     * ⚠️ 这里与工单 §5 的字面规则有一处**刻意的差异**，记下来免得被当成漏实现：
     * §5 还写了「`openQuestions.length > 0` 且 status 为 `collecting`」也要确认。
     * 但实测那一格就是"用户刚回复、AI 还在答"的窗口 —— 用户的回答已经把上一轮的提问
     * 消解了，此时还要弹确认，直接与验收第 4 条（「用户回复后（末条 user），无新 AI 提问时
     * **不强制** confirm」）冲突。取舍是**以验收为准**：那一格不确认。
     *
     * 代价说清楚：**用户跳过的项**（`open_questions`）在"AI 正在回 / 用户刚答完"这段窗口里
     * 不会单独触发确认 —— 但它仍然出现在 `openQuestions` 与顶栏计数里（用户看得见），
     * 而且等 AI 说完话（末条变回 AI）就会照常触发。
     */
    needsConfirmBeforeGenerate: (lastIsAi && asking) || warnings.length > 0,
  }
}
