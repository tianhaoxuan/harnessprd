/**
 * Generation Job 的编排 hook：**三条生成链路全部走"创建任务 + 订阅进度"**。
 *
 * ## 它替页面承担了哪些事（页面里一行都不该再有）
 *
 * | 职责 | 说明 |
 * | --- | --- |
 * | `createJob` + `subscribeJob` | 用户点生成之后的全部 HTTP / SSE |
 * | 刷新重连 | 看到 `activeJobId` 就 `getJob` → 已收尾则读结论、在跑则订阅 |
 * | 正文累积与落盘 | `snapshot` 的全文 + 后续 `text_delta` 追加 → `writeDoc` |
 * | 视图/观测/失败 | 切 `generating-*` / `review-*`、喂 `useGenerationObservability`、收错误 |
 * | AbortController | 新订阅前 abort 旧的（StrictMode 双挂载 / 快速连点） |
 *
 * ## 与 `useGenerationObservability` 的分工
 *
 * 观测 hook 管"步骤条、秒表、汇总怎么变"，本 hook 管"任务怎么跑、结果写到哪"。
 * 两者**并列**（本 hook 收下 `obs` 参数），所以这里不重复实现任何 Stepper 逻辑 ——
 * 只把 job 的事件翻译成它认识的那个形状（`PrdGenerationStage` / `RunSummary`）。
 *
 * ## 一处与"前台流式"不同的关键行为
 *
 * **收到 `snapshot` 不要重播打字机**：它可能是上万字的已有草稿。正确做法是
 * 立刻整篇显示（写进 `streamingContent`，生成态下 `DocumentReview` 只认它），
 * 之后只把新到的增量追加在后面。
 *
 * ⚠️ 顺带说明：`DocumentReview` 的显示规则是 `isGenerating ? streamingContent : content`
 * —— **生成中只看 `streamingContent`**。所以"只把增量塞进 streamingContent"会让
 * 重连后的界面变成空的（快照那部分没人显示）。这就是这里 streaming 与 content
 * 都写全文、而不是"content 存全文、streaming 只存增量"的原因。
 */

import { useCallback, useEffect, useRef, useState } from 'react'

import type { GenerationObservability } from './useGenerationObservability'
import type { PrdGenerationStage } from '../services/api'
import { JobApiError, createJob, getJob } from '../services/jobApi'
import type { DocKind, RunSummary, ViewState } from '../types'
import type {
  GenerateJobArtifact,
  JobArtifact,
  JobDocType,
  JobDoneEvent,
  JobReview,
  JobSnapshot,
  JobSnapshotEvent,
} from '../types/job'
import {
  docKindFor,
  generatingViewFor,
  isOptimizeArtifact,
  optimizeArtifactForDocType,
  reviewViewFor,
} from '../utils/jobViews'
import { ARTIFACT_LABEL, readJobStream } from '../utils/jobStream'
// ⚠️ 从组件文件里借一个**纯函数**：优化流的正文是"那一节"，要拼回整篇才能显示，
// 而拼接规则（标题逐字匹配、子节标题兜底、代码围栏里的 `#` 不算标题）只有那一份实现
// （后端 `services/section_edit.py` 是它的镜像）。在这里重写一份必然分叉。
import { replaceSection } from '../components/DocumentReview'

/** 传给 hook 的会话上下文与状态写入器。**刻意是"写状态"而不是"拿状态"**：
 *  hook 不读页面状态（除了 sessionId 与消息），拿到的越多越容易与页面耦合。 */
