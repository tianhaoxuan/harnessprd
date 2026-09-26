import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { AlertCircle, ArrowLeft, Download, Eraser, FileText, Loader2, RefreshCw, Sparkles } from 'lucide-react'

import ChatInput from './components/ChatInput'
import DocumentReview, {
  countContentChars,
  replaceSection,
  type DocumentAction,
  type OptimizeRequest,
} from './components/DocumentReview'
// ⚠️ 这里只借 `FormStep` 的类型与校验函数：**第一步的界面已经换成 `StructuredForm`**
// （结构化录入）。`FormStep`（20 题逐题渲染）仍保留在仓库里，想切回去把下面这行换成
// 默认导入、并把 `<StructuredForm .../>` 换回 `<FormStep .../>` 即可。
import { type FormValues, validateForm } from './components/FormStep'
import StructuredForm, { type StructuredSubmit } from './components/StructuredForm'
import RagHitsPanel from './components/RagHitsPanel'
import SummaryDiffPanel, {
  diffSummaries,
  type SummaryChange,
} from './components/SummaryDiffPanel'
import MessageList from './components/MessageList'
import StepProgress from './components/StepProgress'
import {
  continueConversationStream,
  describeApiError,
  extractStreamingMessage,
  generateApiDocsStream,
  generatePrdStream,
  generatePromptsStream,
  getDocumentPlan,
  getQuestions,
  optimizeDocumentStream,
  readDialogueStageStatus,
  startConversationStream,
  retrieveApiDocsRag,
  syncSummaryFromConversation,
  type RagHit,
  type StreamChunkMeta,
} from './services/api'
import {
  clearFormDraft,
  clearLocal,
  readFormDraft,
  readLocal,
  SESSION_KIND,
  writeFormDraft,
  writeLocal,
} from './services/storage'
import { downloadFile, safeFileName } from './services/download'
import {
  STEPS,
  type ChatMessage,
  type ConversationTurn,
  type DocStatus,
  type QuestionsConfig,
  type ViewState,
} from './types'

type LoadState =
  | { status: 'loading' }
  | { status: 'ok'; config: QuestionsConfig }
  | { status: 'error'; message: string }

// ---------------------------------------------------------------- 三份产物

/** 产物类型。与后端 `services/state.py` 的 `DocKind` 一致。 */
type DocKind = 'prd' | 'api' | 'prompts'

/**
 * 三份产物的元信息。
 *
 * 把「哪种产物 ↔ 哪两个视图 ↔ 哪个标题」收在一张表里，是因为 App 里到处都要按 kind
 * 分支（生成、优化、状态推导、恢复）。散成七八处 `if (kind === 'prd')` 之后，
 * 加第四个产物就会漏掉其中一两处。
 */
const DOC_META: Record<
  DocKind,
  {
    title: string
    /** 生成中的视图（进度条与"正在生成…"用） */
    generating: ViewState
    /** 产出后的审核视图 */
    review: ViewState
    /** 生成按钮文案 */
    generateLabel: string
    /** 它依赖哪些上游（用于按钮禁用提示与错误文案） */
    requiresPrd: boolean
    /**
     * 下载时的文件名主干（不含扩展名）。实际文件名是 `{产品名}-{主干}.md`。
     *
     * 刻意**不用 `title`**：那个带全角括号与"（产品需求文档）"这种说明，拼出来是
     * `群聊周报助手-PRD（产品需求文档）.md`，长且啰嗦。
     */
    fileStem: string
  }
> = {
  prd: {
    title: 'PRD（产品需求文档）',
    generating: 'generating-prd',
    review: 'review-prd',
    generateLabel: '生成 PRD',
    requiresPrd: false,
    fileStem: 'PRD',
  },
  api: {
    title: '接口文档',
    generating: 'generating-api-docs',
    review: 'review-api-docs',
    generateLabel: '生成接口文档',
    requiresPrd: true,
    fileStem: '接口文档',
  },
  prompts: {
    title: '提示词套件',
    generating: 'generating-prompts',
    review: 'review-prompts',
    generateLabel: '生成提示词套件',
    requiresPrd: true,
    fileStem: '提示词套件',
  },
}

/** 链条顺序：PRD 是唯一源头，接口文档从 PRD 推导，套件消费前两者（`HANDOFF.md` §1）。 */
const DOC_ORDER: DocKind[] = ['prd', 'api', 'prompts']

/**
 * **每个审核阶段的操作配置。**
 *
 * 把「通过之后去哪」「依赖哪一份上游」「通过按钮叫什么」集中成一张表，
 * 而不是散在 `handleApproveDocument` / `docActionsFor` 的 if 链里 ——
 * 加第四个产物时，散着写就会漏掉其中一两处，而漏掉的表现是**流程断在某一步**
 * （按钮没了、或者通过了却不动），排查起来要翻好几处。
 *
 * | 字段 | 作用 |
 * | --- | --- |
 * | `approveLabel` | 「通过」按钮的文案。写明**通过之后会发生什么**，而不是干巴巴一个"通过" |
 * | `next` | 通过之后进入哪个阶段的审核页；`null` = 通过即完成（走 `handleEndTask`） |
 * | `upstream` | 必需的**已通过**的上游产物；`null` = 链条源头。缺它就拒绝通过 |
 */
const STAGE_ACTIONS: Record<
  DocKind,
  { approveLabel: string; next: DocKind | null; upstream: DocKind | null }
> = {
  prd: { approveLabel: '通过，去生成接口文档', next: 'api', upstream: null },
  api: { approveLabel: '通过，去生成提示词套件', next: 'prompts', upstream: 'prd' },
  // 最后一份：通过即完成 —— `next: null` 就是"这一步之后没有下一步"的唯一声明处
  prompts: { approveLabel: '通过并完成任务', next: null, upstream: 'prd' },
}

/**
 * 落本地的一条消息。
 *
 * ⚠️ **刻意不含 UI 字段**（`ChatMessage` 的 `streaming` / `error`）：
 * - `streaming` 存下来会让刷新后的消息永远显示"正在输入"
 * - `error` 的那条正文可能是残缺的，当成完整一轮存下来就是污染历史
 */
export interface StoredMessage {
  id: string
  role: 'user' | 'ai'
  content: string
}

/**
 * 落本地的一份产物。
 *
 * ⚠️ **只存正文与「是否已通过」，不存"正在生成"**：存 `generating` 会让刷新后的文档
 * 永远显示"生成中"。失败态同理不存 —— 本地记"上次失败了"没有意义，重来一次就行。
 * （视图状态里那个 `generating-*` 是另一回事，见 `SessionData.viewState`。）
 */
export interface StoredDocument {
  content: string
  /** 仅 `approved` 会被写进来；其余状态一律按"未通过"还原 */
  approved: boolean
  updatedAt: string
  /**
   * 这份正文**被模型单次输出上限截断**（末尾是缺的）。
   *
   * ⚠️ **必须存进来**：截断在正文里看不出来（末尾就是半行表格），刷新一次
   * 就再没人知道它是残的 —— 而用户在不知情的情况下会拿它当完整文档去用。
   * 这是少数几个"内容之外也必须持久化"的状态之一。
   */
  truncated?: boolean
}

/**
 * 一份完整的本地会话快照。
 *
 * 这是**产品决定**的形状（哪些字段值得留住），所以定义在 `App.tsx` 而不是 `storage.ts`；
 * 存储层只提供 key 约定、信封、过期与泛型读写。
 *
 * 会话接口就绪后，这个类型会被 `GET /sessions/{id}` 的 `SessionSnapshot` 取代
 * （`api/schemas.py` 里已有那份契约）—— `sessionId` 就是那个 `{id}`。
 */
export interface SessionData {
  /** 本地生成的会话 id。将来就是服务端 `GET /sessions/{id}` 的那个 id */
  sessionId: string
  /** 会话按**创建时**的表单版本解释作答（`状态数据设计` §6.2），所以必须一起存 */
  formVersion: string
  /** 本会话绑定的表单快照 —— `continue-stream` 每轮都要重发 `form` */
  form: Record<string, string>
  messages: StoredMessage[]
  /** 已发生的轮次，用来算 `round_index` */
  roundIndex: number
  documents: Partial<Record<DocKind, StoredDocument>>
  /**
   * 上次离开时在哪一屏。
   *
   * ⚠️ **这会存进去，但绝不原样恢复。** `会话持久化方案` §7.3 警告的是"本地记我发起过生成
   * → 刷新后错误地显示生成中"。`loadSession()` 的对策是：**所有 `generating-*` 一律降级**
   * 成对应的 `review-*`（§7.4 的孤儿生成态那条），并把这个事实报给调用方去提示用户。
   * 所以"存视图状态"与"不能显示假的生成中"这两件事是不冲突的 —— **前提是降级一定要做**。
   */
  viewState: ViewState
  updatedAt: string
}

/** `loadSession()` 的结果。 */
export interface LoadedSession {
  /** 已做**降级**的会话数据（`viewState` 保证不是 `generating-*`） */
  data: SessionData
  /**
   * 上次离开时**正在生成**的那一份产物；`null` = 没有被打断。
   *
   * 单独返回而不是塞进 `data`：它是**读的时候才知道**的信息，不该被写回存储。
   * 调用方据此提示"上次生成被刷新打断了" —— 只降级不提示的话，
   * 用户会把上一版内容当成刚生成出来的。
   */
  interrupted: DocKind | null
}

/** 保存整份会话。存储不可用时静默失败（`writeLocal` 的行为）—— 它只是缓存，不是权威。 */
export function saveSession(data: SessionData): void {
  writeLocal(SESSION_KIND, data)
}

/** 清掉会话。 */
export function clearSession(): void {
  clearLocal(SESSION_KIND)
}

/** 所有合法的视图状态。**从 `DOC_META` 推导**，加第四个产物时不用回来补。 */
const KNOWN_VIEW_STATES: ReadonlySet<string> = new Set<string>([
  'form',
  'chatting',
  'done',
  ...DOC_ORDER.flatMap((kind) => [DOC_META[kind].generating, DOC_META[kind].review]),
])

/** `generating-*` → 它对应的产物与审核视图。`loadSession` 的降级表。 */
const IN_FLIGHT_VIEWS: Partial<Record<ViewState, { kind: DocKind; review: ViewState }>> =
  Object.fromEntries(
    DOC_ORDER.map((kind) => [
      DOC_META[kind].generating,
      { kind, review: DOC_META[kind].review },
    ]),
  ) as Partial<Record<ViewState, { kind: DocKind; review: ViewState }>>

/**
 * 老记录没有 `viewState` 时的回落：链条上第一份「有内容但还没通过」的产物的审核页。
 * 全都没内容就回对话页。这条规则与降级无关，纯粹是兼容旧数据。
 */
function fallbackView(documents: SessionData['documents']): ViewState {
  const pending = DOC_ORDER.find((kind) => documents[kind] && !documents[kind]?.approved)
  if (pending) return DOC_META[pending].review
  return DOC_ORDER.some((kind) => documents[kind]) ? 'done' : 'chatting'
}

/**
 * 读回本地会话。**读不出或形状不对就返回 `null`**（当没有），
 * 并且**在这里把生成中的视图降级**（§7.4 的孤儿生成态）。
 *
 * 逐字段校验而不是直接 `as SessionData`：本地数据可能是旧版本前端写的
 * （甚至在开发期被手工改过），形状不对时宁可当作没有，也不要把脏数据喂给 UI。
 */
