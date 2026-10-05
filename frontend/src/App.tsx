import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { AlertCircle, ArrowLeft, Download, Eraser, FileText, Loader2, RefreshCw, Sparkles } from 'lucide-react'

import ChatInput from './components/ChatInput'
import {
  countContentChars,
  type DocumentAction,
  type OptimizeRequest,
} from './components/DocumentReview'
// 04：审阅面板 + 版本历史侧栏的合成体。App 只传业务 props ——
// 版本 hook 与侧栏组件**一行都不在这一层**（全在
// `components/DocumentReviewWithVersions.tsx` 里）。
import DocumentReviewWithVersions from './components/DocumentReviewWithVersions'
// ⚠️ 这里只借 `FormStep` 的类型与校验函数：**第一步的界面已经换成 `StructuredForm`**
// （结构化录入）。`FormStep`（20 题逐题渲染）仍保留在仓库里，想切回去把下面这行换成
// 默认导入、并把 `<StructuredForm .../>` 换回 `<FormStep .../>` 即可。
import { type FormValues, validateForm } from './components/FormStep'
import StructuredForm, {
  summaryFromStoredDraft,
  type StructuredSubmit,
} from './components/StructuredForm'
// ---------- 方案入口（04）----------
// 三个子界面都只负责画，**动作与状态全在本页**（`entryMode` / `formSubView` /
// `importedPrd` / `handleImportPrdAsBaseline` 都在这里）。入口语义因此只有一处。
import ProjectEntryChooser from './components/project-entry/ProjectEntryChooser'
import ImportPrdPanel from './components/project-entry/ImportPrdPanel'
import ImportPromptsPanel from './components/project-entry/ImportPromptsPanel'
import RagHitsPanel from './components/RagHitsPanel'
import SummaryDiffPanel, {
  diffSummaries,
  type SummaryChange,
} from './components/SummaryDiffPanel'
import MessageList from './components/MessageList'
// 顶栏从 `StepProgress`（流程进度：一格 = 一个 ViewState）换成 `ArtifactProgressBar`
// （产出物：一格 = 一份产出物，`done` 由"这份有没有正文"推导）。
//
// ⚠️ `components/StepProgress.tsx` 与 `types/index.ts` 的 `stepsForMode` / `stepIndexOf`
// **刻意留着不删**：全仓只有它自己、本文件的 import、以及 README 两行散文提到它们，
// 没有别处依赖 —— 留着这次改动就能一键回退。真要清理请单独一次提交（连 README 一起）。
import ArtifactProgressBar from './components/ArtifactProgressBar'
// ---------- 观测（03）----------
// 四个展示组件 + 一个 hook。**组件只负责摆放，state 全在 hook 里** ——
// 页面里不该出现"步骤怎么变、秒表怎么走"这类与业务流程无关的代码。
import ClarificationContextPanel from './components/ClarificationContextPanel'
import ClarificationWarnBanner from './components/ClarificationWarnBanner'
import GenerationStepper from './components/GenerationStepper'
import RunSummaryPanel from './components/RunSummaryPanel'
import StreamErrorBanner from './components/StreamErrorBanner'
import { useGenerationObservability } from './hooks/useGenerationObservability'
// Generation Job 编排：**四条产物链路（PRD / 接口文档 / 提示词套件 的生成，以及 AI 优化）
// 都从这里出去**。页面里不再有 createJob / SSE handler / 重连 effect / AbortController。
import { useGenerationJob } from './hooks/useGenerationJob'
import {
  continueConversationStream,
  describeApiError,
  extractStreamingMessage,
  // ⚠️ 四个 `generate*-Stream` / `optimize-document-stream` 与 `getDocumentPlan`
  // **都已经不在这里用了**：产物生成与 AI 优化都改走 `POST /api/jobs`（后台任务）。
  // 那几个前台接口后端仍保留（标了 deprecated），但前端不再调用。
  getQuestions,
  readDialogueStageStatus,
  startConversationStream,
  retrieveApiDocsRag,
  syncSummaryFromConversation,
  type PrdGenerationStage,
  type RagHit,
} from './services/api'
import {
  clearFormDraft,
  clearStructuredIntake,
  clearLocal,
  readFormDraft,
  readStructuredIntake,
  readLocal,
  SESSION_KIND,
  writeFormDraft,
  writeEntryMode,
  writeStructuredIntake,
  writeLocal,
} from './services/storage'
import { downloadBlob, downloadFile, safeFileName } from './services/download'
// ⚠️ 必须起别名：本文件自己有 `loadSession()`（读**本地存储**的那份，还导出了）。
// 直接同名导入会撞车（TS2440），而且更阴的是——`loaded` 会被推断成本地那个类型，
// 于是 `loaded.title` / `loaded.interruptedKind` 报"属性不存在"，看不出真正原因。
import {
  debouncedSaveSession,
  flushPendingSave,
  loadSession as loadRemoteSession,
  saveSession as saveRemoteSession,
} from './services/sessionService'
import { buildZip, splitPromptSuite, type ZipEntry } from './services/zip'
// 三份产物的元信息表（`DOC_META` / `DOC_ORDER` / `DOC_RUN_TYPES`）已搬到 `utils/docMeta`：
// `utils/jobViews` 也要用它把 Job 的 artifact 映射成视图，而它 import 本文件就成环了。
import { DOC_META, DOC_ORDER, DOC_RUN_TYPES } from './utils/docMeta'
// 顶栏「产出物」的格子怎么裁剪、怎么点亮：**纯函数**，所以状态由内容推导而不另存一份
// （刷新后不会出现"进度条说做完了、正文却是空的"），也可以离线断言。
import {
  buildArtifactNodes,
  type ArtifactContents,
  type ArtifactId,
} from './utils/artifactProgress'
import type { JobReview } from './types/job'
import {
  STEPS,
  type ChatMessage,
  type ConversationTurn,
  type DocStatus,
  type QuestionsConfig,
  type EntryMode,
  type FormSubView,
  type ViewState,
  formSubViewForEntryMode,
} from './types'

type LoadState =
  | { status: 'loading' }
  | { status: 'ok'; config: QuestionsConfig }
  | { status: 'error'; message: string }

// ---------------------------------------------------------------- 三份产物

/** 产物类型。与后端 `services/state.py` 的 `DocKind` 一致。 */
type DocKind = 'prd' | 'api' | 'prompts'

// `DOC_META` / `DOC_ORDER` / `DOC_RUN_TYPES` 见 `utils/docMeta.ts`（唯一来源，
// 接入 Generation Job 之后 `utils/jobViews` 与 `hooks/useGenerationJob` 也要用它们）。

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
  /**
   * **上次离开页面时正在生成**的那份产物（`prd` / `api` / `prompts`）。
   *
   * 为什么需要它：服务端按规格在**写入前**就把 `generating-*` 降级成了 `review-*`，
   * 所以「被打断」这件事在库里看不出来 —— 恢复时就没法提示用户，
   * 而他会把半份正文当成完整产物。这个字段归前端所有（快照形状本来就是我们的），
   * 由 `pagehide` 写入、恢复时读取，之后的保存不再带它（自然消失）。
   */
  interruptedKind?: DocKind
  /**
   * **正在进行的生成任务 id**（Generation Job）。
   *
   * 它的唯一用途是**刷新后自动重连**：`loadSession` 读到它 → `GET /api/jobs/{id}`
   * → 还在跑就订阅进度、已收尾就按结论落地（见 `hooks/useGenerationJob` 的重连 effect）。
   *
   * ⚠️ **必须由前端写进快照**（`buildSnapshot`）。后端在任务开始时也会往同一个键写
   * （`session_service.sync_job_started`），但前端每次自动保存都是**整份覆盖** ——
   * 快照里不带它，下一次自动保存就会把后端写的那份抹掉，刷新后便无从重连。
   */
  activeJobId?: string | null
  /**
   * PRD 的**审查结论**（双智能体的 Reviewer 给的）。
   *
   * 同样要由前端写进快照：刷新之后 `prdStage`（内存态）没了，而顶部那排徽标
   * （"审核通过" / "还有 N 条意见没改完" / "这次没审核"）正是从这里复原的。
   */
  prdReviewResult?: JobReview | null
  /**
   * 本次走的**入口**（04）。
   *
   * ⚠️ 这个字段是 04 篇**新加**的：在此之前快照里根本没有它 —— 而服务端
   * `session_service.derive_summary_fields()` 一直在读 `parsed.get("entryMode")`
   * 去填 `plans.entry_mode` 那一列。所以那一列**从来没被写过**，一直是建表默认的
   * `structured`：列表页的"入口"标签对每份方案都显示同一个值，而从列表点进一份
   * 导入型老方案也不会落到对应的导入界面（入口只在 `localStorage` 里记了一个"上次选的"）。
   *
   * 写进来之后，`entry_mode` 列与列表标签才名副其实，老方案也能按自己的入口落地。
   */
  entryMode?: EntryMode
  /**
   * 表单屏停在哪个子视图（04）。**可选**：老快照里没有它，
   * 读的时候用 `formSubViewForEntryMode(entryMode)` 推导即可（需求 §八）。
   */
  formSubView?: FormSubView
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

// 生成任务化（05）之后这里**不再拼接分片**：分片由后端在任务内部做
// （`services/job_runner._consume_sharded`），拼接走 `services/stitch.py`。
// 前端这一层只收 `text_delta` 与终稿，不再自己循环、自己 stitch。

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

/**
 * V2 编辑态通过它传入"要加载哪一条方案"（见 `pages/V2Workbench.tsx`）。
 *
 * ⚠️ **不传 = 与之前完全一样**：V1（路由 `/`）就是这么用的，
 * 所以这条改动对 V1 没有行为影响 —— 新增的只有"传了 id 就去服务端取回来"这一条路径。
 */
export interface AppProps {
  /** 服务端方案 id。`null`/不传 = 新建。 */
  sessionId?: string | null
  /**
   * 首次保存成功后的回调（V2 外壳据此把地址**替换**成 `/v2/{id}`）。
   *
   * 为什么由外壳跳转、而不是 `App` 自己 navigate：V1（`/`）也渲染同一个组件，
   * 那里没有 `/v2/:id` 这条路由 —— 把路由知识塞进 `App` 会让 V1 多出一个不成立的跳转。
   */
  onSessionSaved?: (id: string) => void
}