export interface UseGenerationJobOptions {
  /** 服务端方案 id。为空时**不能**创建任务（后端要求会话已存在）。 */
  sessionId: string | null
  /** 上一次任务 id（来自快照）。有值 + 页面就绪 → 自动重连。 */
  activeJobId: string | null
  /** 记录/清除当前任务 id（要写进快照，否则刷新后无法重连）。 */
  onActiveJobIdChange: (jobId: string | null) => void
  /** 对话历史（`{role, content}`；`ai` 侧传原始 JSON 那一份）。 */
  conversationMessages: Array<{ role: 'user' | 'ai'; content: string }>
  /** 观测 hook 的返回值（步骤条 / 秒表 / 汇总都在里面）。 */
  obs: GenerationObservability
  /** 写某份产物的正文（会同时更新内存与落盘队列）。 */
  writeDoc: (kind: DocKind, content: string) => void
  /**
   * 读某份产物的正文。
   *
   * ⚠️ 只有**优化**需要它：优化流的正文是"那一节"，要拼回整篇才能显示，
   * 而"整篇"就是优化前页面上的那份正文（会话同步在收尾前不会碰它）。
   */
  readDoc: (kind: DocKind) => string
  /** 生成中的流式正文（`DocumentReview` 在 `generating` 下只认它）。 */
  setStreamingDoc: (content: string) => void
  setViewState: (view: ViewState) => void
  /** 双智能体的阶段标签（界面顶部那排徽标）。 */
  setPrdStage: (stage: PrdGenerationStage | null) => void
  /** PRD 的审查结论（要写进快照，刷新后仍能显示"审核通过 / 有 N 条意见"）。 */
  setPrdReviewResult: (review: JobReview | null) => void
  /** 失败横幅（与既有 `docFailure` 同形）。 */
  setDocFailure: (
    failure: { kind: DocKind; message: string; requestId?: string | null } | null,
  ) => void
  /** 截断标记（**必须收下**：截断在正文里看不出来，不显示用户会拿残文档当完整的）。 */
  setTruncated: (kind: DocKind, truncated: boolean) => void
  /** 重新生成后要撤掉"已通过"。 */
  clearApproved: (kind: DocKind) => void
  setIsGenerating: (busy: boolean) => void
  setGeneratingKind: (kind: DocKind | null) => void
}

export interface GenerationJobController {
  startPrd: (summary: Record<string, unknown>) => Promise<void>
  startApiDocs: (prdContent: string) => Promise<void>
  startPrompts: (prdContent: string, apiDocsContent: string) => Promise<void>
  /**
   * **按指令重写某一节**（F8.6「AI 优化」）。
   *
   * 与三个 `start*` 的差别（都会影响界面，务必对齐）：
   * - `viewState` **不变**（用户就停在审阅页上，切到 `generating-*` 会像是整篇在重生成）；
   * - 没有 Stepper（优化是单段流），只有一行"正在按你的反馈重写这一节…"；
   * - 流的正文是**那一节**，显示时由本 hook 拼回整篇（见 `subscribeJob` 的 optimize 分支）。
   *
   * `documentContent` 是**优化前的整篇** —— 后端收尾时把新的一节拼回它。
   * 只传该节的话后端拼不回整篇，而"把一节片段当成整篇写进快照"是数据丢失。
   */
  startOptimize: (request: OptimizeJobRequest) => Promise<void>
  /** 有没有任务在跑（本 hook 自己的判定，与页面的 `isGenerating` 同源）。 */
  isJobRunning: boolean
  /** 在跑的那条任务是不是**优化**（页面据此决定显示哪种等待态）。 */
  isOptimizingJob: boolean
  /**
   * 中止当前订阅（**不清任务**：任务在后端照跑）。
   *
   * 用途：`handleRestart`（清空重来）与组件卸载 —— 不中止的话，旧订阅的增量会写进
   * 一个已经清空的页面。
   */
  cancel: () => void
}

/** `startOptimize` 的入参：与 `DocumentReview` 的 `OptimizeRequest` 同形 + 整篇文档。 */
export interface OptimizeJobRequest {
  /** 改哪份产物（服务层口径：`prd` / `api` / `prompts`） */
  docType: JobDocType
  /** 要改的节标题（**逐字**来自正文） */
  section: string
  /** 该节的现有正文（标题 + 正文） */
  sectionContent: string
  /** 优化前的**整篇**文档 */
  documentContent: string
  /** 用户指令 */
  instruction: string
  /** 可选的生成侧上下文（修 api / prompts 时要带上 `prd_content` 之类） */
  context?: Record<string, unknown>
}

/** 跑着的任务（后端口径：`pending` 与 `running` 都算"在跑"）。 */
const runningStatuses: ReadonlySet<string> = new Set(['pending', 'running'])

/** 部分正文写进产物的节流间隔（比后端 500ms 的落库节流宽，避免两层叠在一起）。 */
const PARTIAL_WRITE_INTERVAL_MS = 1500