export function loadSession(): LoadedSession | null {
  const raw = readLocal<unknown>(SESSION_KIND)
  if (typeof raw !== 'object' || raw === null) return null
  const payload = raw as Record<string, unknown>

  const sessionId = typeof payload.sessionId === 'string' ? payload.sessionId : ''
  // 没有 id 就不是一份能用的会话 —— 会话是**提交表单之后**才建立的，所以两边都要有
  if (!sessionId) return null

  const form: Record<string, string> = {}
  if (typeof payload.form === 'object' && payload.form !== null) {
    for (const [key, value] of Object.entries(payload.form as Record<string, unknown>)) {
      if (typeof value === 'string') form[key] = value
    }
  }
  if (Object.keys(form).length === 0) return null

  const messages: StoredMessage[] = []
  if (Array.isArray(payload.messages)) {
    for (const item of payload.messages) {
      if (typeof item !== 'object' || item === null) continue
      const message = item as Record<string, unknown>
      if (
        typeof message.id === 'string' &&
        (message.role === 'user' || message.role === 'ai') &&
        typeof message.content === 'string'
      ) {
        messages.push({ id: message.id, role: message.role, content: message.content })
      }
    }
  }

  const documents: SessionData['documents'] = {}
  if (typeof payload.documents === 'object' && payload.documents !== null) {
    for (const kind of DOC_ORDER) {
      const item = (payload.documents as Record<string, unknown>)[kind]
      if (typeof item !== 'object' || item === null) continue
      const doc = item as Record<string, unknown>
      // 与保存侧同一个判据：**只有空白**的产物当成没有（否则会还原出一个"待审核的空壳"）
      if (typeof doc.content !== 'string' || doc.content.trim().length === 0) continue
      documents[kind] = {
        content: doc.content,
        approved: doc.approved === true,
        updatedAt: typeof doc.updatedAt === 'string' ? doc.updatedAt : '',
        // 老记录没有这个键 → `undefined` = 没截断（"不知道"一律当没问题，不猜）
        truncated: doc.truncated === true,
      }
    }
  }

  // ---------- 视图状态：校验 + **降级** ----------
  const storedView =
    typeof payload.viewState === 'string' && KNOWN_VIEW_STATES.has(payload.viewState)
      ? (payload.viewState as ViewState)
      : null
  const inFlight = storedView ? IN_FLIGHT_VIEWS[storedView] : undefined

  return {
    data: {
      sessionId,
      formVersion: typeof payload.formVersion === 'string' ? payload.formVersion : '',
      form,
      messages,
      roundIndex:
        typeof payload.roundIndex === 'number' && payload.roundIndex >= 1 ? payload.roundIndex : 1,
      documents,
      // 生成中 → 对应的审核页；其余原样；识别不出来的走兼容回落
      viewState: inFlight ? inFlight.review : (storedView ?? fallbackView(documents)),
      updatedAt: typeof payload.updatedAt === 'string' ? payload.updatedAt : '',
    },
    interrupted: inFlight?.kind ?? null,
  }
}

/** 草稿写回本地的防抖间隔。对齐 `会话持久化方案` §5.1 给服务端同步定的 800ms 节奏。 */
const DRAFT_DEBOUNCE_MS = 800

/**
 * 会话落本地的防抖间隔。
 *
 * ⚠️ **必须有防抖**：产物正文是**逐键**写进 `prdContent` 的（`DocumentReview` 的 textarea
 * 每敲一个字就 `onContentChange`），不防抖就是每个按键一次 localStorage 同步写 ——
 * 而文档 §2 明确警告它是同步 API、会阻塞主线程。
 * 会话只在"完成一轮 / 切屏 / 停止输入"这类节点落盘，丢 800ms 内的那次输入是可以接受的
 * （服务端才是权威，本地只是缓存）。
 */
const SESSION_DEBOUNCE_MS = 800

/**
 * 会话落本地的**最长推迟**（配合上面的防抖使用）。
 *
 * ⚠️ **光有防抖会被"饿死"**：每一处 state 变化都会清掉上一个定时器、重排一次，
 * 所以只要变化间隔一直小于 800ms，就**一次都不会落盘**。
 * 实测踩到：探针以 250ms 一步连点走完整条链条，盘上始终只有草稿、没有会话 ——
 * 真实场景里"连续打字 30 秒然后关标签页"是同一回事（每敲一个字都会重排定时器）。
 *
 * 所以加一道上限：从**第一次待写**算起最多推迟这么久，到点就强制落一次。
 * 结果是最坏情况下最多丢 2 秒的输入，而不会"一直没存"。
 */
const SESSION_MAX_WAIT_MS = 2000

/**
 * 把用户确认过的结构化摘要渲染成 `known_info` 的一段。
 *
 * 为什么整份 JSON 都给出去：生成 PRD 时提示词吃的是 `known_info`（自由文本，优先级高于表单），
 * 而回填可能改了任意字段。逐字段挑着给会让"下次加字段忘了同步"重演；
 * 整份给出去，口径与摘要本身永远一致。
 *
 * 不覆盖 `structuredExtras`（页面结构 / 关键交互 / 在不在范围内 / LLM 说明 / 可用性）——
 * 那几项不在 8 字段 schema 里，由表单那条通道单独送，两者在 `known_info` 里并列。
 */
function buildExtrasFromSummary(summary: Record<string, unknown>): string {
  return [
    '结构化摘要（对话澄清后已由用户逐条确认，优先级高于表单作答）：',
    JSON.stringify(summary, null, 2),
  ].join('\n')
}

/**
 * 把对话里**用户亲口说过的话**拼成 `known_info` 的过渡替代品。
 *
 * ⚠️ **这不是设计里的 `known_info`。** 设计要求它是「对话中确认的**结构化**信息：
 * 动线、角色与权限、边界情况、接口约定、验收口径」，由 `apply_event` 在阶段推进时
 * 合并进 `dialogue.known_info`（`状态数据设计` §6.4）。那个还没实现。
 *
 * 为什么不干脆把整段对话塞进去：AI 的回复是 JSON，里面 `questions[].suggested_answer`
 * 是**它自己的建议**，不是用户的确认。把它们当事实喂给生成阶段，就是 `gen_common.md`
 * 里点名禁止的「拿邻近内容顶替」—— 而错位的内容看起来像真的（坑 #3）。
 * 所以这里**只取用户的原话**，并明确标注它是什么。
 *
 * ⚠️ 代价要清楚：缺结构化的 `known_info` 会让生成质量下降 —— 实测缺 S3 优先级会让
 * 第 7 章所有功能都标 P0、缺 S4 默认值会让合规小节被权限内容顶替（坑 #4）。
 * 这是**数据流缺口**，提示词救不了。会话层落地后这个函数应当整体删掉。
 *
 * @returns 有内容时返回可直接当 `known_info` 用的文本；**一个字都没有时返回空串**，
 *          调用方应当**不要传这个字段**（让后端填「（尚无）」），而不是传空串进去
 */
function buildKnownInfo(messages: ChatMessage[]): string {
  const said = messages
    .filter((message) => message.role === 'user')
    .map((message) => message.content.trim())
    .filter(Boolean)
  if (said.length === 0) return ''

  return [
    '【对话中用户提供的补充信息】',
    '（说明：结构化 known_info 的合并属于会话层的 apply_event，尚未实现；',
    '这里只列出用户在对话里亲口说过的内容，不含 AI 的推测。）',
    ...said.map((text) => `- 用户：${text}`),
  ].join('\n')
}

/**
 * 正在流式接收的那条 AI 消息用的固定 id。
 *
 * 用固定值而不是自增 id：它同时只可能存在一条，固定 id 能让 React 在收到新 chunk 时
 * **复用同一个 DOM 节点**（否则每来一段就换一次 key，整段重挂载，中文输入/选区都会抖）。
 */
const STREAMING_MESSAGE_ID = '__streaming__'

/**
 * 轮次上限，**镜像后端** `services.conversation_service.DEFAULT_MAX_ROUNDS`。
 *
 * ⚠️ 这是**权宜之计**。按 `docs/对话阶段设计.md` §8 第 5 项，阶段与轮次推进**应当由服务端
 * 状态机负责**，不该由前端算；而 `阶段` 现在还是调用方传进来的参数（`HANDOFF.md` §5）。
 * 服务端接上后这里要连同 `roundIndex` 一起删掉。
 */
const MAX_ROUNDS = 4

/**
 * 对话回复被输出上限截断时给用户看的话。
 *
 * 为什么要单独一句、而且按**失败**处理：对话推的是结构化 JSON，被截断后
 * **解析不出下一轮要问什么**（`readDialogueStageStatus()` 会返回 `null`）。
 * 不提示的话，用户看到的只是"AI 回了一句没头没尾的话、追问也不动"，
 * 完全联想不到是输出长度的问题（`StreamChunkMeta.truncated` 的注释里有完整说明）。
 */
const CHAT_TRUNCATED_NOTICE =
  '模型这一轮的回复撞上了单次输出上限，内容是断的（解析不出下一轮要问什么）。可以直接重试；若反复出现，把表单里最长的两项写短一些。'

// `stitchParts` 已挪到 `services/stitch.ts`：AppV2 的分步流程也要用它，
// 而让 V2 反向 import 本文件会把整个旧界面拖进打包结果。
import { stitchParts } from './services/stitch'

/** 本地会话 id。服务端 `/sessions` 还是 501，所以先自己生成 —— 它同时是本地存储的分片键。 */
function newConversationId(): string {
  const cryptoApi = globalThis.crypto
  if (cryptoApi && typeof cryptoApi.randomUUID === 'function') return cryptoApi.randomUUID()
  return `local-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`
}

/** 生成消息 id。会话内唯一即可，不要求和后端一致（后端现在是 `_serialize_messages` 现场造的）。 */
let messageSeq = 0
function newMessageId(role: ChatMessage['role']): string {
  messageSeq += 1
  return `${role}-${Date.now().toString(36)}-${messageSeq}`
}

/**
 * 从**原始模型输出**里派生出给人看的那一份。
 *
 * ⚠️ 后端推的不是散文，是 `对话阶段设计` §6.2 的结构化 JSON：
 *
 *     {"message": "…", "questions": [{id, question, suggested_answer, reason}],
 *      "conflicts": [], "stage_status": "asking", "open_questions": []}
 *
 * 直接把它丢进气泡里，用户看到的就是一坨带 `\n` 字面量的 JSON —— 实测确认过。
 * 所以显示走 `display`（从 JSON 里抠出的 `message`），而 `content` 保留原文：
 * **回传给后端的 `history` 必须是原文**，抠掉 `questions` 就等于让模型忘了自己问过什么。
 *
 * 抠不出来时（比如以后提示词改成纯文本输出）回落到原文，不会变成空白气泡。
 */
function deriveDisplay(
  raw: string,
  role: ChatMessage['role'],
  streaming = false,
): string | undefined {
  if (role !== 'ai') return undefined

  const extracted = extractStreamingMessage(raw)
  if (extracted !== null) return extracted

  // 还没抠出 `message`。
  if (raw.trimStart().startsWith('{')) {
    // 文本看起来是 JSON，但 `"message"` 这个键还没出现（流式开头只到了 `{`、`{"mes`…）。
    // 这时候**回落到原文就会闪出一截原始 JSON 残片**（实测确实会闪一个 `{`），
    // 所以流式期间先显示空 —— 气泡里只剩光标，正是想要的"正在思考"观感。
    if (streaming) return ''
    // 已经收完了却仍没有 `message` = 后端违反了 §6.2 契约。
    // 这种情况**把原文摆出来**，宁可难看也要让人看见，别静默变成空气泡。
    return raw
  }

  // 根本不是 JSON（提示词改成纯文本输出了）→ 原文就是正文
  return raw
}

function makeAiMessage(raw: string, error?: string): ChatMessage {
  return {
    id: newMessageId('ai'),
    role: 'ai',
    content: raw,
    display: deriveDisplay(raw, 'ai'),
    ...(error ? { error } : {}),
  }
}

/** 本地记录 → UI 消息。`display` 是派生的，所以**不落盘**，读回来时重新算。 */
function storedToChat(message: StoredMessage): ChatMessage {
  return {
    id: message.id,
    role: message.role,
    content: message.content,
    display: deriveDisplay(message.content, message.role),
  }
}

/**
 * `done` 这一屏还没有内容可放 —— 三份产物都通过之后就到此为止。
 *
 * 其余非表单/对话的视图（`generating-*` / `review-*`）**已经接上真实实现**了，
 * 所以它们不再走占位文案这条路。
 */
const DONE_NOTE = {
  title: '全部完成',
  detail: '三份产物都已通过审核。',
  source: 'docs/状态机设计.md',
} as const