export default function App({ sessionId = null, onSessionSaved }: AppProps = {}) {
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
  /**
   * 双智能体生成 PRD 的**阶段**（由服务端 `stage` 事件给出，不是前端自己猜的）。
   *
   * 为什么非要有它：`reviewing`（审核）那一段**没有任何流式输出**，界面如果只写「生成中…」，
   * 用户会以为卡死；`rewriting`（重写）时上一稿的正文必须**清空重来**，否则两稿会首尾拼
   * 成一份前后矛盾的 PRD，而用户只会觉得「模型写歪了」。`done` 时 `issues` 非空表示
   * 审核意见没改完就停了（重写有上限，防无限循环），必须显示出来。
   */
  const [prdStage, setPrdStage] = useState<PrdGenerationStage | null>(null)
  /**
   * 当前入口（三套流程）。
   *
   * 它**只影响显隐与跳转**，不改变任何生成逻辑：三条路最终都调同几个生成接口，
   * 差别只在"前面那些步走不走"。所以切入口只是切 `viewState` 与步骤条，
   * 不会出现"某个入口下生成规则不一样"这种第二套真相。
   */
  const [entryMode, setEntryMode] = useState<EntryMode>('structured')
  /**
   * 「表单」这一屏的子视图（04）。**全新会话默认落在 `chooser`** —— 先选路径再进界面。
   * 老会话由 `formSubViewForEntryMode(entryMode)` 推导，跳过选择页。
   */
  const [formSubView, setFormSubView] = useState<FormSubView>('chooser')
  /** 快捷入口里粘贴进来的 PRD 正文（确认后写进 `prd` 产物）。 */
  const [importedPrd, setImportedPrd] = useState('')
  /**
   * 服务端方案 id。首次保存成功前为 `null`（`/v2` 新建态）。
   *
   * 与 `savedIdRef` 成对：state 用于渲染（按钮禁用），ref 给不在依赖数组里的
   * effect/回调读 —— 否则为了一个 id 要去改一堆 deps，改漏一处就是过期闭包。
   */
  const [savedId, setSavedId] = useState<string | null>(sessionId)
  const savedIdRef = useRef<string | null>(sessionId)
  /** 首次保存进行中：相关按钮禁用，避免连点创建出两条方案（需求测试 8）。 */
  const [saveBusy, setSaveBusy] = useState(false)
  const saveBusyRef = useRef(false)
  /** 这份 PRD 是否已作为基准被接受 —— 只影响面板的显示与按钮态，不参与生成逻辑。 */
  const [baselineAccepted, setBaselineAccepted] = useState(false)
  /**
   * 接受基准之后要**自动开起来**的那份产物（`null` = 没有待启动）。
   *
   * 为什么用标记 + effect 而不是当场直接调 `handleGenerateDocument()`：
   * 同一次事件里 `writeDoc()` 还没落到 state 上，当场调用拿到的还是**旧的** prdContent（空），
   * 后端会因缺 `prd_content` 返 422 —— 一个只在「点得快」时出现、极难查的 bug。
   */
  const [pendingAutoGenerate, setPendingAutoGenerate] = useState<DocKind | null>(null)
  /** 正在打包 zip（打包要算 CRC + deflate，产物大时不是瞬间完成）。 */
  const [bundleBusy, setBundleBusy] = useState(false)
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
  /**
   * 最近一次产物失败。记录是**哪一份**失败的 —— 否则切到别的文档页会看到不相干的报错。
   *
   * `requestId`（03）来自 `StreamError`：失败时后端会回一个请求 ID，用户复制给维护者
   * 就能直接定位那一趟日志。没有它时（网络断了、后端没起）保持 `null`，界面不显示复制按钮。
   */
  const [docFailure, setDocFailure] = useState<{
    kind: DocKind
    message: string
    requestId?: string | null
  } | null>(null)
  /** 正在流式接收的产物正文（与 `content` 分开，理由同对话侧的 `streamingContent`）。 */
  const [streamingDoc, setStreamingDoc] = useState('')
  /**
   * 当前正在跑的**生成任务 id**（Generation Job）。
   *
   * - 开始生成时由 hook 写入（`onActiveJobIdChange`）；
   * - 收到 `done` / `error`、或重连时发现任务已收尾 → 清回 `null`；
   * - 它会被写进会话快照（见 `buildSnapshot`），刷新后据此重连。
   *
   * ⚠️ 与 `isGenerating` **不是一回事**：`isGenerating` 还包含「AI 优化」那条前台流式
   * （它没有任务、也就没有 job id）。判断"能不能再开一个任务"用 `isGenerating`，
   * 判断"要不要重连"用 `activeJobId`。
   */
  const [activeJobId, setActiveJobId] = useState<string | null>(null)
  /**
   * `activeJobId` 的 **ref 镜像**（与 `savedIdRef` 同一个理由）。
   *
   * 为什么必须有：会话快照是**整份覆盖**写入的，而 `buildSnapshot` 被几个防抖 effect 调用。
   * 只用 state 的话有一条真实的窗口 —— 点「生成」之后、`setActiveJobId` 生效之前，
   * 那些 effect 会用一份**没有 job id** 的快照把后端刚写进去的 `activeJobId` 抹掉
   * （实测在日志里抓到过 `保存会话时把 generating-prd 降级后再写入`），
   * 于是那次刷新就接不上任务了。ref 在创建任务那一刻就写好，与渲染时机无关。
   */
  const activeJobIdRef = useRef<string | null>(null)
  /** 同时写 ref 与 state —— 清空（`handleRestart`）、恢复（读快照）、开始任务都走它。 */
  const applyActiveJobId = useCallback((jobId: string | null) => {
    activeJobIdRef.current = jobId
    setActiveJobId(jobId)
  }, [])
  /** PRD 的审查结论（落快照，刷新后仍能显示审核徽标）。 */
  const [prdReviewResult, setPrdReviewResult] = useState<JobReview | null>(null)
  /**
   * 会话"第一次待写"的时刻（`null` = 当前没有待写）。
   *
   * 配合 `SESSION_MAX_WAIT_MS` 防止防抖被连续变化饿死；落盘后重置为 `null`。
   */
  const sessionPendingSince = useRef<number | null>(null)
  // ⚠️ 这里原本还有 `docAbortRef` / `docPartialRef` / `lastPartialWriteRef` 三个 ref，
  // 服务于"前台流式的产物生成 / 优化"。四条链路都任务化之后它们没有任何写入者了，
  // 于是成对删掉：产物流的取消与部分正文落盘现在都在 `useGenerationJob` 里
  // （它有自己的 AbortController 与 1.5 秒节流写库）。

  /**
   * 观测（03）：步骤条 / 秒表 / `run_summary` / 上下文占用 / 失败的请求 ID。
   *
   * **只实例化一次**，四个流程（澄清、PRD、接口文档、提示词）共用同一份 —— 每个流程各起
   * 一份的话，切换流程时上一份的秒表还在跑、步骤条还留着上一趟的状态。
   *
   * ⚠️ 这些 `useCallback` 的身份会随秒表每秒变一次（`captureStreamError` 依赖
   * `generationElapsed`），所以它们**不能**出现在任何 effect 的依赖里 —— 现在没有，
   * 加 effect 时要留意。
   */
  /**
   * 整份观测对象也要留一个引用：`useGenerationJob` 需要它（步骤条 / 秒表 / 汇总
   * 都由那一层驱动），下面的解构只是页面自己要用到的那些。
   */
  const obs = useGenerationObservability()
  const {
    runSummary,
    contextUsage,
    generationSteps,
    generationElapsed,
    generationHint,
    errorRequestId,
    clarificationRounds,
    // 观测（04）：本次生成用的 run id —— 产物汇总面板按它归属（取代原先只看 run_type）
    activeRunId,
    resetGeneration,
    resetErrorMeta,
    resetClarification,
    stopTimer,
    copyErrorRequestId,
    createClarificationHandlers,
  } = obs
  // ⚠️ `startRun` / `startTimer` / `beginOptimizeGeneration` / `captureStreamError` /
  // `createOptimizeHandlers` 这几个页面自己的代码**已经不用了**（也没有解构出来）：
  // 它们现在由 `useGenerationJob` 通过 `obs` 直接在内部调用 ——
  // run id 由后端给、优化用 `beginOptimizeGeneration`、错误走 `captureStreamError`。

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

  /**
   * **Generation Job 的编排**（三条生成链路都从这里出去）。
   *
   * 页面这一层只做三件事：传会话上下文、把 hook 返回的 `start*` 接到按钮上、
   * 把 hook 写回的状态渲染出来。`createJob` / SSE handler / 重连 effect /
   * AbortController **一行都不在页面里**（见 `hooks/useGenerationJob.ts`）。
   *
   * 下面三个 `useCallback` 适配器是**有意的**：hook 需要的是
   * `(kind, x) => void` 这样的写入器，而页面持有的是 `setState` 的 updater 形式。
   * 写成内联箭头函数会让它们每次渲染都换身份，进而让 hook 里的 `useCallback`
   * 与重连 effect 反复重建 —— 那类问题只在刷新时才暴露，很难查。
   */
  const setTruncatedFor = useCallback((kind: DocKind, truncated: boolean) => {
    setTruncatedDocs((prev) => ({ ...prev, [kind]: truncated }))
  }, [])
  const clearApprovedFor = useCallback((kind: DocKind) => {
    setApprovedDocs((prev) => ({ ...prev, [kind]: false }))
  }, [])

  /**
   * 从快照恢复 PRD 的审查结论。
   *
   * 为什么要单独一个函数：`prdStage`（驱动顶部徽标）是**内存态**，刷新就没了；
   * 而 `prdReviewResult` 是**落快照**的那份事实。恢复时必须**两处一起写** ——
   * 只写后者的话，刷新后徽标不见了（而用户刚看到过"审核通过"），
   * 这种"东西悄悄消失"的表现比一开始就没有更让人怀疑。
   */
  const restorePrdReview = useCallback((review: JobReview | null | undefined) => {
    setPrdReviewResult(review ?? null)
    setPrdStage(
      review
        ? {
            stage: 'done',
            round: review.round ?? undefined,
            issues: review.issues,
            review_model: review.review_model ?? undefined,
            review_skipped: review.review_skipped ?? false,
          }
        : null,
    )
  }, [])

  const jobGen = useGenerationJob({
    // 任务的 `session_id` 必须是**服务端已存在**的那条（后端会 404）：
    // `savedId` 在首次保存成功之前是 `null`，那时 hook 会给出明确的失败提示。
    sessionId: savedId,
    activeJobId,
    // ⚠️ 传的是 **ref + state 一起写**的那个 setter（见 `applyActiveJobId` 的说明）：
    // 只写 state 时，"任务已创建"到"下一次保存"之间有一份不带 job id 的快照，
    // 整份覆盖会把后端的 `activeJobId` 抹掉。
    onActiveJobIdChange: applyActiveJobId,
    conversationMessages: messages.map((message) => ({
      role: message.role,
      content: message.content,
    })),
    obs,
    writeDoc,
    // ⚠️ 优化要**读**：优化流的正文是"那一节"，需要拿会话里的**整篇**当基准才能拼回显示
    // （见 `hooks/useGenerationJob` 的 optimize 分支）。
    readDoc,
    setStreamingDoc,
    setViewState,
    setPrdStage,
    setPrdReviewResult,
    setDocFailure,
    setTruncated: setTruncatedFor,
    clearApproved: clearApprovedFor,
    setIsGenerating,
    setGeneratingKind,
  })

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

      // 生成任务化（05）：**本地快照里也可能有未收尾的任务**（关标签页/刷新时正在生成）。
      // 把它恢复出来，`useGenerationJob` 的重连 effect 会据此接上后端那条还在跑的流 ——
      // 这正是"刷新即重连"的入口（本机自用那条路；V2 编辑态走下面那个远程加载 effect）。
      applyActiveJobId(data.activeJobId ?? null)
      restorePrdReview(data.prdReviewResult)
      // 入口（04）：老会话按**它自己快照里的** `entryMode` 落地，不再看 localStorage。
      // 快照里有 `formSubView` 就优先用，没有就用 `entryMode` 推导（需求 §八）。
      if (data.entryMode) {
        setEntryMode(data.entryMode)
        writeEntryMode(data.entryMode)
      }
      setFormSubView(data.formSubView ?? formSubViewForEntryMode(data.entryMode ?? 'structured'))

      // 被刷新/关页面打断的生成必须**说出来**：只降级不提示的话，
      // 用户会把上一版内容当成刚刚生成出来的（§7.4 那条孤儿生成态的本意就在此）。
      setNotice(
        data.activeJobId
          ? { text: '检测到上次有一条生成任务还没结束，正在重新接上它的进度…', warn: false }
          : interrupted
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

    // 结构化摘要与附加项是**内存态**，刷新就没 —— 恢复它们，否则「生成 PRD」
    // 会静默退回没有双智能体审核的表单路径（实测踩过）。
    const intake = readStructuredIntake()
    if (intake) {
      setStructuredSummary(intake.summary)
      setStructuredExtras(intake.extras)
    }

    setHydrated(true)
    setChatHydrated(true)
  }, [load.status, applyActiveJobId, restorePrdReview])

  /**
   * V2 编辑态：把服务端那条方案取回来灌进工作台。
   *
   * 几个刻意的决定：
   * - **只在传了 `sessionId` 时才跑**：V1 与 `/v2`（新建）完全不受影响。
   * - **它排在本地恢复之后执行，并且以服务端为准**：本地那份是"这台机器上次没关的会话"，
   *   而编辑一条指定方案时，用户要的显然是那一条。
   * - **`documents` 逐份写回**（`writeDoc`）：产物正文可能有上万字符，它们是工作台的既有状态，
   *   不走新的一套存储，避免出现"两处都能改正文"的第二份真相。
   * - **降级已经由服务端做过**：`loadSession()` 返回的 `viewState` 保证不是 `generating-*`，
   *   这里的 `interruptedKind` 只用来提示"上次生成被打断了"（与 `loadSession()` 的约定一致）。
   */
  useEffect(() => {
    if (load.status !== 'ok' || !sessionId) return
    let cancelled = false
    void (async () => {
      const loaded = await loadRemoteSession(sessionId)
      if (cancelled) return
      if (!loaded) {
        setNotice({
          text: `没能加载方案 ${sessionId}（可能已被删除）—— 当前是新建状态。`,
          warn: true,
        })
        return
      }
      const data = loaded.data
      const form = data.form ?? {}
      setValues(form)
      setSubmitted(Object.keys(form).length > 0 ? form : null)
      setFormVersion(data.formVersion ?? '')
      setConversationId(data.sessionId ?? sessionId)
      setRoundIndex(data.roundIndex ?? 1)
      setMessages(
        (data.messages ?? []).map((message) => ({
          id: message.id,
          role: message.role,
          content: message.content,
        })),
      )
      for (const kind of DOC_ORDER) {
        const doc = data.documents?.[kind]
        if (doc?.content) writeDoc(kind, doc.content)
      }
      setApprovedDocs({
        prd: Boolean(data.documents?.prd?.approved),
        api: Boolean(data.documents?.api?.approved),
        prompts: Boolean(data.documents?.prompts?.approved),
      })
      setTruncatedDocs({
        prd: Boolean(data.documents?.prd?.truncated),
        api: Boolean(data.documents?.api?.truncated),
        prompts: Boolean(data.documents?.prompts?.truncated),
      })
      // 落地哪一屏：**别把用户丢在一个空产物页上**。
      //
      // 为什么需要这条：生成是被刷新打断的，流式正文从没写进产物（它只在结束时落盘），
      // 而服务端把 `generating-prd` 降级成了 `review-prd` —— 于是刷新后停在
      // 「PRD（空的）待审核」，对话还在库里却看不见。用户的描述就是「内容全被清空」（实测）。
      // 规则：目标屏对应的产物没有正文时，退回**对话**（有消息）或**表单**（没消息）。
      const targetView = data.viewState ?? 'form'
      // 「被打断」有两个来源：服务端读取时降级报出的（旧数据），以及前端
      // 自己在 `pagehide` 里记下的（新数据 —— 服务端写入前已降级，只能这样留痕）。
      const interruptedKind = loaded.interruptedKind ?? data.interruptedKind ?? null
      const emptyTarget = DOC_ORDER.find(
        (kind) =>
          (DOC_META[kind].review === targetView || DOC_META[kind].generating === targetView) &&
          !(data.documents?.[kind]?.content ?? '').trim(),
      )
      // 生成任务化（05）：**刷新重连的入口就在这两行**。
      //
      // 服务端把任务 id 写在快照的 `activeJobId` 上（`session_service.sync_job_started`），
      // 而读取时**有任务在跑就不降级** `generating-*` —— 所以这里能同时拿到
      // "停在哪一屏（生成中）"与"接哪个任务"。恢复出 id 之后，
      // `useGenerationJob` 的重连 effect 会 `GET /api/jobs/{id}`：
      // 还在跑就订阅（首帧 `snapshot` 把已有全文交出来）、已收尾就按结论落地。
      //
      // ⚠️ 顺序无所谓（effect 只看 `activeJobId` 有没有值），但**不能漏**：
      // 漏了它，刷新后会停在一个永远不动的"生成中"。
      applyActiveJobId(data.activeJobId ?? null)
      restorePrdReview(data.prdReviewResult)
      // 入口（04）：老会话按**它自己快照里的** `entryMode` 落地，不再看 localStorage。
      // 快照里有 `formSubView` 就优先用，没有就用 `entryMode` 推导（需求 §八）。
      if (data.entryMode) {
        setEntryMode(data.entryMode)
        writeEntryMode(data.entryMode)
      }
      setFormSubView(data.formSubView ?? formSubViewForEntryMode(data.entryMode ?? 'structured'))
      // 落地哪一屏：**留在快照记录的那一步**。
      //
      // 刻意**不**因为「那份产物是空的」就退回对话页：用户上一秒在 PRD 步，把她踢回澄清页
      // 会让人以为流程被重置了（实测反馈：「直接退回到了 AI 澄清页面」）。留在原步 + 说清
      // 「上次被打断、这是部分内容」，用户点一下「生成」就能接着做。
      setViewState(targetView)
      setNotice(
        data.activeJobId
          ? { text: '检测到这条方案还有一个生成任务没结束，正在重新接上它的进度…', warn: false }
          : emptyTarget
            ? {
                text: `已加载方案「${loaded.title}」，但${DOC_META[emptyTarget].title}还没生成完 —— 上次生成被打断了，` +
                  `下面是当时已经收到的部分内容，点「生成」可以重新来一遍。`,
                warn: true,
              }
            : interruptedKind
              ? {
                  text: `已加载方案「${loaded.title}」；上次生成${DOC_META[interruptedKind].title}时被打断，` +
                    `下面是当时已经收到的部分内容，点「生成」可以重新来一遍。`,
                  warn: true,
                }
              : { text: `已加载方案「${loaded.title}」。`, warn: false },
      )
      setHydrated(true)
      setChatHydrated(true)
    })()
    return () => {
      cancelled = true
    }
  }, [load.status, sessionId, writeDoc, applyActiveJobId, restorePrdReview])

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

      // 同一轮防抖里把结构化录入也落下：刷新之后仍能走「摘要 + 双智能体」那条路，
      // 而不是悄悄退回没有审核的表单路径。
      if (structuredSummary) {
        writeStructuredIntake({ summary: structuredSummary, extras: structuredExtras })
      }
    }, DRAFT_DEBOUNCE_MS)
    return () => window.clearTimeout(timer)
  }, [values, hydrated, structuredSummary, structuredExtras])

  /**
   * 组装一份工作台快照。本地保存与 V2 服务端保存**共用这一份组装逻辑**：
   * 两处各写一遍必然漂移（一处加了 `truncated` 另一处没加，就成了「本地有、服务端没有」）。
   *
   * `formOverride` 给「表单刚提交、state 还没生效」的那一刻用（开始对话时的首次保存）；
   * 不传就用 `submitted`。不这么做的话首次保存存下的是空表单，标题会退成记录 id。
   */
  const buildSnapshot = useCallback(
    (formOverride?: Record<string, string>): SessionData => {
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

      // 只落**有内容**的产物；空的不写 —— 写空串会让「没生成过」和「生成出来是空的」分不清。
      const stamped = new Date().toISOString()
      const documents: SessionData['documents'] = {}
      for (const kind of DOC_ORDER) {
        // ⚠️ 任务的"部分正文"由 `useGenerationJob` 自己按 1.5 秒节流写进产物，
        // 所以这里直接读产物就是最新的（早先那个 `inFlightDoc` 覆盖参数已经不需要了）。
        const content = readDoc(kind)
        // `trim()` 而不是直接判空：**只有空白**的产物与没有产物是一回事。
        if (!content.trim()) continue
        documents[kind] = {
          content,
          approved: approvedDocs[kind],
          updatedAt: stamped,
          // 截断标记跟着正文一起落盘（它是「这正文是残的」这件事的唯一记录）
          truncated: truncatedDocs[kind],
        }
      }

      return {
        sessionId: conversationId ?? savedIdRef.current ?? '',
        formVersion,
        // `submitted` 为空时退到 `values`：首存常由「生成 / PRD 入口」触发，
        // 那一刻 `submitted` 还没生效，只认它会让快照表单是空的 → 标题退成记录 id（实测踩到）。
        form: formOverride ?? submitted ?? values,
        messages: persistable,
        roundIndex,
        documents,
        // ⚠️ `generating-*` 存进去是**允许**的：服务端在写入前会把它降级成 `review-*`
        // （需求测试 6 盯的就是这条：生成途中刷新不许卡在 generating）。
        // **但有一个例外**：`activeJobId` 指向的任务真的在跑时，服务端**不降级** ——
        // 那时 `generating-*` 是"活的生成中"，降级会让前端不去重连（后端
        // `session_service.downgrade_session_data` 的 `running_job_ids`）。
        viewState,
        // 生成任务化（05）：这两个字段**必须由前端写进快照**。后端在任务开始/完成时
        // 也会往同一个键写，但前端每次自动保存是**整份覆盖** —— 不带它们就等于
        // 把后端写的那份抹掉，刷新后既接不上任务、也看不到审核结论。
        //
        // ⚠️ `activeJobId` 读的是 **ref 而不是 state**：本函数被几个防抖 effect 调用，
        // 而 state 要等下一次渲染才可见 —— 那个窗口里保存出来的快照会把 job id 写丢
        // （见 `activeJobIdRef` 的说明）。
        activeJobId: activeJobIdRef.current,
        prdReviewResult,
        // 入口（04）：这两项也**必须由前端写进快照** —— 服务端的
        // `derive_summary_fields()` 读的就是 `entryMode`（填 `plans.entry_mode` 那一列），
        // 而整份覆盖意味着"前端不带它 = 把它抹掉"。不带的话列表页的入口标签
        // 与老方案的落地子视图都会退回默认值（这个字段是 04 篇才加上的，见 `SessionData`）。
        entryMode,
        formSubView,
        updatedAt: stamped,
      }
    },
    [
      messages,
      submitted,
      values,
      formVersion,
      roundIndex,
      conversationId,
      viewState,
      approvedDocs,
      truncatedDocs,
      // 入口（04）：`buildSnapshot` 会把它们写进快照，所以回流时必须拿到最新值。
      entryMode,
      formSubView,
      // ⚠️ **不放 `activeJobId`**：它从 ref 读，所以这个回调不必因为它换身份
      // （依赖数组里的 state 只用于"确认快照形状要带这个字段"这件事本身）。
      prdReviewResult,
      readDoc,
    ],
  )

  /**
   * **首次保存**：没有 id 时立刻建一条并记下 id；已有 id 时只刷防抖队列（不重复创建）。
   * 返回 id；失败返回 `null`。
   *
   * 三个刻意的点：
   * 1. **加锁**：`saveBusyRef` 挡住并发（连点按钮、或「开始对话」与「生成」几乎同时触发），
   *    否则会创建出两条方案（需求测试 8 盯的就是这个）。
   * 2. **失败不挡主流程**：只提示一句，用户照样能继续生成（本地那份还在）——
   *    把「存不上」升级成「用不了」是更糟的失败模式。
   * 3. **表单值可显式传入**：`handleSubmit` 那一刻 `submitted` 还没生效。
   *
   * ⚠️ 声明位置必须**早于**所有引用它的 useCallback 依赖数组（依赖数组是渲染时求值的）。
   */
  const ensureSessionSaved = useCallback(
    async (formOverride?: Record<string, string>): Promise<string | null> => {
      const existing = savedIdRef.current
      if (existing) {
        await flushPendingSave()
        return existing
      }
      if (saveBusyRef.current) return null
      saveBusyRef.current = true
      setSaveBusy(true)
      try {
        const result = await saveRemoteSession(buildSnapshot(formOverride))
        if (!result) {
          setNotice({
            text: '方案没能存到服务端 —— 后续改动不会入库（详见浏览器控制台）。',
            warn: true,
          })
          return null
        }
        savedIdRef.current = result.id
        setSavedId(result.id)
        onSessionSaved?.(result.id)
        return result.id
      } finally {
        saveBusyRef.current = false
        setSaveBusy(false)
      }
    },
    [buildSnapshot, onSessionSaved],
  )

  /**
   * 产物正文变化 → 也自动保存。
   *
   * 主保存 effect 的依赖是**对话侧**状态（messages/submitted/...），正文变化它不重跑；
   * 而生成途中每 1.5 秒会写一次部分正文（见 `onChunk`）—— 没有这条，那些部分正文
   * 只会留在内存里，刷新照样丢。顺带也让「手改产物正文」能自动入库。
   */
  useEffect(() => {
    if (!savedIdRef.current) return
    const timer = window.setTimeout(() => {
      const id = savedIdRef.current
      if (id) debouncedSaveSession(buildSnapshot(), id)
    }, 800)
    return () => window.clearTimeout(timer)
  }, [prdContent, apiDocsContent, promptsContent, buildSnapshot])

  /**
   * 视图 / 生成状态变化 → 也要自动保存。
   *
   * ⚠️ **这条是「生成中途刷新能回到 PRD 步」的关键。** 生成 PRD 时 `viewState` 变成
   * `generating-prd`，而主保存 effect 的依赖是对话侧状态（messages / submitted / ...）——
   * 生成期间它们不变，于是**库里记的仍是上一屏**（`chatting`）。刷新后按库里的状态恢复，
   * 用户就被退回了澄清页（实测反馈：刷新依旧退回前置页面）。
   *
   * 存下 `generating-*` 之后，服务端在**写入前**会把它降级成 `review-*`，
   * 所以刷新是落回 PRD 步，而不是"卡在生成中"。
   */
  useEffect(() => {
    if (!savedIdRef.current) return
    const timer = window.setTimeout(() => {
      const current = savedIdRef.current
      if (current) debouncedSaveSession(buildSnapshot(), current)
    }, 300)
    return () => window.clearTimeout(timer)
  }, [viewState, isGenerating, buildSnapshot])

  /**
   * `pagehide` / 页面隐藏 → **立刻把当前状态落库**（需求第 4 条）。
   *
   * 为什么必须做：刷新、关标签页、跳到别的站点时，浏览器**不会等你**那 1 秒防抖。
   * 这里做两件事：
   * ① 用**此刻的状态**覆盖防抖队列里那份旧快照；
   * ② 马上 flush，请求带 `keepalive`，页面卸载后仍能发完。
   *
   * ⚠️ 快照里那份"生成中的部分正文"**不需要在这里特殊处理**了：任务化之后
   * `useGenerationJob` 自己每 1.5 秒把已收到的正文写进产物（`writeDoc`），
   * 所以 `readDoc(kind)` 拿到的就是最新的 —— 早先那个 `inFlightDoc` 参数
   * 是给"前台流式只在结束时落盘"补的，现在没有可补的窗口了。
   *
   * 只做在有 id 之后：`/v2` 新建、还没 id 时**不创建方案**（与需求测试 1 一致）。
   */
  useEffect(() => {
    const leave = () => {
      const id = savedIdRef.current
      if (!id) return
      const snapshot = buildSnapshot()
      // 记下「此刻正在生成哪一份」：下一屏要据此提示用户
      // （服务端会把 generating-* 降级，库里留不下这个信息）
      if (isGenerating && generatingKind) snapshot.interruptedKind = generatingKind
      debouncedSaveSession(snapshot, id)
      void flushPendingSave()
    }
    const onVisibility = () => {
      if (document.visibilityState === 'hidden') leave()
    }
    window.addEventListener('pagehide', leave)
    document.addEventListener('visibilitychange', onVisibility)
    return () => {
      window.removeEventListener('pagehide', leave)
      document.removeEventListener('visibilitychange', onVisibility)
    }
  }, [buildSnapshot, isGenerating, generatingKind])

  // 每完成一轮落一次本地。
  // **不是每收一个 chunk 写一次** —— 流式期间 `messages` 不变（增量在 `streamingContent`），
  // 所以这个 effect 根本不会被触发，localStorage 的同步写不会卡住流式渲染。
  // 状态一变就把整份会话落本地（**防抖**，见 `SESSION_DEBOUNCE_MS` 的说明）。
  //
  // 一次写整份而不是增量：会话是一个整体，"对话在、产物没了"这种半份状态没有意义
  // （也正因如此键才合并成一个）。流式期间不会触发 —— 增量在 `streamingContent` /
  // `streamingDoc` 这两个**不参与本 effect** 的 state 里。
  useEffect(() => {
    // 本地那份要求会话已经起来（它靠 conversationId 做键）；
    // 服务端那份只要求**有 id** —— 例如只贴了 PRD 的快捷入口，对话还没开始也要能存。
    const localReady = Boolean(chatHydrated && conversationId && submitted)
    const remoteId = savedIdRef.current
    if (!localReady && !remoteId) return
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
      const snapshot = buildSnapshot()
      if (localReady) saveSession(snapshot)
      // V2：**有 id 才自动保存**。`/v2` 新建、还没 id 时这里什么都不做 ——
      // 需求测试 1 盯的就是这条：只填表单、不点关键动作，网络面板里不该出现 save。
      if (remoteId) debouncedSaveSession(snapshot, remoteId)
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

  // 卸载时把对话流掐掉，别留下悬挂连接。
  // ⚠️ 产物流（生成 / 优化）**不在这里**：它们由 `useGenerationJob` 管理，
  // 那个 hook 有自己的卸载清理（同样的理由：不掐会留下悬挂连接）。
  useEffect(
    () => () => {
      abortRef.current?.abort()
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
      // 观测（03）：轮次归零，澄清的上下文/分轮统计也要跟着归零 ——
      // 不然新一轮的占用会追加在旧列表后面，界面上显示"第 4 轮"而实际只聊过 1 轮。
      resetClarification()
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
            // 观测（03）：澄清流只在 `done` 里带 `context_usage`（后端按轮估算上下文占用）。
            // ⚠️ 只走这一条路 —— `readStream` 已经把 done 帧里的 usage 交给这个 handler 了，
            // 下面的 `onDone` 里**不要**再读一次 `meta.context_usage`，否则同一轮会记两条。
            ...createClarificationHandlers(),
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
    [conversationId, createClarificationHandlers, resetClarification],
  )

  const handleSubmit = useCallback(
    (formValues: FormValues) => {
      if (load.status !== 'ok') return
      void (async () => {
        // 首存进行中：**整个动作忽略**，不只是跳过第二次保存。
        // 不这么做的话，快速双击「开始澄清对话」会开两路对话流（方案只有一条，但对话是两份）——
        // 保存锁只保证"不重复创建方案"，管不了"动作被触发两次"。
        if (saveBusyRef.current) return
        // 「开始澄清对话」= 关键动作之一：先落库（首次保存），再开对话。
        // 传 `formValues`：此刻 `submitted` 还没被 set 上去（同一事件里 setState 是异步的），
        // 不传的话首次保存存的是空表单，列表里会显示记录 id。
        await ensureSessionSaved(formValues)
        void handleStartConversation(formValues, load.config)
      })()
    },
    [load, handleStartConversation, ensureSessionSaved],
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
            // 观测（03）：接续轮同样只收 `done.context_usage`（理由见 `handleStartConversation`）
            ...createClarificationHandlers(),
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
    [submitted, messages, roundIndex, createClarificationHandlers],
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
  /** 快捷入口（非结构化）下：进度条之外的步骤也要能进，见下面的入口条与可点击步骤条。 */
  const shortcutMode = entryMode !== 'structured'

  /**
   * 切入口（04：由**路径选择页**调用，不再有一条全局入口条）。
   *
   * 三套流程的「起点」现在都落在**同一屏**（`viewState === 'form'`）的不同子视图上：
   * `structured` → 表单；`prd-shortcut` / `prompts-debug` → 各自的粘贴向导。
   * 贴完点「作为基准并继续」（`handleImportPrdAsBaseline`）才会把 `viewState`
   * 抬到 `review-api-docs` / `review-prompts` —— 也就是说**"进入导入向导"与
   * "开始跑这条流程"是两件事**，改造前它们被混在同一个按钮里（旧的入口条一按就直接
   * 跳到 review 屏，用户还没贴 PRD 就先看到一张空的 PRD 审阅页）。
   */
  const chooseEntryMode = useCallback(
    (mode: EntryMode) => {
      setEntryMode(mode)
      writeEntryMode(mode)
      setFormSubView(formSubViewForEntryMode(mode))
      setViewState('form')
      // 进导入向导时把输入框预填成当前会话的 PRD（有的话），省一次粘贴
      if (mode !== 'structured') setImportedPrd((prev) => prev || prdContent)
      setBaselineAccepted(false)
      // 「进导入路径」= 关键动作：一选就落库（沿用改造前入口条对 `prd-shortcut` 的做法，
      // 现在两条导入路径一视同仁）。不这么做的话，用户贴完 PRD 才第一次建会话 ——
      // 而 `handleImportPrdAsBaseline` 会立刻按 `entryMode` 起生成，那时才建会话就晚了。
      if (mode !== 'structured') void ensureSessionSaved()
    },
    [prdContent, ensureSessionSaved],
  )

  /** 步骤条点击：**自由推进**，不做前端门槛（不合条件的生成按钮自己会灰）。 */
  const handleStepSelect = useCallback((next: ViewState) => setViewState(next), [])

  /** 三个子界面的「← 返回路径选择」（需求 §一）。 */
  const backToChooser = useCallback(() => {
    setFormSubView('chooser')
    setViewState('form')
  }, [])

  /**
   * 这次生成能不能走「摘要 + 双智能体审核」那条路。
   *
   * 内存态摘要、或能从表单草稿重建出摘要，都算有。两个都没有时才为 false ——
   * 那时界面必须**明说**这次没有审核，而不是让用户对着结果猜。
   */
  // 挂 `useMemo`：`summaryFromStoredDraft()` 是**同步读 localStorage**，
  // 放在渲染路径上会让流式期间（每个 chunk 重渲一次）反复读存储。
  // 依赖只有这两样，流式期间都不变 —— 增量在 `streamingDoc` 之类的 state 里。
  const prdSummaryAvailable = useMemo(
    () => structuredSummary !== null || summaryFromStoredDraft() !== null,
    [structuredSummary, values],
  )

  /**
   * 双智能体的阶段 / 结果标签。
   *
   * ⚠️ 它必须挂在**所有屏之上**（顶部提示区，与 `notice` 同一处）：生成 PRD 有**两个**
   * 入口 —— 对话步的「生成 PRD」和产物页底部的「重新生成」（`docActionsFor`）。
   * 只挂在对话步按钮旁的话，用户在 PRD 页点重新生成时就什么都看不到（实测被抓到）。
   *
   * 显示条件：有阶段事件（正在跑 / 刚跑完），或者"这次没有审核"这件事需要告知
   * （正在生成，或屏幕上已经有 PRD）。终态标签额外要求**屏幕上确有 PRD** ——
   * 否则状态一残留它就在骗人。
   */
  const showPrdStatus =
    prdStage !== null || (!prdSummaryAvailable && (isGenerating || Boolean(prdContent)))
  const prdStatusBadges = showPrdStatus ? (
    <div className="flex flex-wrap items-center gap-2">
      {prdStage && prdStage.stage !== 'done' ? (
        <span
          data-testid="prd-stage"
          className="inline-flex items-center gap-1.5 rounded-md border border-slate-200 bg-slate-50 px-2.5 py-1 text-xs text-slate-600"
        >
          {prdStage.stage === 'reviewing' ? (
            <Loader2 className="h-3 w-3 animate-spin" aria-hidden />
          ) : null}
          {prdStage.stage === 'writing'
            ? `双智能体：正在写第 ${prdStage.round ?? 1} 稿…`
            : prdStage.stage === 'reviewing'
              ? '写完了，正在审核（这一步没有正文输出，属正常）…'
              : `审核提了 ${prdStage.issues?.length ?? 0} 条问题，正在重写…`}
        </span>
      ) : null}
      {prdStage?.stage === 'done' && prdStage.review_skipped ? (
        <span
          data-testid="prd-stage-skipped"
          className="rounded-md border border-amber-200 bg-amber-50 px-2.5 py-1 text-xs text-amber-700"
        >
          审核没跑成：审核员的输出无法解析（或缺问题清单），已按通过处理 ——
          这一稿实际上没经过质检，请自己过一遍。
          {prdStage.review_model ? `（原本要用的审核模型：${prdStage.review_model}）` : ''}
        </span>
      ) : null}
      {prdStage?.stage === 'done' &&
      (prdStage.issues?.length ?? 0) === 0 &&
      !prdStage.review_skipped &&
      prdContent ? (
        <span
          data-testid="prd-stage-passed"
          className="rounded-md border border-emerald-200 bg-emerald-50 px-2.5 py-1 text-xs text-emerald-700"
        >
          双智能体：写作 → 审核通过，没有发现问题。
          {prdStage.review_model ? `（审核模型：${prdStage.review_model}）` : ''}
        </span>
      ) : null}
      {prdStage?.stage === 'done' &&
      (prdStage.issues?.length ?? 0) > 0 &&
      prdContent ? (
        <span
          data-testid="prd-stage-issues"
          className="rounded-md border border-amber-200 bg-amber-50 px-2.5 py-1 text-xs text-amber-700"
        >
          PRD 已生成，但审核还有 {prdStage.issues?.length} 条意见没改完（重写有上限，
          改满一轮就停，避免无限循环）：
          {(prdStage.issues ?? [])
            .map((issue) => issue.problem ?? issue.section ?? '未描述')
            .join('；')}
          {prdStage.review_model ? `（审核模型：${prdStage.review_model}）` : ''}
        </span>
      ) : null}
      {!prdSummaryAvailable ? (
        <span
          data-testid="prd-no-review"
          className="rounded-md border border-amber-200 bg-amber-50 px-2.5 py-1 text-xs text-amber-700"
        >
          这次没有双智能体审核：结构化摘要和表单草稿都没有，只能走表单路径（没有任何质检）。
          填一次结构化表单就会带上 Writer 和 Reviewer 两个智能体。
        </span>
      ) : null}
    </div>
  ) : null

  /**
   * 生成一份产物 —— **只做"生成之前"的事，然后交给 `useGenerationJob`**。
   *
   * 职责边界（这是本次改造的核心）：
   *
   * | 归这里 | 归 hook |
   * | --- | --- |
   * | 接口文档的 RAG 检索与用户确认 | `createJob` + 订阅 SSE |
   * | 链条约束（缺 PRD 不许生成下游） | 正文累积、写库、截断标记 |
   * | 首次落库（任务必须挂在一条已存在的会话上） | 切 `generating-*` / `review-*` |
   * | 结构化摘要缺失时的明确提示 | 步骤条/秒表/汇总、失败收束、刷新重连 |
   *
   * ⚠️ **这里已经不生成任何东西了**：没有分片循环、没有 SSE handler、没有
   * `AbortController`。产物由**后台任务**生成，页面断开也不影响它
   * （这就是把生成任务化的全部意义）。
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

      // 「生成」也是**关键动作**：任务要求会话已存在（后端对不存在的会话返 404），
      // 所以先确保落库。按钮那一路已经调过一次 `ensureSessionSaved()`（幂等，只是 flush），
      // 但对话步的「生成 PRD」与快捷入口的自动生成没有 —— 收在这里，两种入口都安全。
      const sessionIdForJob = await ensureSessionSaved()
      if (!sessionIdForJob) {
        setDocFailure({
          kind,
          message: '方案没能存到服务端，生成任务无法创建 —— 检查网络后重试（详见浏览器控制台）。',
        })
        return
      }

      if (kind === 'prd') {
        // ⚠️ **任务接口只吃结构化摘要**（技能包 8 字段），没有"20 题表单"那条入口。
        // 摘要来自内存，刷新后由本机草稿重建（`summaryFromStoredDraft`）。
        // 两个都没有时**明确报错**，而不是偷偷退回一条没有审核的路径 ——
        // 那正是 `HANDOFF.md` §4 坑 #4 说的"缺输入就自己编"。
        const summary = structuredSummary ?? summaryFromStoredDraft()
        if (!summary) {
          setDocFailure({
            kind,
            message:
              '这次没有结构化摘要，无法创建 PRD 生成任务 —— 请回到第一步填一次结构化表单' +
              '（或刷新页面让本机草稿恢复）。',
          })
          setViewState(DOC_META.prd.review)
          return
        }
        await jobGen.startPrd(summary)
        return
      }

      if (kind === 'api') {
        await jobGen.startApiDocs(prdContent)
        return
      }
      await jobGen.startPrompts(prdContent, apiDocsContent)
    },
    [
      isGenerating,
      prdContent,
      apiDocsContent,
      messages,
      structuredSummary,
      ensureSessionSaved,
      jobGen,
    ],
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
    // 摘要优先用内存态；内存态没有（刷新过）就用表单草稿重建 —— 否则会静默走
    // 没有双智能体审核的表单路径。
    const base = structuredSummary ?? summaryFromStoredDraft()
    if (!base || messages.length === 0) {
      void handleGenerateDocument('prd')
      return
    }

    setSummarySyncBusy(true)
    setSummarySyncError(null)
    try {
      const result = await syncSummaryFromConversation(
        base,
        messages.map((message) => ({ role: message.role, content: message.content })),
      )
      const changes = diffSummaries(base, result.summary)
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
  }, [structuredSummary, messages, values, handleGenerateDocument])

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
      }, { ...(structuredSummary ?? summaryFromStoredDraft() ?? {}) })

      setStructuredSummary(next)
      // 摘要变了 → `known_info` 也要跟着变，否则用户确认的修正进不了提示词
      setSummaryExtras(buildExtrasFromSummary(next))
      setPendingSummary(null)
      void handleGenerateDocument('prd')
    },
    [structuredSummary, values, handleGenerateDocument],
  )

  /** 「拒绝更改」：摘要一字不动，照常生成（用户只是不接受这次回填）。 */
  const rejectSummaryChanges = useCallback(() => {
    setPendingSummary(null)
    void handleGenerateDocument('prd')
  }, [handleGenerateDocument])

  /**
   * AI 优化：只改一节（F8.6）—— **现在也走 Generation Job**（与其他三条链路一致）。
   *
   * 这里只做三件事：守卫（没内容不优化）、把**整篇**一起交给 hook（后端收尾要把改过的
   * 那一节拼回整篇）、把生成侧上下文塞进 `context`。剩下的全在
   * `useGenerationJob.startOptimize` 里：创建任务、订阅、刷新重连、失败收束。
   *
   * ⚠️ 与生成链路的差别（都由 hook 负责，这里不要重复实现）：
   * **视图不切**（用户就停在审阅页）、没有 Stepper（单段流）、正文是"那一节"，
   * 显示时由 hook 用 `replaceSection` 拼回整篇。
   */
  const handleOptimizeDocument = useCallback(
    async (kind: DocKind, request: OptimizeRequest) => {
      if (isGenerating) return
      const current = readDoc(kind)
      if (!current.trim()) {
        setDocFailure({ kind, message: `${DOC_META[kind].title}还没有内容，没什么可优化的。` })
        return
      }

      // 结构化表单里"20 题装不下"的那部分（页面结构、关键交互、范围、LLM 说明、可用性）
      // 排在最前面：它是用户**明确填过**的内容，比从对话里抠出来的更硬。
      const knownInfo = [structuredExtras, summaryExtras, ragExtras, buildKnownInfo(messages)]
        .filter(Boolean)
        .join('\n\n')

      await jobGen.startOptimize({
        docType: kind,
        section: request.section,
        sectionContent: request.currentContent,
        documentContent: current,
        instruction: request.feedback,
        context: {
          form: submitted ?? {},
          ...(knownInfo ? { known_info: knownInfo } : {}),
          // ⚠️ 修接口文档 / 套件时要能回指 PRD（服务层对这两类**强制**要求 prd_content）。
          // `kind === 'prd'` 时**不能传**：它没有上游，服务层的签名也不收。
          ...(kind === 'prd' ? {} : { prd_content: prdContent }),
        },
      })
    },
    [
      isGenerating,
      prdContent,
      messages,
      submitted,
      structuredExtras,
      summaryExtras,
      ragExtras,
      readDoc,
      setDocFailure,
      jobGen,
    ],
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
   *
   * @param options.navigate 默认 `true`：通过之后按 `STAGE_ACTIONS.next` 推进到下一屏。
   *   传 `false` = **只标通过、留在原地**。PRD 页的「跳过接口文档，直接生成提示词」要它：
   *   默认那一跳会落在**接口文档页**（`STAGE_ACTIONS.prd.next === 'api'`），而那条路的下一站
   *   是提示词 —— 中间闪一屏接口文档会让人以为"点了跳过却进了接口文档"。
   */
  const handleApproveDocument = useCallback(
    (kind: DocKind, options?: { navigate?: boolean }) => {
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
      // ⚠️ `next === null`（最后一份）时**必须**照旧走 `handleEndTask` ——
      // 不能因为"这次不跳页"就把"通过即完成"也一起吞掉。
      if (next) {
        if (options?.navigate !== false) setViewState(DOC_META[next].review)
      } else handleEndTask(nextApproved)
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
      // 快捷入口**没有表单作答**（`submitted` 为空），产品名要从**导入的 PRD 标题**里取
      // （第一个 `# ` 标题），否则下载名会退化成光秃秃的 `PRD.md`，
      // 与「产品名称-文档类型」的约定不符。
      // 末尾的 `PRD` / `产品需求文档` 去掉：那是文档类型，不是产品名。
      const heading = /^#\s+(.+)$/m.exec(prdContent)?.[1]?.trim() ?? ''
      const fromPrd = heading
        .replace(/\s*PRD\s*$/i, '')
        .replace(/产品需求文档$/, '')
        .trim()
      const productName = (submitted?.product_name ?? '').trim() || fromPrd
      const stem = DOC_META[kind].fileStem
      return `${productName ? `${safeFileName(productName)}-${stem}` : stem}.md`
    },
    [submitted, prdContent],
  )

  /**
   * 下载某一份产物为 `.md`。
   *
   * 文件名是 `{产品名}-{主干}.md`，产品名走 `safeFileName()` 清洗 ——
   * 它是**用户填的**，可能含 `/ \ : * ? " < > |`，而这些在 Windows 上非法、
   * 在某些浏览器里还可能被当成路径分隔符。
   *
   * ⚠️ 提示词套件是**多文件**产物（`=== FILE: ===` 分隔），这里**整体下一个文件**，
   * 分隔行原样保留 —— 想按文件拆开请用上面的「打包下载三份产物」（zip 里会拆成单文件）。
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

  /**
   * 一键打包下载三份产物（zip）。
   *
   * 三个刻意的决定：
   * 1. **提示词套件按 `=== FILE: ===` 拆成单个文件**放进 zip —— 它本来就是
   *    "多文件产物"，整份下下来还得让用户自己手动切；拆不出来（老产物没有分隔行）
   *    就整份放一个文件，并在通知里如实说明。
   * 2. **没有的产物不占位、也不失败**：只打包已有的，通知里点名"还没生成的是哪几份"。
   *    直接失败会被当成按钮坏了；静默少文件则会被当成产物齐了。
   * 3. **打包在本地做**（见 `services/zip.ts`）：离线可用，也不把产物再传回服务端。
   */
  const handleDownloadBundle = useCallback(() => {
    const folder = safeFileName((submitted?.product_name ?? '').trim(), 'harnessprd-产物')
    const entries: ZipEntry[] = []
    const missing: DocKind[] = []
    let suiteNote = ''
    for (const kind of DOC_ORDER) {
      const content = readDoc(kind).trim()
      if (!content) {
        missing.push(kind)
        continue
      }
      if (kind === 'prompts') {
        const files = splitPromptSuite(content)
        if (files.length > 0) {
          for (const file of files) {
            entries.push({
              path: `${folder}/${DOC_META.prompts.fileStem}/${file.path}`,
              content: file.content,
            })
          }
          suiteNote = `提示词套件拆成 ${files.length} 个文件`
          continue
        }
        suiteNote = '提示词套件没有分隔行，整份存放'
      }
      entries.push({ path: `${folder}/${DOC_META[kind].fileStem}.md`, content })
    }
    if (entries.length === 0) {
      setNotice({ text: '三份产物都还没有内容，没有可打包的东西。', warn: true })
      return
    }
    setBundleBusy(true)
    void (async () => {
      try {
        const bytes = await buildZip(entries)
        downloadBlob(
          `${folder}-三份产物.zip`,
          new Blob([bytes as BlobPart], { type: 'application/zip' }),
        )
        setNotice({
          text:
            `已打包 ${entries.length} 个文件（${(bytes.length / 1024).toFixed(1)} KB` +
            `${suiteNote ? '，' + suiteNote : ''}）；` +
            (missing.length > 0
              ? `还没生成：${missing.map((k) => DOC_META[k].title).join('、')}`
              : '三份产物齐全'),
          warn: missing.length > 0,
        })
      } catch (error) {
        setNotice({ text: `打包失败：${describeApiError(error)}`, warn: true })
      } finally {
        setBundleBusy(false)
      }
    })()
  }, [readDoc, submitted])

  /** 用户编辑产物正文。 */
  const handleDocChange = useCallback(    (kind: DocKind, content: string) => {
      writeDoc(kind, content)
      // 改过就不再算"已通过"（F4.12：改过就要在重生成前二次确认）
      setApprovedDocs((prev) => (prev[kind] ? { ...prev, [kind]: false } : prev))
    },
    [writeDoc],
  )

  /**
   * 快捷入口把**外部 PRD** 当基准：写进 `prd` 产物并直接标记「已通过」。
   *
   * 为什么可以直接算通过：这份 PRD 是用户给定的事实输入，不是本工具生成的产物 ——
   * 链条约束要求的只是「有 PRD 可读」。把它挂到「必须由本工具生成并通过」上，
   * 快捷入口就永远推不动（这正是它要省掉的那段流程）。
   * 提示词调试入口连接口文档也一并算通过：后端允许 `api_content` 缺失并填「（尚无）」。
   */
  const handleImportPrdAsBaseline = useCallback(() => {
    const text = importedPrd.trim()
    if (!text) return
    writeDoc('prd', text)
    setApprovedDocs((prev) => ({
      ...prev,
      prd: true,
      ...(entryMode === 'prompts-debug' ? { api: true } : {}),
    }))
    setNotice({ text: `已把 ${text.length} 字符的 PRD 作为基准（跳过表单与对话澄清）。`, warn: false })
    setBaselineAccepted(true)
    // "并继续"就该**真的继续**：把该入口要生成的那份产物挂成待启动，
    // 等 PRD 落到 state 之后由下面的 effect 开起来。
    // （接口文档那一步会先弹 RAG 检索结果让你逐条确认 —— 那是有意保留的人工关口。）
    if (entryMode === 'prompts-debug') {
      setViewState('review-prompts')
      setPendingAutoGenerate('prompts')
    } else {
      setViewState('review-api-docs')
      setPendingAutoGenerate('api')
    }
  }, [importedPrd, entryMode, writeDoc])

  /** 待启动的那份产物：等 PRD 内容真的就绪再开（见 `pendingAutoGenerate` 的说明）。 */
  useEffect(() => {
    if (!pendingAutoGenerate) return
    if (!prdContent.trim()) return
    setPendingAutoGenerate(null)
    void handleGenerateDocument(pendingAutoGenerate)
  }, [pendingAutoGenerate, prdContent, handleGenerateDocument])

  /**
   * 「重新开始」：把这一轮的东西全清掉，回到表单。
   *
   * 清的是**本地这一份会话**（对话 + 三份产物 + 草稿）。服务端没有会话接口，
   * 所以这里不会有"清了本地、服务端还留着"的问题；会话层就绪后它应当变成
   * 「发 `abandon` 事件 + 清本地缓存」。
   *
   * ⚠️ 在飞的流要先掐掉：不掐的话那条流的 `catch` 会在清空之后往状态里写东西
   * （守卫按 `controller` 身份判断，所以写不进去，但连接会挂着）。
   * 对话流是 `abortRef`；**产物流（生成 / 优化）是 `jobGen.cancel()`** ——
   * 它只中止订阅，**不取消后端那个任务**（任务照跑完并落库）。
   */
  const handleRestart = useCallback(() => {
    abortRef.current?.abort()
    abortRef.current = null
    jobGen.cancel()
    applyActiveJobId(null)
    setPrdReviewResult(null)
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
    // 入口（04）：「清空重来」= 回到**路径选择页**（而不是直接落在表单上）。
    // 入口本身也一起复位：上一次选的 `prompts-debug` 留着，会让"重新开始"后的顶栏
    // 进度只剩一步，而用户其实还没选过任何路径。
    setEntryMode('structured')
    writeEntryMode('structured')
    setFormSubView('chooser')
    // 观测（03）：整轮观测状态一起清 —— 秒表、步骤条、汇总、上下文档位、失败的请求 ID。
    // ⚠️ 秒表必须**停**：它是个 `setInterval`，不停会一直给这个已经清空的页面喂 state。
    stopTimer()
    resetGeneration()
    resetClarification()
    resetErrorMeta()
    // ⚠️ 双智能体阶段是**上一次生成**的痕迹：不清就会在还没开始生成 PRD 时
    // 挂着「审核通过」（实测被用户当场抓到）。同一批会话态一起清：
    // 摘要/附加项/待确认项都属于这一轮，留着会让下一轮悄悄继承旧输入。
    setPrdStage(null)
    setStructuredSummary(null)
    setStructuredExtras('')
    setSummaryExtras('')
    setRagExtras('')
    setPendingSummary(null)
    setPendingRag(null)
    setSummarySyncError(null)
    clearStructuredIntake()
    setViewState('form')
    clearFormDraft()
    clearSession()
  }, [stopTimer, resetGeneration, resetClarification, resetErrorMeta, jobGen])

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
   * 属于**当前这一趟生成**的汇总（观测 03/04）。
   *
   * ⚠️ 归属键是 **`run_id`**，不再是 `run_type`：分片生成（接口文档、提示词套件）是
   * **N 次请求、N 帧 `run_summary`**，而 `run_type` 三片完全一样 —— 只看它的话，
   * 面板上留下的永远是**最后一片**的账（实测：用户等了 2 分钟，面板写着 12s）。
   * `run_id` 是"这几片是同一趟"的键，同 run 的后到帧覆盖先到帧（后端把整份合计放进后到帧）。
   *
   * ⚠️ 两道都必须：`run_id` 认这一趟，`run_type` 兜住**缺 `run_id` 的老后端**
   * （那时退回改动前的行为，而不是整块面板消失）。`activeRunId` 为空（还没生成过）时
   * 同样只认 `run_type` —— 刷新后重新进页面就是这种状态。
   */
  const docRunSummary = useMemo(() => {
    if (!runSummary || !activeDoc) return null
    const sameRun = Boolean(activeRunId) && runSummary.run_id === activeRunId
    if (sameRun) return runSummary
    // 老后端（帧里没有 run_id）或还没开过 run：退回按 run_type 归属
    if (!runSummary.run_id) {
      return DOC_RUN_TYPES[activeDoc].includes(runSummary.run_type) ? runSummary : null
    }
    return null
  }, [runSummary, activeDoc, activeRunId])

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
      // 提示词调试入口要「跳过前置流程」：不拿「接口文档已通过」当门槛，
      // 只要求**真的有 PRD 文本可读** —— 后端 422 的判据也只有这一条，
      // 前端不另立一套（两套判据必然漂移）。
      const upstreamReady =
        stage.upstream === null ||
        approvedDocs[stage.upstream] ||
        (entryMode === 'prompts-debug' && stage.upstream === 'api' && Boolean(prdContent))
      const hasContent = status !== 'not_started'
      const actions: DocumentAction[] = []

      /**
       * PRD 审阅页的 **forward 分层**（本篇改造）。
       *
       * PRD 是链条的源头，"往下走"其实有**两条**路，改造前这两条路只有后者被说出口：
       *
       * | 出口 | 走什么 |
       * | --- | --- |
       * | 生成接口文档 | 链条主路径。要过 RAG 确认（`RagHitsPanel`），**不绕过** |
       * | 直接生成提示词 | 跳过接口文档。提示词会缺 API 细节，所以摆成**次要出口** |
       *
       * 两条都以"手上真有一份 PRD 正文"为前提，所以门是 `hasContent`。
       * `structured` 是"本来就会生成接口文档"的主路径 —— 跳过那一枚做成文字链
       * （`variant: 'link'`）；另外两个入口是用户**主动选的捷径**，两枚并列，
       * 跳过那一枚降成 `secondary` 即可。
       */
      const showPrdForward = kind === 'prd' && hasContent
      /** 三份都通过时"唯一的深色主按钮"要让给「进入完成页」（见下面两处的 `variant`）。 */
      const allApproved = DOC_ORDER.every((k) => approvedDocs[k])

      if (hasContent) {
        actions.push({
          key: 'approve',
          label: approved ? '已通过' : stage.approveLabel,
          // ⚠️ PRD 页上深色主按钮让给下面的 forward：审核页的主按钮该指向"下一步做什么"，
          // 而 PRD 这一页的下一步就是往下生成。通过本身仍然在这儿（它是一道真实的状态变更，
          // 也是"生成 / 下一步"两道门槛的判据），只是不再抢视觉焦点。
          variant: showPrdForward ? 'secondary' : 'primary',
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
        disabled: !upstreamReady || saveBusy,
        title: upstreamReady
          ? '重新生成会覆盖当前内容 —— 想保住手改的部分，请改用上面的「AI 优化」'
          : `需要先生成${DOC_META[stage.upstream as DocKind].title}`,
        // 「生成 / 重新生成」也是关键动作：首次保存 + 保存中禁用（需求测试 8）
        onClick: () => {
          void (async () => {
            await ensureSessionSaved()
            void handleGenerateDocument(kind)
          })()
        },
      })

      // 只有**已通过**才给"下一步"：这是有序性的关键（见上面的说明）
      //
      // ⚠️ PRD 页除外：那一页的 forward 已经把"去接口文档"这一步包进去了
      // （见 `showPrdForward`），再挂一枚「下一步：接口文档」就是同一件事说两遍。
      if (approved && stage.next && !showPrdForward) {
        actions.push({
          key: 'next',
          label: `下一步：${DOC_META[stage.next].title}`,
          variant: 'secondary',
          onClick: () => setViewState(DOC_META[stage.next as DocKind].review),
        })
      }

      if (showPrdForward) {
        actions.push({
          key: 'forward-api-docs',
          // 主路径 PRD 页的**深色主按钮**。文案是"生成"而不是"通过"：它真的会起一次生成。
          // 已经有接口文档时也不改文案 —— 顶栏与页脚以"下一步是什么"为准，
          // 覆盖风险由 `title` 说清楚（改文案会让同一颗按钮在不同会话里叫两个名字）。
          label: '生成接口文档 →',
          variant: allApproved ? 'secondary' : 'primary',
          title: apiDocsContent.trim()
            ? '会重新生成并覆盖现有接口文档 —— 想只改一节请用接口文档页的「AI 优化」'
            : '会先做一次 RAG 检索（接口规范 + 历史示例），由你逐条确认后才开始生成',
          // ⚠️ **先把 PRD 标成已通过**，再往下生成。不通过就生成会走进死胡同：
          // 生成不看"通过"标志（`handleGenerateDocument` 只要求有 `prd_content`），
          // 但**下一步的「通过」看**（`STAGE_ACTIONS.api.upstream === 'prd'`）——
          // 于是用户生成完接口文档，却卡在那一页被禁用的「通过」上。
          //
          // RAG 门控照旧走：`handleGenerateDocument('api')` 在 `ragConfirmedRef` 为假
          // 且还没有接口文档正文时会先检索并弹 `RagHitsPanel`，确认之后才真生成。
          onClick: () => {
            handleApproveDocument('prd')
            void handleGenerateDocument('api')
          },
        })

        actions.push({
          key: 'skip-api-docs',
          label: '跳过接口文档，直接生成提示词',
          variant: entryMode === 'structured' ? 'link' : 'secondary',
          title: '提示词套件会缺少接口清单与字段契约那部分细节',
          onClick: () => {
            // ⚠️ 只在"这条入口本来会生成接口文档"时问一句。`prompts-debug` 本来就是
            // 跳过接口文档的入口（见 `ENTRY_MODES` 的 hint），在那里再问一遍是噪音。
            if (entryMode !== 'prompts-debug') {
              const ok = window.confirm('跳过接口文档时，提示词可能缺少 API 细节。是否继续？')
              if (!ok) return
            }
            // 与上面同一道理：不先通过 PRD，提示词页那枚「通过」会因为缺上游而禁用
            // （`STAGE_ACTIONS.prompts.upstream === 'prd'`）。
            // `navigate: false` = 别先跳到接口文档页（见 `handleApproveDocument` 的说明）。
            handleApproveDocument('prd', { navigate: false })
            void handleGenerateDocument('prompts')
          },
        })
      }

      // 三份都通过之后，**任何**审核页都要能回完成页。
      // 早先只在 `prompts` 阶段给这个按钮，结果是：从完成页点进「接口文档」看一眼，
      // 就再也回不去了（只能按浏览器后退，或者用调试跳转行）。
      if (allApproved) {
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
    [
      statusFor,
      approvedDocs,
      handleApproveDocument,
      handleGenerateDocument,
      handleEndTask,
      viewState,
      entryMode,
      prdContent,
      apiDocsContent,
    ],
  )

  /**
   * 「产出物」进度条上点某一格跳去哪。
   *
   * ⚠️ **只有 `done` 的格子会被调用** —— `buildArtifactNodes` 只对 `done` 调 `onNavigate`，
   * 所以这里不必再判断"这一份有没有正文"。返回 `undefined` = 这一屏没有对应去处，
   * 那一格就渲染成**不可点**（而不是"点了没反应"）。
   */
  const handleArtifactNavigate = useCallback(
    (id: ArtifactId): (() => void) | undefined => {
      switch (id) {
        case 'requirements':
          // 「需求」= 表单这一屏的**本入口子视图**（structured 是表单，
          // 两个导入入口是各自的粘贴向导）。与「返回导入入口」那颗按钮同一个落点。
          return () => {
            setFormSubView(formSubViewForEntryMode(entryMode))
            setViewState('form')
          }
        case 'clarification':
          return () => handleStepSelect('chatting')
        case 'prd':
          return () => handleStepSelect('review-prd')
        case 'api-docs':
          return () => handleStepSelect('review-api-docs')
        case 'prompts':
          return () => handleStepSelect('review-prompts')
      }
    },
    [entryMode, handleStepSelect],
  )

  /**
   * 顶栏那一排格子（本篇改造）。
   *
   * 输入只有三样：**入口**（裁剪格子数）、**当前屏**（谁是 active）、**各份正文**（谁打勾）。
   * 于是状态完全由内容推导 —— 不另存一份进度，刷新后也不会出现
   * "进度条说做完了、正文却是空的"。
   */
  const artifactNodes = useMemo(() => {
    /**
     * 「需求」那一格的判据之一：表单里有没有有效的产品名。
     *
     * 两个来源取并集：`values` 是**正在填**的那份（刷新后从本机草稿恢复过来），
     * `submitted` 是**已提交**的那份。只看其中一个都会留下一段"明明填了却不算"的窗口。
     */
    const hasProductName = Boolean(
      (values.product_name ?? '').trim() || (submitted?.product_name ?? '').trim(),
    )
    const contents: ArtifactContents = {
      hasProductName,
      hasClarification: messages.length > 0,
      roundIndex,
      prdContent,
      apiDocsContent,
      promptsContent,
    }
    return buildArtifactNodes({
      entryMode,
      viewState,
      contents,
      onNavigate: handleArtifactNavigate,
    })
  }, [
    entryMode,
    viewState,
    values.product_name,
    submitted?.product_name,
    messages.length,
    roundIndex,
    prdContent,
    apiDocsContent,
    promptsContent,
    handleArtifactNavigate,
  ])

  const showDone = load.status === 'ok' && viewState === 'done'
  /** 只要有一份产物有内容就给打包入口 —— 不要求三份齐全（缺哪份会在通知里点名）。 */
  const showBundleButton =
    load.status === 'ok' && DOC_ORDER.some((kind) => readDoc(kind).trim().length > 0)

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

      {/* 顶栏：**产出物**进度（本篇改造）。格子数与点亮状态全部由
          `buildArtifactNodes` 从「入口 + 当前屏 + 各份正文」推导出来 ——
          这里只摆一个组件，没有任何进度判断的逻辑。 */}
      <ArtifactProgressBar nodes={artifactNodes} />

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

      {/* 双智能体阶段与结果也放**所有屏之上**，理由同上面的恢复提示：
          生成 PRD 有两个入口（对话步按钮 / 产物页「重新生成」），只挂一处必然有看不到的时候。 */}
      {prdStatusBadges}

      {/* V2：方案已经落库的可视证据 —— 测试 2/3/5 里不用翻 Network 也看得到它在 */}
      {savedId ? (
        <p data-testid="session-saved" className="text-xs text-slate-400">
          方案已保存到服务端 · {savedId.slice(0, 8)}
        </p>
      ) : null}

      {/* 打包下载也放**所有屏之上**：产物可能分散在三步里看，用户不该为了下载而先跳回去。 */}
      {showBundleButton ? (
        <div className="flex flex-wrap items-center gap-2">
          <button
            type="button"
            data-testid="download-bundle"
            onClick={handleDownloadBundle}
            disabled={bundleBusy}
            className="inline-flex items-center gap-1.5 rounded-lg bg-slate-800 px-3 py-1.5 text-xs font-medium text-white transition hover:bg-slate-900 disabled:cursor-not-allowed disabled:bg-slate-300"
          >
            {bundleBusy ? <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden /> : null}
            {bundleBusy ? '正在打包…' : '打包下载三份产物（.zip）'}
          </button>
          <span data-testid="bundle-hint" className="text-xs text-slate-400">
            只打包已有内容；提示词套件会按 `=== FILE: ===` 拆成单个文件。
          </span>
        </div>
      ) : null}

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

      {/*
        ---------- 表单这一屏：按 `formSubView` 分支（04）----------
        改造前：三套入口是一条**全局入口条**（钉在所有屏之上），一按就直接把 `viewState`
        跳到 review-* —— 用户还没贴 PRD 就先看到一张空的 PRD 审阅页。现在四个子视图都在
        这一屏里，选好、贴完、点「作为基准并继续」才抬 `viewState`（见 `chooseEntryMode`）。
      */}
      {load.status === 'ok' && viewState === 'form' && formSubView === 'chooser' && (
        <ProjectEntryChooser onChoose={chooseEntryMode} />
      )}

      {load.status === 'ok' && viewState === 'form' && formSubView === 'import-prd' && (
        <ImportPrdPanel
          title="导入 PRD：把你的 PRD 贴进来当基准"
          description="跳过表单与对话澄清。确认之后先生成接口文档（那一步会先做 RAG 检索：接口规范 + 历史接口示例，由你逐条确认），再生成提示词套件。"
          value={importedPrd}
          onChange={(next) => {
            setImportedPrd(next)
            // 内容变了就不再是「已确认」的那一份，按钮要能再点一次（改造前同此行为）
            setBaselineAccepted(false)
          }}
          onConfirmBaseline={handleImportPrdAsBaseline}
          onLoadCurrentPrd={() => setImportedPrd(prdContent)}
          canLoadCurrentPrd={Boolean(prdContent.trim())}
          baselineAccepted={baselineAccepted}
          onBack={backToChooser}
        />
      )}

      {load.status === 'ok' && viewState === 'form' && formSubView === 'import-prompts' && (
        <ImportPromptsPanel
          value={importedPrd}
          onChange={(next) => {
            setImportedPrd(next)
            setBaselineAccepted(false)
          }}
          onConfirmBaseline={handleImportPrdAsBaseline}
          onLoadCurrentPrd={() => setImportedPrd(prdContent)}
          canLoadCurrentPrd={Boolean(prdContent.trim())}
          baselineAccepted={baselineAccepted}
          onBack={backToChooser}
        />
      )}

      {load.status === 'ok' && viewState === 'form' && formSubView === 'structured' && (
        <>
          <button
            type="button"
            data-testid="form-back-to-chooser"
            onClick={backToChooser}
            className="self-start text-xs text-slate-500 transition hover:text-slate-800"
          >
            ← 返回路径选择
          </button>

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

          {/* ---------- 观测（03）：澄清上下文接近上限 ----------
              放在「生成 PRD」按钮**正上方**：这条提示的下一步动作就是那个按钮，
              放在顶部用户还要自己把两件事连起来。
              ⚠️ 按钮**复用同一个入口与同一套可用性判据**（`handleGeneratePrdWithSync`
              + `canStartPrd` / 回填未确认 / 保存中）—— 判据放松的话，这条提示就变成了
              绕过「等 AI 说聊完了」这些前置条件的后门。 */}
          <ClarificationWarnBanner
            visible={contextUsage?.level === 'warn' || contextUsage?.level === 'exceed'}
            onGeneratePrd={
              canStartPrd && !summarySyncBusy && pendingSummary === null && !saveBusy
                ? () => void handleGeneratePrdWithSync()
                : undefined
            }
          />
          <ClarificationContextPanel rounds={clarificationRounds} />

          {showStartPrd && (
            <div className="flex flex-wrap items-center gap-3 border-t border-slate-100 pt-3">
              <button
                type="button"
                data-testid="start-prd"
                onClick={() => void handleGeneratePrdWithSync()}
                disabled={
                  !canStartPrd || summarySyncBusy || pendingSummary !== null || saveBusy
                }
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

      {/* 导入路径的**返回入口**（04）。
          粘贴区已经搬到表单屏的子视图里（`ImportPrdPanel` / `ImportPromptsPanel`）——
          它原来挂在这里，而这里已经是"贴完并往下走"之后的那一屏，同一个粘贴框出现两次
          只会让人不知道该用哪个。这条链接补回**入口条被删掉后失去的能力**：
          从审阅屏回到导入向导改基准 PRD（需求 §八：导入路径审阅中点「返回入口」
          → 回到 import-prd / import-prompts，不是 chooser）。 */}
      {load.status === 'ok' && shortcutMode && (
        <button
          type="button"
          data-testid="back-to-import-entry"
          onClick={() => {
            setViewState('form')
            setFormSubView(formSubViewForEntryMode(entryMode))
          }}
          className="self-start text-xs text-slate-500 transition hover:text-slate-800"
        >
          ← 返回导入入口
        </button>
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
          <DocumentReviewWithVersions
            className="min-h-0 flex-1"
            title={DOC_META[activeDoc].title}
            // 04：版本接口要的是**服务端会话 id**（`plans` 那一行的 id），
            // 也就是 `savedId` —— 不是本地生成的 `conversationId`。还没落库时为 null，
            // 那时 `versionsEnabled` 之外的 `enabled` 也会是 false，一个请求都不发。
            sessionId={savedId ?? undefined}
            docType={DOC_META[activeDoc].docType}
            // 生成中不挂版本侧栏（需求 §十二最后一条）：生成期间列表本来就没变化。
            //
            // ⚠️ **两个来源都要看，只看 `isGenerating` 是错的**（实测抓到）：它是**本地
            // state**，只在"本页点了生成"之后为真；而刷新页面后它恒为 false，那一屏靠的是
            // 服务端写进快照的 `viewState='generating-*'`（有任务在跑时后端**不**降级，
            // 正是为了让前端知道要重连）。只看前者的话，"生成到一半刷新"会误挂出版本侧栏
            // —— 而那一刻侧栏里的版本还是旧的，点它会跟正在跑的任务抢同一份产物。
            versionsEnabled={
              !(activeDoc && DOC_META[activeDoc].generating === viewState) &&
              !(isGenerating && generatingKind === activeDoc)
            }
            // 任务空闲时刷一次版本列表：优化是流式的，盯着布尔值会让整个生成过程反复请求。
            jobIdle={!jobGen.isJobRunning}
            // §6.5 的老数据回退：v1 是迁移导入的、metadata 里没有 review 时用它。
            reviewResult={prdReviewResult}
            content={readDoc(activeDoc)}
            streamingContent={streamingDoc}
            status={statusFor(activeDoc)}
            actions={docActionsFor(activeDoc)}
            failureReason={docFailure?.kind === activeDoc ? docFailure.message : undefined}
            truncated={truncatedDocs[activeDoc]}
            // ⚠️ 生成任务化（05）之后这里**不再有分片进度**：分片是后端在任务内部做的
            // （`services/job_runner._consume_sharded`），而 Job 的 SSE 协议目前**不发**
            // 分片级事件（只有 snapshot / text_delta / phase / review / run_summary / done）。
            // 所以用户看到的是 Stepper + 「正在分片生成…可能需要一两分钟」的 hint，
            // 而不是"第 2/3 片"。要恢复那一行得让后端补一个 part 事件（本次未做）。
            onContentChange={(content) => handleDocChange(activeDoc, content)}
            onOptimize={(request) => handleOptimizeDocument(activeDoc, request)}
            // ---------- 观测（03）：四个插槽，内容全在编排层决定 ----------
            stepperSlot={
              // 只在**这一份**正在生成时显示。步骤条本身不带 `run_type`（它是按阶段事件
              // 推出来的），所以生成结束后它就过期了 —— 那时该看下面那个汇总面板。
              isGenerating && generatingKind === activeDoc ? (
                <GenerationStepper
                  steps={generationSteps}
                  elapsedSeconds={generationElapsed}
                  hint={generationHint}
                />
              ) : undefined
            }
            errorSlot={
              // 失败原因已经由 `failureReason` 渲染在同一个横幅里了，这里**只补复制按钮**
              // （`showMessage={false}` 的作用就是只渲染那颗按钮）
              docFailure?.kind === activeDoc && errorRequestId ? (
                <StreamErrorBanner
                  message=""
                  showMessage={false}
                  requestId={errorRequestId}
                  onCopyRequestId={copyErrorRequestId}
                />
              ) : undefined
            }
            // 传 `undefined` 而不是空组件：否则插槽判断为真，正文下面会多一个空的 `mx-4`
            // ⚠️ `streaming` 必须传：任务化之后 `run_summary` 仍是**收尾前**发的那一帧，
            // 不区分"还在跑"的话用户会在生成途中读到一份看起来已完成的汇总。
            summarySlot={
              docRunSummary ? (
                <RunSummaryPanel
                  summary={docRunSummary}
                  streaming={isGenerating && generatingKind === activeDoc}
                />
              ) : undefined
            }
          />

          {/* ⚠️ 该提醒的是**耗时**：一次要几分钟（实测 24–27 秒 + 排队），不说的话
              用户会以为卡死了。至于"会不会太长被截断"—— **接口文档与提示词套件会**
              （由 `truncated` 横幅单独提示），PRD 不会（实测完整）。 */}
          {activeDoc === 'prd' && statusFor('prd') !== 'not_started' && (
            <p className="text-xs text-slate-400">
              {jobGen.isOptimizingJob
                ? '正在按你的反馈重写这一节 —— 刷新不会中断它：任务在后台继续跑，回来后会自动接上进度。'
                : '整份 PRD 一次生成完，实测需要几十秒（约 3 分钟属于宽裕估计）。' +
                  '刷新或关掉页面都不会中断它：生成是后台任务，回来会自动接上进度。' +
                  '长度不用担心：实测能一次出全 6 章。'}
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