export function useGenerationJob(options: UseGenerationJobOptions): GenerationJobController {
  const {
    sessionId,
    activeJobId,
    onActiveJobIdChange,
    conversationMessages,
    obs,
    writeDoc,
    readDoc,
    setStreamingDoc,
    setViewState,
    setPrdStage,
    setPrdReviewResult,
    setDocFailure,
    setTruncated,
    clearApproved,
    setIsGenerating,
    setGeneratingKind,
  } = options

  const [isJobRunning, setIsJobRunning] = useState(false)
  /** 在跑的那条任务是不是优化（页面据此决定等待态：优化没有 Stepper）。 */
  const [isOptimizingJob, setIsOptimizingJob] = useState(false)
  /** 当前订阅的 AbortController。新订阅前一定 abort 旧的。 */
  const streamAbortRef = useRef<AbortController | null>(null)
  /** 已发起过重连的任务 id —— 防 StrictMode 双挂载与多次渲染重复订阅。 */
  const reconnectedRef = useRef<string | null>(null)
  /** 正在生成的那份产物（失败兜底时要知道往哪写）。 */
  const runningKindRef = useRef<DocKind | null>(null)
  /** 上一次把部分正文写进产物的时间（节流）。 */
  const lastPartialWriteRef = useRef(0)

  /**
   * 回调需要读"最新"的消息，但不能因此进依赖数组（它每轮对话都换身份，
   * 会让 `start*` 每次重建，页面拿到的就不是稳定引用了）。
   */
  const messagesRef = useRef(conversationMessages)
  messagesRef.current = conversationMessages

  const cancel = useCallback(() => {
    streamAbortRef.current?.abort()
    streamAbortRef.current = null
  }, [])

  /** 取消订阅但**不动**任务：后端那个 run 会一直跑到完成（这正是任务化的意义）。 */
  useEffect(() => () => cancel(), [cancel])

  /**
   * 把 PRD 的审查结论同时喂给两处。
   *
   * - `prdStage`：驱动界面顶部那排徽标（`prd-stage-*` 那几条），沿用既有展示；
   * - `prdReviewResult`：写进会话快照，刷新后仍能复原徽标。
   *
   * 两处都写是**有意的**：前者是展示用的派生形状，后者是持久化的事实。
   */
  const applyReview = useCallback(
    (review: JobReview, artifact: JobArtifact) => {
      if (artifact !== 'prd') return
      setPrdReviewResult(review)
      setPrdStage({
        stage: 'done',
        round: review.round ?? undefined,
        issues: review.issues,
        review_model: review.review_model ?? undefined,
        review_skipped: review.review_skipped ?? false,
      })
    },
    [setPrdReviewResult, setPrdStage],
  )

  /**
   * 按阶段恢复界面上"在干什么"（重连时用）。
   *
   * 为什么要它：刷新后重连拿到的第一个信号是 `snapshot.phase`，而步骤条是按
   * **阶段事件**推的 —— 不主动恢复一次，用户会看到 PRD 页在出字、步骤条却是空的。
   *
   * ⚠️ `running=false`（任务已收尾）时**什么都不做**：那时步骤条该是"全部完成"或干脆
   * 不显示（审核页看的是汇总面板）。这里若照旧 `begin*`，一个已完成的任务会在界面上
   * 显示成"正在分片生成…"。
   */
  const restorePhaseUI = useCallback(
    (artifact: JobArtifact, phase: string, running: boolean) => {
      if (!running) return
      if (artifact !== 'prd') {
        // 05 篇：这两份产物**也有了**"机器审查"这一步。刷新/重连时 `snapshot.phase`
        // 可能是 `reviewing` —— 那时要停在第 2 格，否则用户看到的是"在生成"（第一格）
        // 而实际进度条该在审查那一段。
        if (phase === 'reviewing') obs.beginGenerationReview(artifact)
        else if (artifact === 'api-docs') obs.beginApiDocsGeneration()
        else obs.beginPromptsGeneration()
        return
      }
      const stage: PrdGenerationStage['stage'] =
        phase === 'reviewing' ? 'reviewing' : phase === 'rewriting' ? 'rewriting' : 'writing'
      setPrdStage({ stage })
      obs.createPrdHandlers().onStage?.({
        stage,
        detail: stage === 'reviewing' ? '正在审核初稿…' : '正在撰写初稿…',
      })
    },
    [obs, setPrdStage],
  )

  /**
   * 把一份**终稿**写进产物并收尾。
   *
   * 完成路径与"重连时发现已完成"共用同一份实现，是刻意的 —— 分成两份就会出现
   * "实时跑完和刷新后跑完的界面不一样"，而那种差别只在刷新时才暴露。
   */
  const finishWithResult = useCallback(
    (
      artifact: JobArtifact,
      payload: {
        content: string
        review?: JobReview | null
        truncated?: boolean | null
      },
    ) => {
      const kind = docKindFor(artifact)
      writeDoc(kind, payload.content)
      // ⚠️ 优化**不写**截断标记：那个标记说的是"**整篇**正文被截断过"，
      // 而优化只重写一节 —— 优化成功不代表末尾补全了（与旧的优化路径同一条取舍）。
      if (!isOptimizeArtifact(artifact) && payload.truncated !== null && payload.truncated !== undefined) {
        setTruncated(kind, payload.truncated)
      }
      // 重新生成后撤掉"已通过"：内容变了，旧的通过结论不再成立
      clearApproved(kind)
      if (payload.review) applyReview(payload.review, artifact)
      setStreamingDoc('')
      setDocFailure(null)
      setViewState(reviewViewFor(artifact))
      setIsGenerating(false)
      setGeneratingKind(null)
      setIsJobRunning(false)
      setIsOptimizingJob(false)
      runningKindRef.current = null
      obs.stopTimer()
    },
    [
      applyReview,
      clearApproved,
      obs,
      setDocFailure,
      setGeneratingKind,
      setIsGenerating,
      setStreamingDoc,
      setTruncated,
      setViewState,
      writeDoc,
    ],
  )

  /** 订阅一个任务的进度，直到它收尾。**重连也走这里**（只有 jobId 的来源不同）。 */
  const subscribeJob = useCallback(
    async (jobId: string, artifact: JobArtifact) => {
      const kind = docKindFor(artifact)
      const optimize = isOptimizeArtifact(artifact)
      const controller = new AbortController()
      streamAbortRef.current?.abort()
      streamAbortRef.current = controller
      runningKindRef.current = kind
      setIsGenerating(true)
      setGeneratingKind(kind)
      setIsJobRunning(true)
      setIsOptimizingJob(optimize)
      lastPartialWriteRef.current = 0

      /** 客户端自己累积的正文。生成任务是**全文**、优化任务是**那一节**。 */
      let accumulated = ''
      /**
       * 优化专用：**优化前的整篇**。显示时用 `replaceSection` 把累积的那一节拼进去。
       *
       * 为什么不每来一个增量都往产物里写：优化还没结束，产物里那份是"优化前"的正文 ——
       * 写进去会让"刷新后看到的是整篇 + 新节"这件事失去基准（而且后端也只在自己收尾时写）。
       */
      let optimizeBase = ''
      let optimizeSection = ''

      const prdHandlers = obs.createPrdHandlers()
      const apiHandlers = obs.createApiDocsHandlers()
      const promptsHandlers = obs.createPromptsHandlers()

      /** 优化：把"已改写的那一节"拼回整篇，得到该显示什么。 */
      const optimizeDisplay = (): string => {
        if (!optimizeBase.trim() || !optimizeSection.trim()) return accumulated
        return replaceSection(optimizeBase, optimizeSection, accumulated)
      }
      /** 优化：把拼好的整篇写进产物（**只在节流点与收尾时**调）。 */
      const persistOptimize = () => {
        if (!accumulated.trim()) return
        writeDoc(kind, optimizeBase.trim() ? optimizeDisplay() : accumulated)
      }

      try {
        await readJobStream(
          jobId,
          {
            onSnapshot: (snap: JobSnapshotEvent) => {
              if (optimize) {
                // 优化：`draft_content` 是**那一节**的进度（不是整篇），
                // `section` 告诉我们拼哪一节，`readDoc` 给出"优化前的整篇"基准。
                accumulated = snap.draft_content ?? ''
                optimizeSection = snap.section ?? ''
                optimizeBase = readDoc(kind)
                setStreamingDoc(optimizeDisplay())
                return
              }
              // ⚠️ 生成：快照就是**已有全文**，直接整篇显示，**不重播打字机**
              accumulated = snap.draft_content ?? ''
              writeDoc(kind, accumulated)
              setStreamingDoc(accumulated)
              restorePhaseUI(artifact, snap.phase, runningStatuses.has(snap.status))
              if (snap.review) applyReview(snap.review, artifact)
            },
            onDelta: (chunk: string) => {
              accumulated += chunk
              setStreamingDoc(optimize ? optimizeDisplay() : accumulated)
              // 每隔一会儿把"已收到的部分正文"写进产物：刷新/断电时不至于一个字不剩
              // （与对话侧同一条理由，见 `App.tsx` 里那段 1.5 秒节流的说明）。
              const stamp = Date.now()
              if (stamp - lastPartialWriteRef.current > PARTIAL_WRITE_INTERVAL_MS) {
                lastPartialWriteRef.current = stamp
                if (optimize) persistOptimize()
                else writeDoc(kind, accumulated)
              }
            },
            onPhase: (event) => {
              // ---------- 05 篇：接口文档 / 提示词也有"机器审查"这一段了 ----------
              // 后端在生成完之后会再调一次模型做审查，并先发 `review_started`
              // （见 `job_runner._review_generated_document`）。不处理它的话，
              // 用户在那一二十秒里看到的是一个**完全没动静**的步骤条（第一格已打勾、
              // 第二格永远不亮）—— 正是 `generationSteps` 里那个"曾经有过又被删掉"的
              // 第二步要避免的观感。
              if (artifact !== 'prd') {
                if (event.phase === 'review_started') {
                  obs.beginGenerationReview(artifact)
                }
                return
              }
              // ⚠️ `rewrite_started` = 服务端**已把草稿清空**、从零写下一稿。
              // 客户端的累积必须跟着清 —— 不清就会把两稿拼在一起，而拼起来的文档
              // 看起来是完整的（没有语法错误，只是前后矛盾）。v1 并没有丢：
              // 后端把它留在 `previous_draft` 里。
              if (event.phase === 'rewrite_started') {
                accumulated = ''
                setStreamingDoc('')
              }
              const stage: PrdGenerationStage = {
                stage:
                  event.phase === 'review_started'
                    ? 'reviewing'
                    : event.phase === 'rewrite_started'
                      ? 'rewriting'
                      : 'writing',
                round: event.round ?? undefined,
                detail: event.detail,
                issues: event.issues,
              }
              setPrdStage(stage)
              prdHandlers.onStage?.(stage)
            },
            onReview: (review) => applyReview(review, artifact),
            onRunSummary: (summary: RunSummary) => {
              // ⚠️ **认领这一趟的 run id**：Job 的 `run_summary.run_id` 是**后端**生成的
              // （POST /jobs 那个请求的工号，被后台任务继承），前端不可能提前知道。
              // 而 `handleRunSummary` 会按 `run_id` 过滤（"这一帧是不是我这一趟的"）——
              // 不先认领，整份汇总会被自己丢掉，界面上永远看不到"本次生成"的数字。
              // （前台流式那套的 run id 是客户端 `newRunId()` 生成的，所以那边不需要这一步。）
              //
              // ⚠️ 优化也走这里：它的 `run_type` 是 `optimize_document`，而
              // `docRunSummary` 的兜底匹配表（按 run_type）里**没有**它 ——
              // 认领 run id 是让这份汇总能显示出来的**唯一**途径。
              if (summary.run_id) obs.startRun(summary.run_id)
              if (optimize) {
                obs.createOptimizeHandlers().onRunSummary?.(summary)
                return
              }
              if (artifact === 'prd') prdHandlers.onRunSummary?.(summary)
              else if (artifact === 'api-docs') apiHandlers.onRunSummary?.(summary)
              else promptsHandlers.onRunSummary?.(summary)
            },
            onDone: (event: JobDoneEvent) => {
              // 优化：`content` 是**后端拼好的整篇**（不是那一节）—— 直接落库，
              // 不在客户端再拼一次（两份拼接实现必然分叉，而后端那份还会写进快照）。
              const content = event.content || event.final_prd || accumulated
              const meta = { chunks: 0, chars: [...content].length }
              if (artifact === 'api-docs') apiHandlers.onDone?.(meta, content)
              if (artifact === 'prompts') promptsHandlers.onDone?.(meta, content)
              onActiveJobIdChange(null)
              finishWithResult(artifact, {
                content,
                review: event.review,
                truncated: event.truncated,
              })
            },
          },
          controller.signal,
        )
      } catch (error) {
        if (controller.signal.aborted) return // 被掐掉的不算失败
        const { message, requestId } = obs.captureStreamError(error)
        if (optimize) {
          // 优化失败：**把已经收到的那一节拼回整篇**再落库，然后照常报错。
          // 不写的话用户等了一分钟什么都没留下；写"那一节"的话整篇会被换成一段话
          // （后端也是这么处理的，两边必须一致，否则刷新前后是两份文档）。
          persistOptimize()
        }
        // ⚠️ 生成路径**不把半截内容写进产物**：一次失败的重生成会把上一版好文档顶掉。
        // 界面上仍显示上一版 + 失败原因（`DocumentReview` 的 failed 分支）。
        setDocFailure({ kind, message, requestId })
        setStreamingDoc('')
        setViewState(reviewViewFor(artifact))
        setIsGenerating(false)
        setGeneratingKind(null)
        setIsJobRunning(false)
        setIsOptimizingJob(false)
        obs.stopTimer()
        // 失败也清 `activeJobId`：任务已经结束（后端会把它置 failed），留着它只会在
        // 下次刷新时对同一个失败任务再重连一次。
        onActiveJobIdChange(null)
      } finally {
        if (streamAbortRef.current === controller) streamAbortRef.current = null
      }
    },
    [
      applyReview,
      finishWithResult,
      obs,
      onActiveJobIdChange,
      readDoc,
      restorePhaseUI,
      setDocFailure,
      setGeneratingKind,
      setIsGenerating,
      setPrdStage,
      setStreamingDoc,
      setViewState,
      writeDoc,
    ],
  )

  /** 三个 `start*` 的共同部分：开跑前的清理 + 观测就位 + 创建任务 + 订阅。
   *  ⚠️ 只服务**整篇生成**（优化有它自己的 `startOptimize`：不切视图、没有 Stepper）。 */
  const startJob = useCallback(
    async (
      artifact: GenerateJobArtifact,
      buildPayload: (artifact: GenerateJobArtifact) => Record<string, unknown>,
    ): Promise<void> => {
      if (!sessionId) {
        setDocFailure({
          kind: docKindFor(artifact),
          message: '还没有会话 id —— 方案还没存上服务端，生成任务无法创建。请检查网络后重试。',
        })
        return
      }
      const kind = docKindFor(artifact)
      cancel()
      setDocFailure(null)
      setStreamingDoc('')
      setViewState(generatingViewFor(artifact))
      setIsGenerating(true)
      setGeneratingKind(kind)
      setIsJobRunning(true)
      runningKindRef.current = kind
      // 观测：清上一趟 + 把秒表归零。⚠️ 顺序不能反（`resetGeneration` 会清空步骤条）。
      // ⚠️ 这里**不调 `startRun`**：Job 的 run id 由后端生成，在 `onRunSummary` 里认领
      // （提前编一个只会让真正的汇总被 `handleRunSummary` 按 run_id 过滤掉）。
      obs.resetGeneration()
      obs.resetErrorMeta()
      if (artifact === 'prd') {
        obs.beginPrdGeneration()
        setPrdStage(null)
        setPrdReviewResult(null)
      } else if (artifact === 'api-docs') obs.beginApiDocsGeneration()
      else obs.beginPromptsGeneration()
      obs.startTimer()

      try {
        const { job_id } = await createJob(sessionId, artifact, buildPayload(artifact))
        onActiveJobIdChange(job_id)
        await subscribeJob(job_id, artifact)
      } catch (error) {
        // 创建就失败（409 已有任务在跑 / 422 payload / 503 缺 Key / 网络）：
        // **不静默**，且把后端那句人话文案原样带给用户。
        const isConflict = error instanceof JobApiError && error.status === 409
        const message = isConflict
          ? `${ARTIFACT_LABEL[artifact]}已经有一个任务在跑了 —— 等它结束，或刷新页面接上它。`
          : obs.captureStreamError(error).message
        setDocFailure({ kind, message })
        setViewState(reviewViewFor(artifact))
        setIsGenerating(false)
        setGeneratingKind(null)
        setIsJobRunning(false)
        setIsOptimizingJob(false)
        obs.stopTimer()
      }
    },
    [
      cancel,
      obs,
      onActiveJobIdChange,
      sessionId,
      setDocFailure,
      setGeneratingKind,
      setIsGenerating,
      setPrdReviewResult,
      setPrdStage,
      setStreamingDoc,
      setViewState,
      subscribeJob,
    ],
  )

  /** 历史消息每次都从 ref 取**最新**的（回调不该因为多聊了一轮就重建）。 */
  const history = useCallback(
    () => messagesRef.current.map((message) => ({ role: message.role, content: message.content })),
    [],
  )

  /**
   * 生成 PRD。`summary` 是技能包 8 字段的结构化摘要 —— 任务接口**只收这一种输入**
   * （表单版的 `generate-prd-stream` 是另一条前台链路，没有 Job 版本）。
   */
  const startPrd = useCallback(
    async (summary: Record<string, unknown>) => {
      await startJob('prd', () => ({
        requirements_summary: summary,
        conversation_messages: history(),
      }))
    },
    [history, startJob],
  )

  /** 从**已通过审核的** PRD 推导接口文档。 */
  const startApiDocs = useCallback(
    async (prdContent: string) => {
      await startJob('api-docs', () => ({
        prd_content: prdContent,
        conversation_messages: history(),
      }))
    },
    [history, startJob],
  )

  /** 生成提示词套件（消费 PRD + 接口文档）。 */
  const startPrompts = useCallback(
    async (prdContent: string, apiDocsContent: string) => {
      await startJob('prompts', () => ({
        prd_content: prdContent,
        // 空串不传：后端对"不传"填「（尚无）」，传空串会把提示词里那一格变成空
        ...(apiDocsContent.trim() ? { api_docs_content: apiDocsContent } : {}),
        conversation_messages: history(),
      }))
    },
    [history, startJob],
  )

  /**
   * **按指令重写某一节**（F8.6）。与 `startJob` 的三处刻意不同，见 controller 上的说明。
   *
   * ⚠️ **不切 `viewState`**：用户就停在审阅页上看这一节被改写；切到 `generating-*`
   * 会让人以为整篇在重生成（而且收尾还要切回来，界面会闪一下）。
   *
   * ⚠️ 观测用 `beginOptimizeGeneration()`（单行 hint、无 Stepper），与前台优化那条路一致 ——
   * 优化是**单段流**，没有 PRD 的"起草 / 审查 / 改写"三个阶段，硬show一个三步条是骗人。
   */
  const startOptimize = useCallback(
    async (request: OptimizeJobRequest) => {
      const artifact = optimizeArtifactForDocType(request.docType)
      const kind = docKindFor(artifact)
      if (!sessionId) {
        setDocFailure({
          kind,
          message: '还没有会话 id —— 方案还没存上服务端，优化任务无法创建。请检查网络后重试。',
        })
        return
      }
      if (!request.sectionContent.trim()) {
        setDocFailure({ kind, message: '这一节还没有正文，没什么可优化的。' })
        return
      }

      cancel()
      setDocFailure(null)
      setStreamingDoc('')
      // ⚠️ **viewState 一行都不动** —— 见上面的说明
      setIsGenerating(true)
      setGeneratingKind(kind)
      setIsJobRunning(true)
      setIsOptimizingJob(true)
      runningKindRef.current = kind
      obs.resetGeneration()
      obs.resetErrorMeta()
      obs.beginOptimizeGeneration()
      obs.startTimer()

      try {
        const { job_id } = await createJob(sessionId, artifact, {
          doc_type: request.docType,
          section: request.section,
          current_content: request.sectionContent,
          document_content: request.documentContent,
          instruction: request.instruction,
          ...(request.context ? { context: request.context } : {}),
        })
        onActiveJobIdChange(job_id)
        await subscribeJob(job_id, artifact)
      } catch (error) {
        // 409（同一份文档正在生成/优化）在后端是**常态**，文案要直说"谁在挡着"
        const isConflict = error instanceof JobApiError && error.status === 409
        const message = isConflict
          ? `${ARTIFACT_LABEL[artifact]}没法开始：同一份文档已经有一个任务在跑 —— 等它结束再试。`
          : obs.captureStreamError(error).message
        setDocFailure({ kind, message })
        setIsGenerating(false)
        setGeneratingKind(null)
        setIsJobRunning(false)
        setIsOptimizingJob(false)
        obs.stopTimer()
      }
    },
    [
      cancel,
      obs,
      onActiveJobIdChange,
      sessionId,
      setDocFailure,
      setGeneratingKind,
      setIsGenerating,
      setStreamingDoc,
      subscribeJob,
    ],
  )

  /**
   * **刷新重连**：快照里带着 `activeJobId` 就自动接上。
   *
   * 三种情况必须分开处理（这也是它不能只看"有没有 id"的原因）：
   *
   * | 任务状态 | 怎么做 |
   * | --- | --- |
   * | `pending` / `running` | 订阅 —— 首帧 `snapshot` 会把已有全文交出来 |
   * | `completed` | **不订阅**（没有新事件了），直接按 `result` 收尾 |
   * | `failed` | 不订阅，把错误显示出来 + 把草稿留在产物里 |
   *
   * ⚠️ 触发条件是**`activeJobId` 有值**，不是"页面 mounted"：id 只有在会话数据
   * 读回来之后才知道（本地恢复或 V2 的 `loadSession`），所以这个顺序天然是对的 ——
   * 不需要额外的"加载完成"标志，也就不会出现"先订阅、后拿到 id"的竞态。
   *
   * ⚠️ 也**不要**对 running 的任务再发 `POST /api/jobs`：那会 409（同产物只允许一个）。
   */
  useEffect(() => {
    if (!activeJobId) {
      reconnectedRef.current = null
      return
    }
    if (!sessionId) return
    if (reconnectedRef.current === activeJobId) return
    reconnectedRef.current = activeJobId

    let cancelled = false
    void (async () => {
      let snapshot: JobSnapshot
      try {
        snapshot = await getJob(activeJobId)
      } catch (error) {
        if (cancelled) return
        // 任务查不到（比如库被清了）：不要卡在生成中，落回审核页并说清
        const kind = runningKindRef.current
        setDocFailure({ kind: kind ?? 'prd', message: obs.captureStreamError(error).message })
        setViewState(reviewViewFor(kind ? artifactOfKind(kind) : 'prd'))
        onActiveJobIdChange(null)
        return
      }
      if (cancelled) return

      const artifact = snapshot.artifact
      const optimize = isOptimizeArtifact(artifact)
      if (runningStatuses.has(snapshot.status)) {
        // 任务还在跑：界面先摆到该在的那一屏，再订阅（首帧 snapshot 会给正文）
        //
        // ⚠️ 优化**不切 `generating-*`**：它全程停在 `review-*`。走 `generatingViewFor`
        // 的话刷新后会落到"整篇生成中"，而正文里只有一节 —— 用户会以为整篇被替换了。
        setViewState(reviewViewFor(artifact))
        if (optimize) {
          // 优化：正文要**拼**。订阅的首帧会给"那一节的进度"，这里先按会话里的整篇
          // 把界面摆到"优化前"的样子，随后 `onSnapshot` 会把已改的部分拼上去。
          setIsGenerating(true)
          setGeneratingKind(docKindFor(artifact))
          obs.beginOptimizeGeneration()
          obs.startTimer()
        } else if (snapshot.draft_content) {
          writeDoc(docKindFor(artifact), snapshot.draft_content)
          setStreamingDoc(snapshot.draft_content)
        }
        if (snapshot.review) applyReview(snapshot.review, artifact)
        await subscribeJob(activeJobId, artifact)
        return
      }

      // 已收尾：按结论落库（**不订阅** —— 队列上不会再有新事件）
      if (snapshot.status === 'completed') {
        const result = snapshot.result ?? {}
        finishWithResult(artifact, {
          content: result.content ?? snapshot.draft_content,
          review: result.review ?? snapshot.review,
          truncated: result.truncated,
        })
      } else {
        const kind = docKindFor(artifact)
        if (optimize) {
          // 优化失败：草稿是"那一节"的片段，**不能**直接写进产物（那会把整篇换成一段话）。
          // 能拼就拼回整篇（与后端 `sync_optimize_failed` 的落库口径一致），拼不了就一个字不动。
          const section = snapshot.section ?? ''
          const base = readDoc(kind)
          if (snapshot.draft_content.trim() && section.trim() && base.trim()) {
            writeDoc(kind, replaceSection(base, section, snapshot.draft_content))
          }
        } else if (snapshot.draft_content) {
          writeDoc(kind, snapshot.draft_content)
        }
        setDocFailure({
          kind,
          message: snapshot.error || `${ARTIFACT_LABEL[artifact]}任务已结束，但没有结果。`,
        })
        setViewState(reviewViewFor(artifact))
        setIsGenerating(false)
        setGeneratingKind(null)
        setIsJobRunning(false)
        setIsOptimizingJob(false)
      }
      onActiveJobIdChange(null)
    })()

    return () => {
      cancelled = true
    }
  }, [
    activeJobId,
    sessionId,
    applyReview,
    finishWithResult,
    obs,
    onActiveJobIdChange,
    readDoc,
    setDocFailure,
    setGeneratingKind,
    setIsGenerating,
    setStreamingDoc,
    setViewState,
    subscribeJob,
    writeDoc,
  ])

  return { startPrd, startApiDocs, startPrompts, startOptimize, isJobRunning, isOptimizingJob, cancel }
}

/** `DocKind` → `JobArtifact`（`jobViews` 里那张表是反方向；这里只有失败兜底用得上）。 */
function artifactOfKind(kind: DocKind): JobArtifact {
  return kind === 'api' ? 'api-docs' : kind
}