export default function App() {
  /**
   * 当前渲染哪一屏。
   *
   * ⚠️ **不要把它写进 localStorage。** `会话持久化方案` §7.3 明确警告：
   * 本地记"我发起过生成"只会在服务端已经完成时让前端错误地显示"生成中"。
   * 刷新后的恢复路径是「读会话指针 → `GET /sessions/{id}` → 按 `snapshot.state` 渲染」，
   * 不是读本地视图状态。会话接口就绪后这里要改成由快照推导。
   */
  const [viewState, setViewState] = useState<ViewState>('form')

  const [load, setLoad] = useState<LoadState>({ status: 'loading' })
  const [values, setValues] = useState<FormValues>({})
  const [submitted, setSubmitted] = useState<FormValues | null>(null)
  /**
   * 结构化表单里"20 题装不下"的那部分内容（页面结构、关键交互、范围、LLM 说明、可用性）。
   *
   * 它在**生成**时并进 `known_info`；对话阶段（`start-stream`）拿不到它 ——
   * 那个接口只收 `form`，没有 `known_info` 字段。缺的细节正好由澄清阶段追问补齐。
   */
  const [structuredExtras, setStructuredExtras] = useState('')
  /**
   * 结构化摘要本体（表单交出来、对话回填可更新）。
   *
   * 它是**回填的基准**：没走结构化录入时为 `null`，此时回填整步跳过。
   */
  const [structuredSummary, setStructuredSummary] = useState<Record<string, unknown> | null>(null)
  /** 用户确认后的摘要文本，单独一格并进 `known_info`（不覆盖 `structuredExtras`）。 */
  const [summaryExtras, setSummaryExtras] = useState('')
  /** 待用户确认的回填差异。`null` = 没有待确认项。 */
  const [pendingSummary, setPendingSummary] = useState<{ changes: SummaryChange[] } | null>(null)
  const [summarySyncBusy, setSummarySyncBusy] = useState(false)
  const [summarySyncError, setSummarySyncError] = useState<string | null>(null)
  /** 待确认的 RAG 检索结果（`null` = 没有待确认项）。 */
  const [pendingRag, setPendingRag] = useState<{ hits: RagHit[]; corpusSize: number } | null>(null)
  const [ragBusy, setRagBusy] = useState(false)
  /** 用户确认过的检索片段，拼进 `known_info` 一起发给生成接口。 */
  const [ragExtras, setRagExtras] = useState('')
  /** 防死循环：确认那一次要跳过检索闸门。 */
  const ragConfirmedRef = useRef(false)
  const [restoredDraft, setRestoredDraft] = useState(false)
  /** 草稿恢复完成前**禁止**写回，否则初始的空对象会把已存的草稿冲掉。 */
  const [hydrated, setHydrated] = useState(false)

  // ---------------------------------------------------------------- 对话状态

  /**
   * 本轮正在接收的 AI 正文。
   *
   * **刻意与 `messages` 分开**：流式一轮会推几百个 chunk，若每个 chunk 都往 `messages` 里
   * 塞一次，就会连带触发「落本地」的 effect 和整列表重渲染。分开之后流式期间 `messages`
   * 不变，只有这条字符串在动。
   */
  const [streamingContent, setStreamingContent] = useState('')
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [conversationId, setConversationId] = useState<string | null>(null)
  /** 会话绑定的表单版本（`状态数据设计` §6.2：按创建时的版本解释作答）。 */
  const [formVersion, setFormVersion] = useState('')
  const [roundIndex, setRoundIndex] = useState(1)
  /** 有请求在飞。用来禁用输入框，防止并发发两轮。 */
  const [sending, setSending] = useState(false)
  const [chatError, setChatError] = useState<string | null>(null)
  /** 本地对话恢复完成前不要写回，否则空状态会把已存的对话冲掉。 */
  const [chatHydrated, setChatHydrated] = useState(false)
  /**
   * 页面顶部的提示条（**一个**机制，两种来源）：
   * - 恢复了上次会话 / 上次生成被打断（初始化时设）
   * - 流程守卫拒绝了一次操作（比如"还没通过 PRD 就想生成接口文档"）
   *
   * 合成一个而不是各搞各的：两套 banner 会在同一次操作里同时出现，
   * 而用户只会看最上面那条。放在页面顶部而不是对话页内 —— 现在恢复的可能是任意一屏。
   */
  const [notice, setNotice] = useState<{ text: string; warn: boolean } | null>(null)

  /** 当前流的取消句柄。组件卸载、重开对话、清空时要 abort，别让连接挂着。 */
  const abortRef = useRef<AbortController | null>(null)
  /**
   * 已收到的部分正文。
   *
   * ⚠️ 必须用 ref：`readStream` 抛错时**不会**把已积累的全文带出来，
   * 而失败时我们又想把已经收到的内容留给用户看，只能自己记一份。
   */
  const partialRef = useRef('')

  // ---------------------------------------------------------------- 产物状态

  // 三份产物**分开存**（而不是一个 `Record<DocKind, string>`）：它们是三条独立的链条环节，
  // 各自有内容 / 已通过 两个维度，分开之后读写点一眼能看清在动哪一份。
  const [prdContent, setPrdContent] = useState('')
  const [apiDocsContent, setApiDocsContent] = useState('')
  const [promptsContent, setPromptsContent] = useState('')
  /** 有产物请求在飞（生成或优化）。用来禁用按钮、防止并发。 */
  const [isGenerating, setIsGenerating] = useState(false)
  /** 在飞的是哪一份。单靠 `isGenerating` 分不清"刚点的是 PRD 还是接口文档"。 */
  const [generatingKind, setGeneratingKind] = useState<DocKind | null>(null)
  /** 每一份是否已通过审核。通过动作现在是**本地推进**，见 `handleApproveDocument`。 */
  const [approvedDocs, setApprovedDocs] = useState<Record<DocKind, boolean>>({
    prd: false,
    api: false,
    prompts: false,
  })
  /**
   * 每一份的正文是否**被输出上限截断**（末尾是缺的）。
   *
   * 与 `docFailure` 分开：截断的文档**是有用的**（前面大半是好的），不该按失败处理；
   * 但必须让用户知道末尾缺了 —— 否则他会把半行表格当成文档本来就短。
   */
  const [truncatedDocs, setTruncatedDocs] = useState<Record<DocKind, boolean>>({
    prd: false,
    api: false,
    prompts: false,
  })
  /** 最近一次产物失败。记录是**哪一份**失败的 —— 否则切到别的文档页会看到不相干的报错。 */
  const [docFailure, setDocFailure] = useState<{ kind: DocKind; message: string } | null>(null)
  /** 正在流式接收的产物正文（与 `content` 分开，理由同对话侧的 `streamingContent`）。 */
  const [streamingDoc, setStreamingDoc] = useState('')
  /**
   * 分片生成的进度（`null` = 没在分片生成）。
   *
   * 整份接口文档要分 3 片、每片几十秒 —— 不显示"第 2/3 片"的话，
   * 用户看到的就是一个长时间不动的"生成中"，会以为卡死了。
   */
  const [docProgress, setDocProgress] = useState<{
    kind: DocKind
    part: number
    total: number
    label: string
  } | null>(null)
  /** 单独一个取消句柄：产物流与对话流互不干扰（清空时要两个都 abort）。 */
  const docAbortRef = useRef<AbortController | null>(null)
  /**
   * 会话"第一次待写"的时刻（`null` = 当前没有待写）。
   *
   * 配合 `SESSION_MAX_WAIT_MS` 防止防抖被连续变化饿死；落盘后重置为 `null`。
   */
  const sessionPendingSince = useRef<number | null>(null)
  /** 同上：产物流失败时也要能拿到已收到的部分。 */
  const docPartialRef = useRef('')

  const readDoc = useCallback(
    (kind: DocKind): string =>
      kind === 'prd' ? prdContent : kind === 'api' ? apiDocsContent : promptsContent,
    [prdContent, apiDocsContent, promptsContent],
  )

  const writeDoc = useCallback((kind: DocKind, content: string) => {
    if (kind === 'prd') setPrdContent(content)
    else if (kind === 'api') setApiDocsContent(content)
    else setPromptsContent(content)
  }, [])

  /**
   * 推导某一份产物当前的状态。
   *
   * **刻意是派生而不是另一份 state**：`status` 与"有没有内容""在不在生成""通没通过"
   * 是同一件事的不同说法，各存一份必然漂移（改了内容忘了改 status）。
   *
   * ⚠️ `failed` 排在 `approved` 之前：一份已通过的文档被重新生成且失败了，
   * 界面上该显示"这次失败了"，而不是继续挂个"已通过"让人以为没问题。
   */
  const statusFor = useCallback(
    (kind: DocKind): DocStatus => {
      if (isGenerating && generatingKind === kind) return 'generating'
      if (docFailure?.kind === kind) return 'failed'
      if (approvedDocs[kind]) return 'approved'
      return readDoc(kind) ? 'pending_review' : 'not_started'
    },
    [isGenerating, generatingKind, docFailure, approvedDocs, readDoc],
  )

  const loadQuestions = useCallback(async () => {
    setLoad({ status: 'loading' })
    try {
      setLoad({ status: 'ok', config: await getQuestions() })
    } catch (error) {
      setLoad({ status: 'error', message: describeApiError(error) })
    }
  }, [])

  useEffect(() => {
    void loadQuestions()
  }, [loadQuestions])

  // 题目到手后恢复本地状态。**一次读完**（而不是两个 effect 各读一半）：
  // 表单草稿与对话快照都在动 `values`，分两个 effect 写就变成了顺序依赖的竞态。
  //
  // 会话接口（`PUT /sessions/{id}/form`、`GET /sessions/{id}`）都还是 501，
  // 所以本地这两份目前是**唯一**的"不丢输入 / 不丢对话"手段；接口就绪后
  // 草稿降级为同步失败的兜底（`会话持久化方案` §5.1），对话改读服务端快照。
  useEffect(() => {
    if (load.status !== 'ok') return

    const draft = readFormDraft()
    const loaded = loadSession()

    if (loaded) {
      const { data, interrupted } = loaded
      setConversationId(data.sessionId)
      setMessages(data.messages.map(storedToChat))
      setSubmitted(data.form)
      setFormVersion(data.formVersion)
      setRoundIndex(data.roundIndex)

      setPrdContent(data.documents.prd?.content ?? '')
      setApiDocsContent(data.documents.api?.content ?? '')
      setPromptsContent(data.documents.prompts?.content ?? '')
      setApprovedDocs({
        prd: data.documents.prd?.approved === true,
        api: data.documents.api?.approved === true,
        prompts: data.documents.prompts?.approved === true,
      })
      setTruncatedDocs({
        prd: data.documents.prd?.truncated === true,
        api: data.documents.api?.truncated === true,
        prompts: data.documents.prompts?.truncated === true,
      })

      // ⚠️ `loadSession()` 保证这里**不会**是 `generating-*`（已降级成 `review-*`）。
      // 恢复哪一屏直接用它存下来的值 —— 比"按内容猜"准，且 `done` / `chatting` 也能还原。
      setViewState(data.viewState)

      // 被刷新/关页面打断的生成必须**说出来**：只降级不提示的话，
      // 用户会把上一版内容当成刚刚生成出来的（§7.4 那条孤儿生成态的本意就在此）。
      setNotice(
        interrupted
          ? {
              text: `上次生成${DOC_META[interrupted].title}时被刷新打断了，当前显示的是上一次的版本 —— 重试请用底部操作按钮。`,
              warn: true,
            }
          : { text: '已恢复上次的会话（本地记录，服务端还没有会话接口）', warn: false },
      )
    }

    // 表单值优先用草稿（它比会话里的快照新 —— 用户可能在对话开始后又改过表单）；
    // 没有草稿才回落到会话快照，免得对话还在、表单却是空的。
    if (draft) {
      setValues(draft)
      setRestoredDraft(true)
    } else if (loaded) {
      setValues(loaded.data.form)
    }

    setHydrated(true)
    setChatHydrated(true)
  }, [load.status])

  // 防抖写回本地草稿。**不做逐键写入** —— 文档 §2 说 localStorage 是同步 API、
  // 会阻塞主线程，只该在关键节点读写。
  useEffect(() => {
    if (!hydrated) return
    const timer = window.setTimeout(() => {
      // 空表单要**清掉**草稿，而不是写一个空对象进去。
      // 写空对象的话 key 会留下来：「清空重来」之后本地还挂着一条记录，
      // 看起来像没清干净（虽然读回来看作"没有草稿"）。顺带也不会在首次加载时
      // 就凭空造一个空草稿 key。
      if (Object.keys(values).length === 0) clearFormDraft()
      else writeFormDraft(values)
    }, DRAFT_DEBOUNCE_MS)
    return () => window.clearTimeout(timer)
  }, [values, hydrated])

  // 每完成一轮落一次本地。
  // **不是每收一个 chunk 写一次** —— 流式期间 `messages` 不变（增量在 `streamingContent`），
  // 所以这个 effect 根本不会被触发，localStorage 的同步写不会卡住流式渲染。
  // 状态一变就把整份会话落本地（**防抖**，见 `SESSION_DEBOUNCE_MS` 的说明）。
  //
  // 一次写整份而不是增量：会话是一个整体，"对话在、产物没了"这种半份状态没有意义
  // （也正因如此键才合并成一个）。流式期间不会触发 —— 增量在 `streamingContent` /
  // `streamingDoc` 这两个**不参与本 effect** 的 state 里。
  useEffect(() => {
    if (!chatHydrated || !conversationId || !submitted) return
    // 「防抖 + 最长等待」：正常情况等安静 800ms 再写；但连续变化时不能无限推迟，
    // 从第一次待写算起最多 2000ms 就强制写一次（见 `SESSION_MAX_WAIT_MS` 的说明）。
    const now = Date.now()
    if (sessionPendingSince.current === null) sessionPendingSince.current = now
    const waited = now - sessionPendingSince.current
    const delay = Math.max(0, Math.min(SESSION_DEBOUNCE_MS, SESSION_MAX_WAIT_MS - waited))

    const timer = window.setTimeout(() => {
      sessionPendingSince.current = null
      // 只存**完整的一轮**（user + 一条成功的 ai）。
      //
      // ⚠️ 光按 `!error` 过滤是不够的：失败那一轮里**用户消息身上没有 error 标记**
      // （错在 AI 那条上），于是会存下一条永远等不到回复的提问 —— 下次接续时模型
      // 会以为这句已经被回应过了。所以用户消息必须检查它后面那条回复。
      // 首条 AI 开场本来就没有前置用户消息，所以对 `ai` 不做配对要求。
      const persistable: StoredMessage[] = messages
        .filter((message, index) => {
          if (message.error || message.streaming) return false
          if (message.role === 'ai') return true
          const reply = messages[index + 1]
          return Boolean(reply && reply.role === 'ai' && !reply.error && !reply.streaming)
        })
        .map(({ id, role, content }) => ({ id, role, content }))

      // 只落**有内容**的产物；空的不写 —— 写空串会让"没生成过"和"生成出来是空的"分不清，
      // 而且 `loadSession` 会把空 content 当成没有（两边保持一致）。
      const now = new Date().toISOString()
      const documents: SessionData['documents'] = {}
      for (const kind of DOC_ORDER) {
        const content = readDoc(kind)
        // `trim()` 而不是直接判空：**只有空白**的产物与没有产物是一回事。
        // 不 trim 的话它会以"有内容"的身份通过 `loadSession` 的校验，界面上显示一个空壳文档
        // 却是"待审核"状态。两边必须用同一个判据。
        if (!content.trim()) continue
        documents[kind] = {
          content,
          approved: approvedDocs[kind],
          updatedAt: now,
          // 截断标记跟着正文一起落盘（它是"这正文是残的"这件事的唯一记录）
          truncated: truncatedDocs[kind],
        }
      }

      saveSession({
        sessionId: conversationId,
        formVersion,
        form: submitted,
        messages: persistable,
        roundIndex,
        documents,
        // ⚠️ 存进去是**允许**的，因为 `loadSession` 会把 `generating-*` 降级成 `review-*`
        // 并报出 `interrupted` —— 那正是 §7.4 的孤儿生成态处理。**这两件事必须成对存在**：
        // 只存不降级，刷新后就会显示一个根本没在跑的"生成中"。
        viewState,
        updatedAt: now,
      })
    }, delay)
    return () => window.clearTimeout(timer)
  }, [
    chatHydrated,
    conversationId,
    submitted,
    formVersion,
    messages,
    roundIndex,
    prdContent,
    apiDocsContent,
    promptsContent,
    approvedDocs,
    truncatedDocs,
    viewState,
    readDoc,
  ])

  // 卸载时把两条流都掐掉，别留下悬挂连接
  useEffect(
    () => () => {
      abortRef.current?.abort()
      docAbortRef.current?.abort()
    },
    [],
  )

  /**
   * 提交表单 → 起首轮对话（S0 开场复盘）。
   *
   * 校验在这里**再做一遍**：`StructuredForm` 提交前已经拦过一道，但本函数将来也会被
   * 「重试开场」之类的入口调用，不能假设调用方一定校验过。两处共用 `validateForm`，
   * 规则只有一份，不会漂移。
   */
  const handleStartConversation = useCallback(
    async (formValues: FormValues, config: QuestionsConfig) => {
      const allQuestions = [...config.base_questions, ...config.advanced_questions]
      const errors = validateForm(allQuestions, formValues)
      const errorCount = Object.keys(errors).length
      if (errorCount > 0) {
        // 校验不过就**别进对话页** —— 进去只会看到一个起不来的空对话
        setChatError(`还有 ${errorCount} 项没填好，先回去补一下`)
        setViewState('form')
        return
      }

      // 重开时把上一条流掐掉，避免两轮流同时往界面上写
      abortRef.current?.abort()

      const id = conversationId ?? newConversationId()
      setConversationId(id)
      setFormVersion(config.version)
      setSubmitted(formValues)
      setViewState('chatting')
      setMessages([])
      setStreamingContent('')
      setChatError(null)
      setRoundIndex(1)
      // 新会话开始了，之前那条"已恢复上次的会话"的提示就该消失
      setNotice(null)
      partialRef.current = ''
      setSending(true)

      const controller = new AbortController()
      abortRef.current = controller
      // 本轮是否被输出上限截断。**必须是每次调用自己的局部变量** —— 它是一个
      // 在 `onDone` 里写、在 `await` 之后读的格位；用 state 会读到渲染那一刻的旧值。
      let truncated = false

      try {
        // 后端推的是**结构化 JSON**（见 `deriveDisplay` 的说明），所以：
        // - `content` 收原始 JSON，作为 history 回传时一字不改
        // - `display` 从 JSON 里抠出 `message` 给界面看
        const full = await startConversationStream(
          { form: formValues },
          {
            signal: controller.signal,
            onChunk: (_text, fullText) => {
              partialRef.current = fullText
              setStreamingContent(fullText)
            },
            onDone: (meta) => {
              truncated = meta.truncated === true
            },
          },
        )
        // 截断的 JSON 解析不出下一轮要问什么 —— 按**失败**处理（标红 + 提示），
        // 而不是假装这是一轮正常的回复：`persistable` 会把 error 轮滤掉，
        // 所以刷新后不会留下一个"问了但没人答"的半轮。
        if (truncated) {
          setMessages([makeAiMessage(full, CHAT_TRUNCATED_NOTICE)])
          setChatError(CHAT_TRUNCATED_NOTICE)
        } else {
          setMessages([makeAiMessage(full)])
        }
        setStreamingContent('')
      } catch (error) {
        // 被我们自己掐掉的（重新开场 / 清空重来 / 卸载）不算失败：
        // 继续往下走会往**新一轮**的 messages / chatError 里写东西。
        if (controller.signal.aborted) return
        const message = describeApiError(error)
        const partial = partialRef.current
        // 已经收到的那部分**留着**（标成 error）—— 直接丢掉的话，用户不知道
        // AI 到底说没说、说到哪了
        if (partial) {
          setMessages([makeAiMessage(partial, message)])
        }
        setChatError(message)
        setStreamingContent('')
      } finally {
        // ⚠️ 只有自己仍是"当前那一轮"时才收尾。
        // 不加这个判断的话，被新一次发送顶掉的那一轮结束时会执行 `setSending(false)`，
        // 把正在跑的新一轮误标成"已结束"，输入框提前解禁。
        if (abortRef.current === controller) {
          setSending(false)
          abortRef.current = null
        }
      }
    },
    [conversationId],
  )

  const handleSubmit = useCallback(
    (formValues: FormValues) => {
      if (load.status !== 'ok') return
      void handleStartConversation(formValues, load.config)
    },
    [load, handleStartConversation],
  )

  /**
   * 结构化表单提交。
   *
   * 走的是**同一条** `handleSubmit`（内含 `validateForm` 再校验一次 + 推进到对话步），
   * 所以后面的澄清、生成、审核与选 20 题表单时完全一致 —— 这也是把它并进 V1 而不是
   * 另写一套向导的原因。
   *
   * 多出来的一件事：`extras`（20 题里没有对应题目的那部分结构化内容：页面结构、关键交互、
   * 范围、LLM 说明、可用性）。它在生成时**并进 `known_info`**，因为 `start-stream` 只收
   * `form`，没有 `known_info` 字段 —— 对话阶段看不到它，但它会在生成 PRD 时补上。
   */
  const handleStructuredSubmit = useCallback(
    ({ form, extras, summary }: StructuredSubmit) => {
      setStructuredExtras(extras)
      setStructuredSummary(summary)
      // 同步一份到 `values`：草稿与"恢复上次会话"的逻辑都挂着它
      setValues(form)
      handleSubmit(form)
    },
    [handleSubmit],
  )

  /** 「重试开场」：表单没变，重发一次首轮。 */
  const handleRetryStart = useCallback(() => {
    if (load.status !== 'ok' || !submitted) return
    void handleStartConversation(submitted, load.config)
  }, [load, submitted, handleStartConversation])

  /**
   * 发一条用户消息，接续对话。
   *
   * 失败时的两种收尾**刻意不同**（见下面的注释）：什么都没收到就抛出去让 `ChatInput`
   * 把原文还给用户；已经收到半截就留在界面上并标成失败。
   */
  const handleSendMessage = useCallback(
    async (text: string) => {
      const form = submitted
      if (!form) throw new Error('还没有提交表单，无法接续对话')

      const userMessage: ChatMessage = {
        id: newMessageId('user'),
        role: 'user',
        content: text,
      }
      // 历史要**在这次追加之前**取，否则本轮用户输入会在 history 和 user_input 里各出现一次
      const history: ConversationTurn[] = messages.map(({ role, content }) => ({ role, content }))

      abortRef.current?.abort()
      setMessages((prev) => [...prev, userMessage])
      setStreamingContent('')
      setChatError(null)
      partialRef.current = ''
      setSending(true)

      const controller = new AbortController()
      abortRef.current = controller
      // 同上：本轮是否被截断（局部变量，理由见 `handleStartConversation`）
      let truncated = false

      try {
        const full = await continueConversationStream(
          {
            form,
            history,
            user_input: text,
            // `stage` **不传**：阶段该由服务端状态机推进（`对话阶段设计` §8 第 5 项），
            // 前端并不知道现在是 S1 还是 S3，硬编一个只会骗模型。让后端的默认值生效。
            //
            // `round_index` 前端**确实知道**（就是用户第几次发言），所以传真实值；
            // 但要在 `max_rounds` 处封顶 —— 超过上限时提示词会出现「第 7 / 4 轮」这种
            // 自相矛盾的表述。封顶表示"已到最后一轮，该收尾了"，正是上限的语义。
            round_index: Math.min(roundIndex + 1, MAX_ROUNDS),
          },
          {
            signal: controller.signal,
            onChunk: (_piece, fullText) => {
              partialRef.current = fullText
              setStreamingContent(fullText)
            },
            onDone: (meta) => {
              truncated = meta.truncated === true
            },
          },
        )
        if (truncated) {
          setMessages((prev) => [...prev, makeAiMessage(full, CHAT_TRUNCATED_NOTICE)])
          setChatError(CHAT_TRUNCATED_NOTICE)
        } else {
          setMessages((prev) => [...prev, makeAiMessage(full)])
        }
        setStreamingContent('')
        // 截断的那一轮**不推进轮次**：它没构成一次有效问答，
        // 推进了会让下一轮显示"第 3/4 轮"而实际只问过 1 次。
        if (!truncated) setRoundIndex((prev) => prev + 1)
      } catch (error) {
        // 同上：被掐掉的那一轮不算失败，直接收场
        if (controller.signal.aborted) return
        const message = describeApiError(error)
        const partial = partialRef.current

        if (!partial) {
          // 一个字都没收到（后端没起、503、网络断开…）：
          // 把刚乐观加上的用户消息**撤掉**，然后**抛出**让 `ChatInput` 把原文还回输入框。
          // 不撤的话列表里有一条、输入框里又有一条，看着像发了两遍。
          setMessages((prev) => prev.filter((item) => item.id !== userMessage.id))
          setChatError(message)
          throw error
        }

        // 已经收到半截：用户消息和这半截都留在界面上（标成失败），
        // 这时候把原文塞回输入框反而会让人不知道该怎么改。
        setMessages((prev) => [...prev, makeAiMessage(partial, message)])
        setChatError(message)
        setStreamingContent('')
      } finally {
        if (abortRef.current === controller) {
          setSending(false)
          abortRef.current = null
        }
      }
    },
    [submitted, messages, roundIndex],
  )

  // ---------------------------------------------------------------- 产物生成 / 优化

  /**
   * 对话是否已经聊完 —— 决定对话页「生成 PRD」按钮的显隐。
   *
   * ⚠️ **判据是模型自报的 `stage_status`，也就是"让模型自判阶段"。**
   * `docs/对话阶段设计.md` §8 第 5 项明确要求阶段由**服务端**推进、不让模型自判；
   * 现在没有 `SessionStore`、服务端不下发任何东西，所以这是**唯一的可用信号**。
   * 会话层落地后应改成读 `snapshot.step` / `snapshot.actions`。
   *
   * 另外两条是防御性的：没有对话内容、或者还有流在跑，都不该放行。
   */
  const lastAiMessage = useMemo(() => {
    for (let index = messages.length - 1; index >= 0; index -= 1) {
      if (messages[index].role === 'ai') return messages[index]
    }
    return null
  }, [messages])

  const dialogueFinished =
    !sending && !isGenerating && lastAiMessage !== null && !lastAiMessage.error &&
    readDialogueStageStatus(lastAiMessage.content) === 'done'

  /**
   * 「生成 PRD」按钮的**显示**条件与**可用**条件 —— 刻意分开。
   *
   * | | 条件 | 为什么 |
   * | --- | --- | --- |
   * | 显示 `showStartPrd` | 已有会话且**至少收到一条 AI 回复** | 开场那一轮还没回来时（`messages` 为空）显示一个灰按钮毫无意义；等它回来再出现 |
   * | 可用 `canStartPrd` | 澄清结束（`stage_status=done`）且没有流在跑 | 没聊完就生成，等于拿半份输入去写 PRD |
   *
   * ⚠️ 判据用的是**模型自报的 `stage_status`**，这违反了「阶段由服务端推进、不让模型自判」
   * （`对话阶段设计` §8 第 5 项）—— 没有 `SessionStore` 时它是唯一信号。
   * 会话层落地后换成 `snapshot.actions` 里有没有 `start_generation`。
   *
   * 刻意**显示而禁用**（而不是条件不满足就藏起来）：用户需要知道"这里有个按钮，
   * 但还差点什么"，藏起来只会让人以为流程断了。禁用时旁边有一句说明。
   */
  const showStartPrd = submitted !== null && messages.length > 0
  const canStartPrd = showStartPrd && dialogueFinished

  /**
   * 生成一份产物。
   *
   * 三份走同一条路径，只有"调哪个函数、带哪些上游"不同 —— 所以用 `kind` 分支一次，
   * 而不是写三个几乎一样的函数（那样改错误处理要改三处）。
   *
   * ⚠️ **每个产物一次调用只产出一个分片，且这里是整份生成**（不传 `scope`，
   * 由后端按 `DocumentScope.whole_document()` 处理）。
   *
   * 关于"整份会不会太长"：**真机实测过，不会**。整份 PRD（技能包 6 章结构，含第 2 章的
   * FR-xx / AC-xx 编号）实测远低于输出上限、**没有截断**
   * （`backend/validation_out/prd_skill.txt`）。所以这里的真实代价是
   * **耗时长、失败要整份重来**，不是长度。
   *
   * 仍然**不在这里编一套分片计划** —— 章节清单已经内嵌在提示词里，
   * 前端再抄一份必然漂移（`HANDOFF.md` §4 坑 #10）。
   */
  const handleGenerateDocument = useCallback(
    async (kind: DocKind) => {
      if (isGenerating) return

      // ---------- 接口文档：先检索、用户确认，才真正生成（见 `RagHitsPanel`）----------
      // 取消 = 一个字都不生成。已有内容时（重新生成）不拦：那时用户就是要一份新的，
      // 再走一遍检索确认只会变啰嗦。
      if (kind === 'api' && !ragConfirmedRef.current && !apiDocsContent) {
        setRagBusy(true)
        try {
          const rag = await retrieveApiDocsRag(
            prdContent,
            messages.map((message) => ({ role: message.role, content: message.content })),
          )
          setPendingRag(rag)
        } catch (error) {
          // 检索失败不拦主流程：它只是参考资料，没有它照样能生成
          setDocFailure({
            kind,
            message: `检索参考资料失败（${describeApiError(error)}）—— 可以直接重试生成。`,
          })
        } finally {
          setRagBusy(false)
        }
        return
      }
      ragConfirmedRef.current = false

      // 链条约束：接口文档与提示词套件都必须能追溯到 PRD。
      // 后端也会拦（缺 prd_content → 422），这里先拦是为了不白跑一趟、并给出人话提示。
      if (DOC_META[kind].requiresPrd && !prdContent.trim()) {
        setDocFailure({
          kind,
          message: `${DOC_META[kind].title}必须从 PRD 推导 —— 先生成 PRD。`,
        })
        setViewState(DOC_META[kind].review)
        return
      }

      docAbortRef.current?.abort()
      const controller = new AbortController()
      docAbortRef.current = controller
      docPartialRef.current = ''

      setIsGenerating(true)
      setGeneratingKind(kind)
      setDocFailure(null)
      setStreamingDoc('')
      setViewState(DOC_META[kind].generating)

      // 结构化表单里"20 题装不下"的那部分（页面结构、关键交互、范围、LLM 说明、可用性）
      // 排在最前面：它是用户**明确填过**的内容，比从对话里抠出来的更硬。
      const knownInfo = [structuredExtras, summaryExtras, ragExtras, buildKnownInfo(messages)]
        .filter(Boolean)
        .join('\n\n')
      const context = {
        form: submitted ?? {},
        // ⚠️ 空串**不要传**：后端对"不传"填「（尚无）」，传空串则会把提示词里那一格
        // 变成空 —— 而留空等于告诉模型"这个维度没输入"，它就会拿邻近内容顶替（坑 #3）。
        ...(knownInfo ? { known_info: knownInfo } : {}),
      }
      // 跨片累加的格位：`onDone` 里写、循环里读。
      // **必须是局部变量**：state 在同一个 tick 里读不到刚写的值。
      let truncatedAnyPart = false
      // 失败时报告"断在第几片"（只在分片生成时有意义）
      let failedPart: number | null = null

      const options = {
        signal: controller.signal,
        onChunk: (_piece: string, fullText: string) => {
          docPartialRef.current = fullText
          setStreamingDoc(fullText)
        },
        /**
         * ⚠️ **截断必须在这里收下。** 它在正文里看不出来（末尾就是半行表格），
         * 不接这个标记的话后端辛苦算出来的结论就白丢了 —— 用户会拿一份残缺文档
         * 当成功的结果通过审核、下载、交给下游。
         *
         * 分片生成时**任一片被截断，整份就算残的**，所以跨片取"或"。
         */
        onDone: (meta: StreamChunkMeta) => {
          if (meta.truncated === true) truncatedAnyPart = true
        },
      }

      try {
        // ---------- 先拿分片计划 ----------
        // **切分规则由服务端给**（它解析提示词文件得出，那是章节清单的唯一真相），
        // 前端只负责循环与拼接。计划拿不到就是拿不到：这里**不回退成"整份生成"** ——
        // 那会让接口文档与套件悄悄变回被截断的残文档，而界面上看不出任何区别（坑 #18）。
        const plan = await getDocumentPlan(kind)
        setDocProgress({ kind, total: plan.parts.length, part: 0, label: plan.source })

        // ---------- 逐片生成、逐片累加 ----------
        const pieces: string[] = []
        for (const [offset, part] of plan.parts.entries()) {
          failedPart = offset + 1
          setDocProgress({ kind, total: plan.parts.length, part: offset + 1, label: part.label })
          docPartialRef.current = ''
          setStreamingDoc('')
          const scope = { outline: part.outline, scope: part.scope, spec: part.spec }
          // 三个分支各自直接调用（而不是先拼一个 request 再 as 断言）：
          // `kind` 是判别联合的判别式，分开写才有类型收窄，也免掉三处 `as`。
          if (kind === 'prd') {
            pieces.push(await generatePrdStream({ ...context, scope }, options))
          } else if (kind === 'api') {
            pieces.push(
              await generateApiDocsStream({ ...context, prd_content: prdContent, scope }, options),
            )
          } else {
            pieces.push(
              await generatePromptsStream(
                {
                  ...context,
                  prd_content: prdContent,
                  ...(apiDocsContent ? { api_content: apiDocsContent } : {}),
                  scope,
                },
                options,
              ),
            )
          }
        }

        // ---------- 拼接 ----------
        // 后续分片理论上不该重复文档标题（计划的 `spec` 明确禁止了），但模型不一定听话；
        // 真重复了就会在文首出现两遍标题，所以做一次**保守**清理（见 `stitchParts`）。
        const full = stitchParts(pieces)
        writeDoc(kind, full)
        setDocFailure(null)
        setTruncatedDocs((prev) => ({ ...prev, [kind]: truncatedAnyPart }))
        // 重新生成后要把"已通过"撤掉：内容变了，旧的通过结论不再成立
        setApprovedDocs((prev) => ({ ...prev, [kind]: false }))
        // ⚠️ **必须把视图切回审核页。** 忘了这一句的后果很隐蔽：产物状态是**派生**的
        // （`statusFor` 只看内容/失败/通过），所以界面上照样显示"待审核"，看不出问题；
        // 而 `VIEW_TO_STEP` 又把 `generating-*` 与 `review-*` 映到**同一格**，步骤条也一样。
        // 但盘上留下的是 `generating-*`，下次刷新会被降级并误报"上次生成被打断"。
        setViewState(DOC_META[kind].review)
      } catch (error) {
        // 被掐掉的不算失败（清空重来 / 卸载）
        if (controller.signal.aborted) return
        const message = describeApiError(error)
        // ⚠️ **不把半截内容写进产物**：一次失败的重生成会把上一版好文档顶掉。
        // 分片生成时这条**尤其重要** —— 前几片可能已经成功了，但拼起来的仍然是残的
        // （缺后面几章却看着像完整文档），比"生成失败"危险得多。
        // 界面上仍显示上一版 + 失败原因（`DocumentReview` 的 failed 分支就是这么写的）。
        setDocFailure({
          kind,
          // 分片中断要说清"断在第几片"：只说"生成失败"的话，用户会以为是整份都没跑，
          // 而实际上前面几片已经成功、只是没写进产物（宁可全丢，也不能写成残的）。
          message:
            failedPart !== null
              ? `${message}（分片生成中断在第 ${failedPart} 片，未写入任何内容 —— 已生成的分片不保留，避免留下缺章的残文档）`
              : message,
        })
        setViewState(DOC_META[kind].review)
      } finally {
        setDocProgress(null)
        setStreamingDoc('')
        if (docAbortRef.current === controller) {
          setIsGenerating(false)
          setGeneratingKind(null)
          docAbortRef.current = null
        }
      }
    },
    [isGenerating, prdContent, apiDocsContent, messages, submitted, writeDoc],
  )

  // ---------------------------------------------------------------- 对话 → 摘要回填

  /**
   * 点「生成 PRD」时的**前一步**：把对话里用户明确说过的东西回填进结构化摘要，
   * 让用户在 diff 面板上逐条确认，确认之后才真正开始生成。
   *
   * 为什么要有这一步：澄清对话是自由文本，而 PRD 生成吃的是结构化摘要（`known_info`）。
   * 不把对话里的修正搬过去，用户说了"改成微信小程序"也白说 —— 生成出来的还是表单里那份旧的。
   *
   * 三种出口：
   * - 没有摘要（没走结构化录入，例如旧入口）→ 跳过回填，直接生成；
   * - 回填失败（模型给了不可解析的 JSON，服务端返 502）→ **照常生成**，只提示一句。
   *   回填是**增强**，不该因为它挂了就挡住主流程；
   * - 回填成功 → 打开 diff 面板，等用户的 `确认/拒绝`（见 `applySummaryChanges()`）。
   */
  const handleGeneratePrdWithSync = useCallback(async () => {
    if (!structuredSummary || messages.length === 0) {
      void handleGenerateDocument('prd')
      return
    }

    setSummarySyncBusy(true)
    setSummarySyncError(null)
    try {
      const result = await syncSummaryFromConversation(
        structuredSummary,
        messages.map((message) => ({ role: message.role, content: message.content })),
      )
      const changes = diffSummaries(structuredSummary, result.summary)
      setPendingSummary({ changes })
      if (changes.length === 0) {
        // 没有可回填的内容：不留一个空面板，直接说明并生成
        setPendingSummary(null)
        void handleGenerateDocument('prd')
      }
    } catch (error) {
      setSummarySyncError(
        `对话回填没成功（${describeApiError(error)}）—— 已按原摘要继续生成。`,
      )
      void handleGenerateDocument('prd')
    } finally {
      setSummarySyncBusy(false)
    }
  }, [structuredSummary, messages, handleGenerateDocument])

  /**
   * diff 面板的「确认更新」：把**用户勾选的那几条**写回摘要，然后才开始生成。
   *
   * 只按勾选项写回（而不是整份替换）：用户没勾的项要保持原值 ——
   * 这正是"逐条确认"的意义，不然面板就只是个通知。
   */
  const applySummaryChanges = useCallback(
    (accepted: SummaryChange[]) => {
      const next = accepted.reduce<Record<string, unknown>>((acc, change) => {
        // 只支持顶层与一级下标路径（与 `diffSummaries()` 产出的形状一致）
        const match = /^([A-Za-z_][\w]*)(\[(\d+)\])?(\..+)?$/.exec(change.path)
        if (!match) return acc
        const [, root, , index, rest] = match
        if (index === undefined) {
          if (rest === undefined) acc[root] = change.after
          return acc
        }
        const list = Array.isArray(acc[root]) ? [...(acc[root] as unknown[])] : []
        const position = Number(index)
        if (rest === undefined) {
          list[position] = change.after
        } else {
          const current = (list[position] ?? {}) as Record<string, unknown>
          list[position] = { ...current, [rest.replace(/^\./, '')]: change.after }
        }
        acc[root] = list
        return acc
      }, structuredSummary ? { ...structuredSummary } : {})

      setStructuredSummary(next)
      // 摘要变了 → `known_info` 也要跟着变，否则用户确认的修正进不了提示词
      setSummaryExtras(buildExtrasFromSummary(next))
      setPendingSummary(null)
      void handleGenerateDocument('prd')
    },
    [structuredSummary, handleGenerateDocument],
  )

  /** 「拒绝更改」：摘要一字不动，照常生成（用户只是不接受这次回填）。 */
  const rejectSummaryChanges = useCallback(() => {
    setPendingSummary(null)
    void handleGenerateDocument('prd')
  }, [handleGenerateDocument])

  /**
   * AI 优化：只改一节（F8.6）。
   *
   * ⚠️ **返回的是"被修订的那一节"，不是整篇** —— 所以必须用 `replaceSection` 拼回去，
   * 否则要么整篇被一节替换掉，要么文档里出现两遍同一节。
   */
  const handleOptimizeDocument = useCallback(
    async (kind: DocKind, request: OptimizeRequest) => {
      if (isGenerating) return

      docAbortRef.current?.abort()
      const controller = new AbortController()
      docAbortRef.current = controller
      docPartialRef.current = ''

      setIsGenerating(true)
      setGeneratingKind(kind)
      setDocFailure(null)
      setStreamingDoc('')

      // 结构化表单里"20 题装不下"的那部分（页面结构、关键交互、范围、LLM 说明、可用性）
      // 排在最前面：它是用户**明确填过**的内容，比从对话里抠出来的更硬。
      const knownInfo = [structuredExtras, summaryExtras, ragExtras, buildKnownInfo(messages)]
        .filter(Boolean)
        .join('\n\n')
      const context = {
        form: submitted ?? {},
        ...(knownInfo ? { known_info: knownInfo } : {}),
      }
      const options = {
        signal: controller.signal,
        onChunk: (_piece: string, fullText: string) => {
          docPartialRef.current = fullText
          setStreamingDoc(fullText)
        },
        // ⚠️ **优化不碰 `truncatedDocs`**（与 `handleGenerateDocument` 刻意不同）：
        // 那个标记说的是「**整份**正文被截断过」，而优化只重写一节 ——
        // 把标记清掉等于宣布"这份文档补全了"，而我们并不知道用户有没有补上末尾。
        // 宁可留着一个"末尾可能是缺的"提醒，也不要谎报完整。
      }

      try {
        // ⚠️ 三分支而不是两分支：`kind` 在这三个接口上的**上游要求不同** ——
        // `api` / `prompts` 必须有 `prd_content`，`prd` 不能有（它没有上游）。
        // `OptimizeDocumentRequest` 是判别联合，写成 `kind === 'api' ? A : B`
        // 会让 `prompts` 落进 B 而缺 `prd_content`，编译期就报（已实测报到）。
        const shared = {
          ...context,
          section: request.section,
          current_content: request.currentContent,
          feedback: request.feedback,
        }
        const revised =
          kind === 'api'
            ? await optimizeDocumentStream(
                { ...shared, kind: 'api', prd_content: prdContent },
                options,
              )
            : kind === 'prompts'
              ? await optimizeDocumentStream(
                  { ...shared, kind: 'prompts', prd_content: prdContent },
                  options,
                )
              : await optimizeDocumentStream({ ...shared, kind: 'prd' }, options)

        writeDoc(kind, replaceSection(readDoc(kind), request.section, revised))
        setDocFailure(null)
        // 内容变了 → 旧的"已通过"不再成立
        setApprovedDocs((prev) => ({ ...prev, [kind]: false }))
      } catch (error) {
        if (controller.signal.aborted) return
        setDocFailure({ kind, message: describeApiError(error) })
      } finally {
        setStreamingDoc('')
        if (docAbortRef.current === controller) {
          setIsGenerating(false)
          setGeneratingKind(null)
          docAbortRef.current = null
        }
      }
    },
    [isGenerating, prdContent, messages, submitted, readDoc, writeDoc],
  )

  /**
   * 「结束任务」→ 完成页。
   *
   * ⚠️ **有守卫：三份必须全部通过。** 设计里 `completed` 的 guard 就是这个
   * （`状态机设计` §3.2）；不拦的话用户能从任意一步跳到"全部完成"，
   * 而完成页会声称三份产物都已通过 —— 那是假的。
   *
   * 它同时是 `prompts` 通过之后的落点（见 `STAGE_ACTIONS.prompts.next === null`），
   * 所以在"通过并完成任务"那条路径上会被调用。
   *
   * ⚠️ 声明顺序：必须**排在 `handleApproveDocument` 之前**。后者把它写进了
   * `useCallback` 的依赖数组，而依赖数组是**渲染时**求值的 —— 排在后面会
   * 命中 `const` 的 TDZ，直接 `ReferenceError`。
   */
  const handleEndTask = useCallback(
    /**
     * @param approved 用哪一份"已通过"视图来判断。
     *
     * ⚠️ **必须能显式传进来。** `handleApproveDocument` 刚 `setApprovedDocs` 完就调本函数，
     * 而 state 更新是异步的 —— 本函数闭包里的 `approvedDocs` 还是**旧**的，
     * 于是"通过最后一份"会被自己判成"它还没通过"，拒绝并把人弹回原页（实测踩到）。
     * 所以那边把刚算出来的那份传进来。默认参数用于"按钮直接点"的场景。
     */
    (approved: Record<DocKind, boolean> = approvedDocs) => {
      const missing = DOC_ORDER.filter((kind) => !approved[kind])
      if (missing.length > 0) {
        setNotice({
          text: `还有 ${missing.length} 份产物没过：${missing.map((k) => DOC_META[k].title).join('、')}。`,
          warn: true,
        })
        // 把用户带到**第一个没过的**那一步，而不是干站着
        setViewState(DOC_META[missing[0]].review)
        return
      }
      setNotice(null)
      setViewState('done')
    },
    [approvedDocs],
  )

  /**
   * 「通过」：标记这一份通过，然后按**阶段配置**推进。
   *
   * 推进到哪一步、以及"通过即完成"，都由 `STAGE_ACTIONS` 决定 —— 不在这里写 if 链。
   *
   * ⚠️ **这是本地推进，不是真实的状态变更。** 设计里审核动作是 `approve` 事件，
   * 只能走 `POST /sessions/{id}/events`（那条接口仍是 **501**），而且
   * 「服务端 `state` 是真相，前端不自己实现状态机」（`会话持久化方案` §7.3）。
   * 现在先本地推进，理由与表单提交那里完全相同；会话层就绪后整个删掉换成发事件。
   */
  const handleApproveDocument = useCallback(
    (kind: DocKind) => {
      // ---------- 守卫 1：空产物不能通过 ----------
      // 允许通过一份空文档，等于给后面每一步留下一个没有上游的产物
      if (!readDoc(kind).trim()) {
        setNotice({ text: `${DOC_META[kind].title}还没有内容，不能通过。`, warn: true })
        return
      }
      // ---------- 守卫 2：缺上游不能通过 ----------
      // 链条约束：接口文档与套件都必须能追溯到 PRD（`HANDOFF.md` §1）
      const upstream = STAGE_ACTIONS[kind].upstream
      if (upstream && !approvedDocs[upstream]) {
        setNotice({
          text: `请先把${DOC_META[upstream].title}通过 —— 它是${DOC_META[kind].title}的来源。`,
          warn: true,
        })
        setViewState(DOC_META[upstream].review)
        return
      }

      setNotice(null)
      // ⚠️ 先把新的一份算出来再 set：`handleEndTask` 需要它（见那边的说明）
      const nextApproved = { ...approvedDocs, [kind]: true }
      setApprovedDocs(nextApproved)

      const next = STAGE_ACTIONS[kind].next
      if (next) setViewState(DOC_META[next].review)
      else handleEndTask(nextApproved)
    },
    [readDoc, approvedDocs, handleEndTask],
  )

  /**
   * 某一份产物下载时的文件名。
   *
   * 抽成函数而不是在渲染里现拼：**卡片上要显示这个名字**（用户点下载前就该知道会存成什么），
   * 而实际下载的 `download` 属性必须与显示**逐字一致** —— 两处各拼一次必然漂移。
   */
  const downloadNameFor = useCallback(
    (kind: DocKind): string => {
      const productName = (submitted?.product_name ?? '').trim()
      const stem = DOC_META[kind].fileStem
      return `${productName ? `${safeFileName(productName)}-${stem}` : stem}.md`
    },
    [submitted],
  )

  /**
   * 下载某一份产物为 `.md`。
   *
   * 文件名是 `{产品名}-{主干}.md`，产品名走 `safeFileName()` 清洗 ——
   * 它是**用户填的**，可能含 `/ \ : * ? " < > |`，而这些在 Windows 上非法、
   * 在某些浏览器里还可能被当成路径分隔符。
   *
   * ⚠️ 提示词套件是**多文件**产物（`=== FILE: ===` 分隔），这里**整体下一个文件**，
   * 分隔行原样保留 —— 按文件拆开是尚未实现的那条待办（见 README）。
   */
  const handleDownloadDocument = useCallback(
    (kind: DocKind) => {
      const content = readDoc(kind)
      if (!content.trim()) {
        setNotice({ text: `${DOC_META[kind].title}还没有内容，没法下载。`, warn: true })
        return
      }
      downloadFile(downloadNameFor(kind), content)
    },
    [readDoc, downloadNameFor],
  )

  /** 用户编辑产物正文。 */
  const handleDocChange = useCallback(    (kind: DocKind, content: string) => {
      writeDoc(kind, content)
      // 改过就不再算"已通过"（F4.12：改过就要在重生成前二次确认）
      setApprovedDocs((prev) => (prev[kind] ? { ...prev, [kind]: false } : prev))
    },
    [writeDoc],
  )

  /**
   * 「重新开始」：把这一轮的东西全清掉，回到表单。
   *
   * 清的是**本地这一份会话**（对话 + 三份产物 + 草稿）。服务端没有会话接口，
   * 所以这里不会有"清了本地、服务端还留着"的问题；会话层就绪后它应当变成
   * 「发 `abandon` 事件 + 清本地缓存」。
   *
   * ⚠️ 在飞的流要先掐掉：不掐的话那条流的 `catch` 会在清空之后往状态里写东西
   * （`abortRef` / `docAbortRef` 的守卫按 `controller` 身份判断，所以写不进去，
   * 但连接会挂着）。
   */
  const handleRestart = useCallback(() => {
    abortRef.current?.abort()
    abortRef.current = null
    docAbortRef.current?.abort()
    docAbortRef.current = null
    setValues({})
    setSubmitted(null)
    setRestoredDraft(false)
    setMessages([])
    setStreamingContent('')
    setConversationId(null)
    setFormVersion('')
    setRoundIndex(1)
    setChatError(null)
    setNotice(null)
    setSending(false)
    // 产物一并清掉：它们与对话同属一个会话，留着就会出现"对话没了、文档还在"
    setPrdContent('')
    setApiDocsContent('')
    setPromptsContent('')
    setApprovedDocs({ prd: false, api: false, prompts: false })
    setTruncatedDocs({ prd: false, api: false, prompts: false })
    setDocFailure(null)
    setStreamingDoc('')
    setIsGenerating(false)
    setGeneratingKind(null)
    setViewState('form')
    clearFormDraft()
    clearSession()
  }, [])

  /**
   * 真正交给列表渲染的消息。
   *
   * 把"正在流式接收的那条"**合成**进去，而不是往 `messages` 里塞一个半成品：
   * 这样 `messages` 始终只装**完整轮次**，落本地时不用再过滤，历史也不会被半截内容污染。
   */
  const renderedMessages = useMemo<ChatMessage[]>(() => {
    if (!sending) return messages
    return [
      ...messages,
      {
        id: STREAMING_MESSAGE_ID,
        role: 'ai',
        content: streamingContent,
        // 半截 JSON 也要走同一套抠取 —— 否则流式期间用户看到的是 `{"message": "我先把…`
        display: deriveDisplay(streamingContent, 'ai', true),
        streaming: true,
      },
    ]
  }, [messages, sending, streamingContent])

  /**
   * 当前 `viewState` 对应哪一份产物（生成页与审核页都算）。不在产物页时返回 `null`。
   *
   * **由 `viewState` 决定当前在编哪一份**，而不是另存一个 `activeDoc` state ——
   * 两个都要维护的话，切页时忘改一个就会出现"编辑的是 PRD、状态显示的是接口文档"。
   */
  const activeDoc = useMemo<DocKind | null>(() => {
    for (const kind of DOC_ORDER) {
      if (viewState === DOC_META[kind].generating || viewState === DOC_META[kind].review) {
        return kind
      }
    }
    return null
  }, [viewState])

  /**
   * 产物页底部的按钮。**按阶段配置生成**（`STAGE_ACTIONS`），不在渲染里写 if 链。
   *
   * 每个阶段固定这几类：
   * | 按钮 | 什么时候出现 | 什么时候可点 |
   * | --- | --- | --- |
   * | 通过 | 有内容时 | 未通过 **且** 上游已通过 |
   * | 生成 / 重新生成 | 总是 | 上游已就绪（`handleGenerateDocument` 里还有一道守卫） |
   * | 下一步：X | 有内容 **且已通过** 时 | 总是 |
   * | 进入完成页 | 三份都通过时 | 总是 |
   *
   * ⚠️ **「下一步」要求"已通过"而不是"有内容"** —— 这是流程有序性的关键一处。
   * 只要"有内容"就能往下跳的话，用户可以在没通过 PRD 的情况下生成接口文档，
   * 而那条链条约束在服务端是会 422 的（这里先拦住，免得白跑一趟）。
   * 想看后面几步用调试跳转行，那是**显式的逃生口**，不混进正常按钮里。
   */
  const docActionsFor = useCallback(
    (kind: DocKind): DocumentAction[] => {
      const stage = STAGE_ACTIONS[kind]
      const status = statusFor(kind)
      const approved = approvedDocs[kind]
      const upstreamReady = stage.upstream === null || approvedDocs[stage.upstream]
      const hasContent = status !== 'not_started'
      const actions: DocumentAction[] = []

      if (hasContent) {
        actions.push({
          key: 'approve',
          label: approved ? '已通过' : stage.approveLabel,
          variant: 'primary',
          disabled: approved,
          title: approved
            ? '这一份已经通过了'
            : upstreamReady
              ? undefined
              : `需要先通过${DOC_META[stage.upstream as DocKind].title}`,
          onClick: () => handleApproveDocument(kind),
        })
      }

      actions.push({
        key: 'generate',
        label: hasContent ? '重新生成' : DOC_META[kind].generateLabel,
        variant: 'secondary',
        // 生成也受链条约束（后端缺 prd_content 会 422）—— 这里只做说明，拦截在 handleGenerateDocument
        disabled: !upstreamReady,
        title: upstreamReady
          ? '重新生成会覆盖当前内容 —— 想保住手改的部分，请改用上面的「AI 优化」'
          : `需要先生成${DOC_META[stage.upstream as DocKind].title}`,
        onClick: () => void handleGenerateDocument(kind),
      })

      // 只有**已通过**才给"下一步"：这是有序性的关键（见上面的说明）
      if (approved && stage.next) {
        actions.push({
          key: 'next',
          label: `下一步：${DOC_META[stage.next].title}`,
          variant: 'secondary',
          onClick: () => setViewState(DOC_META[stage.next as DocKind].review),
        })
      }

      // 三份都通过之后，**任何**审核页都要能回完成页。
      // 早先只在 `prompts` 阶段给这个按钮，结果是：从完成页点进「接口文档」看一眼，
      // 就再也回不去了（只能按浏览器后退，或者用调试跳转行）。
      if (DOC_ORDER.every((k) => approvedDocs[k])) {
        actions.push({
          key: 'finish',
          label: '进入完成页',
          variant: 'primary',
          // ⚠️ **不能写成 `onClick: handleEndTask`** —— 那会把 MouseEvent 当成
          // "已通过"那份 map 传进去，于是"缺哪几份"算出来全是缺，永远进不去完成页。
          // `DocumentAction.onClick` 是 `() => void`，多一个可选参数的函数也满足它，
          // **类型检查不会报**。
          onClick: () => handleEndTask(),
        })
      }

      return actions
    },
    [statusFor, approvedDocs, handleApproveDocument, handleGenerateDocument, handleEndTask, viewState],
  )

  const showDone = load.status === 'ok' && viewState === 'done'

  return (
    <div className="mx-auto flex min-h-full max-w-3xl flex-col gap-6 p-8">
      <header className="flex items-center gap-3">
        <FileText className="h-8 w-8 text-primary-600" aria-hidden />
        <div className="flex-1">
          <h1 className="text-2xl font-semibold text-slate-900">harnessprd</h1>
          <p className="text-sm text-slate-500">需求 → PRD → 接口文档 → 提示词套件</p>
        </div>
        {load.status === 'ok' && (
          <button
            type="button"
            onClick={handleRestart}
            className="inline-flex items-center gap-1.5 rounded-lg border border-slate-300 px-3 py-1.5 text-xs text-slate-600 transition hover:bg-slate-50 focus:outline-none focus-visible:ring-2 focus-visible:ring-primary-300"
          >
            <Eraser className="h-3.5 w-3.5" aria-hidden />
            清空重来
          </button>
        )}
      </header>

      <StepProgress viewState={viewState} />

      {/* 恢复提示放在**所有屏之上**：现在恢复的可能是任意一屏（某个产物的审核页 / done /
          对话页），只挂在对话页上的话大部分时候都看不到。
          `warn` 的那条（生成被打断）尤其重要 —— 不说的话用户会把上一版当成刚生成的。 */}
      {notice && (
        <p
          data-testid="app-notice"
          className={[
            'rounded-lg px-3 py-2 text-xs',
            notice.warn
              ? 'border border-amber-200 bg-amber-50 text-amber-800'
              : 'border border-slate-200 bg-slate-50 text-slate-600',
          ].join(' ')}
        >
          {notice.text}
        </p>
      )}

      {load.status === 'loading' && (
        <div className="flex items-center gap-2 rounded-xl border border-slate-200 bg-white p-6 text-sm text-slate-500 shadow-sm">
          <Loader2 className="h-4 w-4 animate-spin" aria-hidden />
          正在取表单题目…
        </div>
      )}

      {load.status === 'error' && (
        <div className="rounded-xl border border-rose-200 bg-rose-50 p-6 shadow-sm">
          <div className="flex items-start gap-2 text-sm text-rose-700">
            <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" aria-hidden />
            <div>
              <p className="font-medium">取不到表单题目：{load.message}</p>
              <p className="mt-2 text-rose-600/90">
                题目<strong className="font-medium">必须</strong>由服务端下发（前端不自备一份，
                否则两份定义必然漂移）。先在 backend/ 下起服务：
                <code className="ml-1 rounded bg-rose-100 px-1 text-xs">
                  .venv\Scripts\python -m uvicorn main:app --port 8000
                </code>
              </p>
              <button
                type="button"
                onClick={() => void loadQuestions()}
                className="mt-3 inline-flex items-center gap-1.5 rounded-lg bg-rose-600 px-3 py-1.5 text-xs font-medium text-white transition hover:bg-rose-700"
              >
                <RefreshCw className="h-3.5 w-3.5" aria-hidden />
                重试
              </button>
            </div>
          </div>
        </div>
      )}

      {load.status === 'ok' && viewState === 'form' && (
        <>
          <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1 text-xs text-slate-500">
            <span>
              {load.config.title} · 版本{' '}
              <strong className="text-slate-700">{load.config.version}</strong>
            </span>
            {restoredDraft && (
              <span className="rounded bg-amber-100 px-1.5 py-0.5 text-amber-800">
                已恢复上次未提交的草稿
              </span>
            )}
            {submitted && (
              <span className="rounded bg-emerald-100 px-1.5 py-0.5 text-emerald-800">
                本次已提交 {Object.keys(submitted).length} 项
              </span>
            )}
          </div>

          <StructuredForm onSubmit={handleStructuredSubmit} submitting={sending} />
        </>
      )}

      {load.status === 'ok' && viewState === 'chatting' && (
        <section className="flex min-h-0 flex-1 flex-col gap-3 rounded-xl border border-slate-200 bg-white p-4 shadow-sm">
          <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
            <h2 className="text-base font-medium text-slate-800">AI 对话</h2>
            <span className="text-xs text-slate-400">
              {sending ? 'AI 正在回复…' : `共 ${messages.length} 条消息`}
              {roundIndex > 1 && ` · 第 ${Math.min(roundIndex, MAX_ROUNDS)}/${MAX_ROUNDS} 轮`}
              {conversationId && (
                <>
                  {' · 会话 '}
                  <code className="rounded bg-slate-100 px-1" title={conversationId}>
                    {conversationId.slice(0, 8)}
                  </code>
                </>
              )}
            </span>
          </div>

          <MessageList messages={renderedMessages} className="min-h-0 flex-1" />

          {messages.length === 0 && !sending && (
            <p className="text-center text-xs text-slate-400">
              还没有开始对话 —— 先在「描述产品」里填完表单并提交。
            </p>
          )}

          {chatError && (
            <div
              role="alert"
              className="flex items-start gap-2 rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-xs text-rose-700"
            >
              <AlertCircle className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden />
              <span className="flex-1">{chatError}</span>
              {messages.length === 0 && (
                <button
                  type="button"
                  onClick={handleRetryStart}
                  className="inline-flex shrink-0 items-center gap-1 rounded border border-rose-300 px-2 py-0.5 transition hover:bg-rose-100"
                >
                  <RefreshCw className="h-3 w-3" aria-hidden />
                  重试开场
                </button>
              )}
            </div>
          )}

          <ChatInput
            onSend={handleSendMessage}
            disabled={sending || !submitted || messages.length === 0}
            placeholder={
              messages.length === 0
                ? '先提交表单开始对话…'
                : '输入你的回答…（Enter 发送，Shift+Enter 换行）'
            }
          />

          {/* ---------- 对话聊完之后才能进生成环节 ----------
              条件见 `showStartPrd` / `canStartPrd`：还没收到 AI 回复时**不显示**
              （开场那一轮还没回来，显示灰按钮没意义），收到之后**显示但可能禁用**，
              并在旁边说明还差什么。 */}
          {summarySyncError && (
            <p role="alert" className="rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-800">
              {summarySyncError}
            </p>
          )}

          {/* 回填的 diff：**生成前**让用户逐条确认（见 `handleGeneratePrdWithSync`）。
              面板自带的两个按钮分别走 `applySummaryChanges()` 与 `rejectSummaryChanges()`，
              两条路都会继续生成 —— 拒绝回填不等于放弃生成。 */}
          {pendingSummary && (
            <SummaryDiffPanel
              changes={pendingSummary.changes}
              busy={summarySyncBusy}
              title={values.product_name}
              onConfirm={applySummaryChanges}
              onReject={rejectSummaryChanges}
            />
          )}

          {showStartPrd && (
            <div className="flex flex-wrap items-center gap-3 border-t border-slate-100 pt-3">
              <button
                type="button"
                data-testid="start-prd"
                onClick={() => void handleGeneratePrdWithSync()}
                disabled={!canStartPrd || summarySyncBusy || pendingSummary !== null}
                className="inline-flex items-center gap-1.5 rounded-lg bg-primary-600 px-4 py-2 text-sm font-medium text-white transition hover:bg-primary-700 focus:outline-none focus-visible:ring-2 focus-visible:ring-primary-400 disabled:cursor-not-allowed disabled:bg-slate-300"
              >
                {summarySyncBusy ? (
                  <Loader2 className="h-4 w-4 animate-spin" aria-hidden />
                ) : (
                  <Sparkles className="h-4 w-4" aria-hidden />
                )}
                {summarySyncBusy ? '正在比对对话…' : prdContent ? '重新生成 PRD' : '生成 PRD'}
              </button>
              <span className="text-xs text-slate-400">
                {pendingSummary
                  ? '上面有对话回填的改动，确认或拒绝之后才会开始生成。'
                  : !dialogueFinished
                    ? '等 AI 说聊完了（`stage_status` = `done`）才能生成。'
                    : prdContent
                      // ⚠️ 纯字符串里**不能写 `**加粗**`** —— 它不是 Markdown，会把星号原样显示给用户
                      ? '已经有了 PRD —— 重新生成会覆盖它；想只改一节请用审核页的「AI 优化」。'
                      : '澄清已结束，可以生成 PRD 了（会先把对话里说过的改动列出来让你确认）。'}
              </span>
            </div>
          )}
        </section>
      )}

      {load.status === 'ok' && activeDoc && (
        <>
          {pendingRag && (
            <RagHitsPanel
              hits={pendingRag.hits}
              corpusSize={pendingRag.corpusSize}
              busy={ragBusy || isGenerating}
              onConfirm={(selected) => {
                setRagExtras(
                  selected.length === 0
                    ? ''
                    : [
                        '接口文档参考资料（检索自团队规范与历史示例）：',
                        ...selected.map(
                          (hit) => `【${hit.kind}】${hit.title}（${hit.source}）\n${hit.content}`,
                        ),
                      ].join('\n\n'),
                )
                setPendingRag(null)
                ragConfirmedRef.current = true
                void handleGenerateDocument('api')
              }}
              onCancel={() => {
                // 取消 = 不调用生成接口（用户明确要求）
                setPendingRag(null)
              }}
            />
          )}
          <DocumentReview
            className="min-h-0 flex-1"
            title={DOC_META[activeDoc].title}
            content={readDoc(activeDoc)}
            streamingContent={streamingDoc}
            status={statusFor(activeDoc)}
            actions={docActionsFor(activeDoc)}
            failureReason={docFailure?.kind === activeDoc ? docFailure.message : undefined}
            truncated={truncatedDocs[activeDoc]}
            progress={
              docProgress?.kind === activeDoc
                ? `正在生成第 ${Math.max(docProgress.part, 1)}/${docProgress.total} 片：${docProgress.label}`
                : undefined
            }
            onContentChange={(content) => handleDocChange(activeDoc, content)}
            onOptimize={(request) => handleOptimizeDocument(activeDoc, request)}
          />

          {/* ⚠️ 该提醒的是**耗时**：一次要几分钟（实测 24–27 秒 + 排队），不说的话
              用户会以为卡死了。至于"会不会太长被截断"—— **接口文档与提示词套件会**
              （由 `truncated` 横幅单独提示），PRD 不会（实测完整）。 */}
          {activeDoc === 'prd' && statusFor('prd') !== 'not_started' && (
            <p className="text-xs text-slate-400">
              整份 PRD 一次生成完，实测需要几十秒（约 3 分钟属于宽裕估计）——
              期间请保持页面打开，刷新会打断这次生成。长度不用担心：实测能一次出全 6 章。
            </p>
          )}

          {activeDoc === 'prompts' && readDoc('prompts') && (
            <p className="text-xs text-slate-400">
              提示词套件是多文件产物，用 <code className="rounded bg-slate-100 px-1">=== FILE: ===</code>{' '}
              分隔。当前只做预览与整篇复制，
              <strong className="font-medium">还没有按文件切分</strong>（见 README 待办）。
            </p>
          )}
        </>
      )}

      {load.status === 'ok' && showDone && (
        <section
          data-testid="done-panel"
          className="flex flex-col gap-4 rounded-xl border border-slate-200 bg-white p-6 shadow-sm"
        >
          <div>
            <h2 className="text-base font-medium text-slate-800">{DONE_NOTE.title}</h2>
            <p className="mt-2 text-sm leading-relaxed text-slate-600">{DONE_NOTE.detail}</p>
            <p className="mt-1 text-xs text-slate-400">
              设计出处：<code className="rounded bg-slate-100 px-1">{DONE_NOTE.source}</code>
            </p>
          </div>

          {/* 三份产物卡片。每张卡能**单独下载**，也能回到对应审核页 ——
              不然"全部完成"就是个死胡同（想再看一眼 PRD 只能按浏览器后退）。 */}
          <ul className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
            {DOC_ORDER.map((kind) => {
              const content = readDoc(kind)
              const empty = !content.trim()
              return (
                <li
                  key={kind}
                  data-doc-card={kind}
                  className="flex flex-col gap-3 rounded-lg border border-slate-200 p-3"
                >
                  <div className="flex flex-col gap-1.5">
                    {/* 标题独占一行：与徽标挤在一行时 `PRD（产品需求文档）` 会在全角括号处折行
                        （实测截图：折成「产品需求文 / 档）」） */}
                    <span className="text-sm font-medium text-slate-800">
                      {DOC_META[kind].title}
                    </span>
                    <div className="flex items-center gap-2">
                      <span className="shrink-0 rounded bg-emerald-100 px-1.5 py-0.5 text-xs text-emerald-800">
                        已通过
                      </span>
                      {/* `shrink-0` + `tabular-nums`：不缩的话「40 字」会被挤成两行 */}
                      <span className="shrink-0 text-xs tabular-nums text-slate-400">
                        {countContentChars(content)} 字
                      </span>
                    </div>
                    {/* 把即将下载的文件名**显示出来**：用户点之前就该知道会存成什么 */}
                    <code
                      className="truncate text-xs text-slate-400"
                      title={downloadNameFor(kind)}
                    >
                      {downloadNameFor(kind)}
                    </code>
                  </div>

                  <div className="mt-auto flex flex-wrap gap-2">
                    <button
                      type="button"
                      data-download={kind}
                      onClick={() => handleDownloadDocument(kind)}
                      disabled={empty}
                      title={empty ? '这一份还没有内容' : '下载为 Markdown 文件'}
                      className="inline-flex items-center gap-1.5 rounded-lg bg-primary-600 px-2.5 py-1.5 text-xs font-medium text-white transition hover:bg-primary-700 focus:outline-none focus-visible:ring-2 focus-visible:ring-primary-400 disabled:cursor-not-allowed disabled:bg-slate-300"
                    >
                      <Download className="h-3.5 w-3.5" aria-hidden />
                      下载 .md
                    </button>
                    <button
                      type="button"
                      data-review={kind}
                      onClick={() => setViewState(DOC_META[kind].review)}
                      className="rounded-lg border border-slate-300 px-2.5 py-1.5 text-xs text-slate-600 transition hover:bg-slate-50"
                    >
                      回到这一份
                    </button>
                  </div>
                </li>
              )
            })}
          </ul>

          {/* ⚠️ 说清提示词套件的多文件契约 —— 否则用户会奇怪"为什么只有一个文件"。
              ⚠️ 这里**不能写 `**加粗**`**：JSX 文本节点不是 Markdown，星号会原样显示
              （这个错本项目已经犯过三次，`pnpm check:text` 会拦住）。 */}
          <p className="text-xs text-slate-400">
            ⚠️ 提示词套件是<strong className="font-medium">多文件</strong>产物，下载下来是
            <strong className="font-medium">一个</strong>带{' '}
            <code className="rounded bg-slate-100 px-1">=== FILE: ===</code> 分隔行的 `.md`；
            按文件拆开还没做（见 README 待办）。
          </p>

          <div className="flex flex-wrap items-center gap-3 border-t border-slate-100 pt-4">
            <button
              type="button"
              data-testid="restart-from-done"
              onClick={handleRestart}
              className="inline-flex items-center gap-1.5 rounded-lg border border-rose-300 px-3 py-1.5 text-xs font-medium text-rose-700 transition hover:bg-rose-50"
            >
              <Eraser className="h-3.5 w-3.5" aria-hidden />
              重新开始（清空本地数据）
            </button>
            <span className="text-xs text-slate-400">
              ⚠️ 服务端<strong className="font-medium">还没有</strong>会话接口，产物只存在这个浏览器里
              —— 先下载或复制走，再清空。
            </span>
          </div>
        </section>
      )}

      {/* 调试用：直接跳任意状态，方便逐个看占位页。接真实流程时删掉。 */}
      {load.status === 'ok' && viewState !== 'form' && (
        <div className="flex flex-wrap items-center gap-1.5 border-t border-slate-100 pt-4">
          <button
            type="button"
            onClick={() => setViewState('form')}
            className="mr-2 inline-flex items-center gap-1.5 rounded-lg border border-slate-300 px-3 py-1.5 text-xs text-slate-600 transition hover:bg-slate-50 focus:outline-none focus-visible:ring-2 focus-visible:ring-primary-300"
          >
            <ArrowLeft className="h-3.5 w-3.5" aria-hidden />
            返回表单
          </button>
          <span className="text-xs text-slate-400">跳到：</span>
          {STEPS.map((step) => (
            <button
              key={step.id}
              type="button"
              onClick={() => setViewState(step.id)}
              className={[
                'rounded border px-2 py-0.5 text-xs transition',
                viewState === step.id
                  ? 'border-primary-300 bg-primary-50 text-primary-800'
                  : 'border-slate-200 text-slate-500 hover:bg-slate-50',
              ].join(' ')}
            >
              {step.label}
            </button>
          ))}
        </div>
      )}
    </div>
  )
}
