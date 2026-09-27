/**
 * 生成观测：状态、计时器、SSE handler 工厂。
 *
 * ## 为什么要一个 hook
 *
 * 观测牵扯 state（步骤、耗时、hint、summary、error 的 request_id）与一个 timer，
 * 还要为四个生成流程各生成一份 handler。堆在页面里的话，`App.tsx` 会多出上百行
 * "与业务流程无关"的代码，而且每加一个流程就再抄一遍。
 *
 * ## 分层（与规格一致）
 *
 * | 谁 | 管什么 |
 * | --- | --- |
 * | `utils/generationSteps` | 步骤长什么样、phase 怎么映射 |
 * | `utils/streamErrors` | 错误文案、request_id 提取 |
 * | **本 hook** | 观测 state 怎么变（含 timer） |
 * | 组件 | 纯展示 |
 * | `App.tsx` | 何时 begin/stop、切 viewState、调 API |
 */

import { useCallback, useRef, useState } from 'react'

import type { StreamHandlers } from '../services/api'
import type { ContextUsage, RunSummary } from '../types'
import {
  REVIEW_STEP_HINT,
  createApiDocsGeneratingSteps,
  createPromptsGeneratingSteps,
  finalizePrdStepsFromSummary,
  getStepFailureLabel,
  mapPrdPhaseToSteps,
  markApiDocsStepsDone,
  markPromptsStepDone,
  type StepItem,
} from '../utils/generationSteps'
import { formatGenerationFailure, getErrorMessage, getErrorRequestId } from '../utils/streamErrors'

/** 澄清每一轮的上下文占用（用于折叠面板按轮展示）。 */
export interface ClarificationRound {
  index: number
  usage: ContextUsage
}

export interface GenerationObservability {
  runSummary: RunSummary | null
  contextUsage: ContextUsage | null
  generationSteps: StepItem[]
  generationElapsed: number
  generationHint: string | undefined
  errorRequestId: string | null
  clarificationRounds: ClarificationRound[]
  /**
   * **本次生成**用的 run id（`null` = 还没开始过一趟产物生成）。
   *
   * 用途有两个，缺一不可：
   * 1. 编排层据此把 `run_summary` 归到**这一趟**（取代原先只看 `run_type` 的归属判定）；
   * 2. 每一片请求的 `X-Run-ID` 头也用它 —— 两边同一个来源才不会有"请求带 A、面板等 B"。
   */
  activeRunId: string | null

  resetGeneration: () => void
  resetErrorMeta: () => void
  /** 清掉澄清那一份观测（上下文占用 + 分轮列表）。**不要并进 `resetGeneration`** ——
   *  后者每趟产物生成都要调，并在一起会让"刚聊完的上下文统计"在生成 PRD 时消失。 */
  resetClarification: () => void
  /**
   * 开一趟新的 run。
   *
   * ⚠️ **必须在发第一个请求之前调**：`activeRunId` 是 `handleRunSummary` 的归属依据，
   * 而归属判定用的是 ref（同一个 tick 里立刻可见），所以这一句之后到达的汇总
   * 一律按新 run 认领 —— 上一趟的迟到帧（比如被中断的那一趟）不会串进来。
   */
  startRun: (runId: string) => void
  startTimer: () => void
  stopTimer: () => void
  clearSteps: () => void

  beginPrdGeneration: () => void
  beginApiDocsGeneration: () => void
  beginPromptsGeneration: () => void
  /** AI 优化（单请求，`part 1/1`）：步骤条与三个 `begin*` 不同，但汇总/失败要同一套。 */
  beginOptimizeGeneration: () => void

  handleContextUsage: (usage: ContextUsage) => void
  handleRunSummary: (summary: RunSummary, options?: { finalizeSteps?: (s: RunSummary) => StepItem[] }) => void
  captureStreamError: (error: unknown, options?: { fallback?: string; elapsedSeconds?: number }) => {
    message: string
    requestId: string | null
  }
  copyErrorRequestId: () => void

  createPrdHandlers: () => Pick<StreamHandlers, 'onStage' | 'onRunSummary'>
  createApiDocsHandlers: () => Pick<StreamHandlers, 'onRunSummary' | 'onDone'>
  createPromptsHandlers: () => Pick<StreamHandlers, 'onRunSummary' | 'onDone'>
  createOptimizeHandlers: () => Pick<StreamHandlers, 'onRunSummary'>
  createClarificationHandlers: () => Pick<StreamHandlers, 'onContextUsage' | 'onRunSummary'>
}

export function useGenerationObservability(): GenerationObservability {
  const [runSummary, setRunSummary] = useState<RunSummary | null>(null)
  const [contextUsage, setContextUsage] = useState<ContextUsage | null>(null)
  const [generationSteps, setGenerationSteps] = useState<StepItem[]>([])
  const [generationElapsed, setGenerationElapsed] = useState(0)
  const [generationHint, setGenerationHint] = useState<string | undefined>(undefined)
  const [errorRequestId, setErrorRequestId] = useState<string | null>(null)
  const [clarificationRounds, setClarificationRounds] = useState<ClarificationRound[]>([])
  const [activeRunId, setActiveRunId] = useState<string | null>(null)

  const timerRef = useRef<number | null>(null)
  const startedAtRef = useRef(0)
  /**
   * `activeRunId` 的 ref 镜像。
   *
   * 为什么不能只用 state：`run_summary` 的归属判定发生在 **SSE 回调**里，
   * 而回调可能是好几个 tick 之前建好的闭包 —— 闭包读 state 只会读到**当时**那个值
   * （分片生成时就是上一趟、或者 `null`），于是这一趟的汇总会被自己丢掉。
   * ref 是同一个格位、立刻可见，归属判定才跟得上"这一片属于刚开的这趟 run"。
   */
  const activeRunIdRef = useRef<string | null>(null)

  const stopTimer = useCallback(() => {
    if (timerRef.current !== null) {
      window.clearInterval(timerRef.current)
      timerRef.current = null
    }
  }, [])

  const startTimer = useCallback(() => {
    // 幂等：重复 begin 时先把上一个 timer 收掉，否则两个 timer 会互相盖住秒数
    if (timerRef.current !== null) {
      window.clearInterval(timerRef.current)
      timerRef.current = null
    }
    startedAtRef.current = Date.now()
    setGenerationElapsed(0)
    timerRef.current = window.setInterval(() => {
      setGenerationElapsed(Math.floor((Date.now() - startedAtRef.current) / 1000))
    }, 1000)
  }, [])

  const resetGeneration = useCallback(() => {
    setRunSummary(null)
    setGenerationSteps([])
    setGenerationHint(undefined)
  }, [])

  const resetErrorMeta = useCallback(() => setErrorRequestId(null), [])
  const clearSteps = useCallback(() => setGenerationSteps([]), [])

  /**
   * 开一趟新 run。
   *
   * ⚠️ **不清 `runSummary`** —— 清的理由由调用方掌握（现在是 `resetGeneration()` 清，
   * 它在 `startRun` 之前调）。两件事混在一起的话，将来只想换个 run id 的地方
   * （比如重试同一趟）会顺手把上一条汇总抹掉。
   */
  const startRun = useCallback((runId: string) => {
    activeRunIdRef.current = runId
    setActiveRunId(runId)
  }, [])

  /**
   * 清掉澄清那一份观测。
   *
   * 存在的理由：`contextUsage` / `clarificationRounds` 属于**这一次澄清会话**，
   * "重新开始"或开新一轮对话时必须归零 —— 否则新一轮的 `handleContextUsage`
   * 会在旧列表后面接着追加，界面上冒出"第 4 轮"而实际只聊过 1 轮。
   */
  const resetClarification = useCallback(() => {
    setContextUsage(null)
    setClarificationRounds([])
  }, [])

  const beginPrdGeneration = useCallback(() => {
    setGenerationSteps(mapPrdPhaseToSteps('writing'))
    setGenerationHint(undefined)
  }, [])
  const beginApiDocsGeneration = useCallback(() => {
    setGenerationSteps(createApiDocsGeneratingSteps())
    setGenerationHint('正在分片生成接口文档，可能需要一两分钟…')
  }, [])
  const beginPromptsGeneration = useCallback(() => {
    setGenerationSteps(createPromptsGeneratingSteps())
    setGenerationHint('正在分片生成提示词套件，可能需要一两分钟…')
  }, [])
  /** AI 优化：单请求（后端按 `1/1` 记账），所以只有一行"正在重写这一节"。 */
  const beginOptimizeGeneration = useCallback(() => {
    setGenerationSteps([])
    setGenerationHint('正在按你的反馈重写这一节…')
  }, [])

  const handleContextUsage = useCallback((usage: ContextUsage) => {
    setContextUsage(usage)
    setClarificationRounds((prev) => [...prev, { index: prev.length + 1, usage }])
  }, [])

  const handleRunSummary = useCallback(
    (summary: RunSummary, options?: { finalizeSteps?: (s: RunSummary) => StepItem[] }) => {
      /**
       * **按 run id 归属**：分片生成时每一片都发一帧 `run_summary`，同一 run 的**后到者覆盖前者
       * 是预期的** —— 后端把整份合计放进后到的那一帧里（第 1 片的帧只算第 1 片，
       * 最后一片的帧才是整份）。前端**不做相加**：耗时是墙钟、且有并发，相加必错。
       *
       * 凭什么要拦：`runSummary` 是一份共享 state，被中断的上一趟、以及澄清流的帧都会写它；
       * 不按 run id 拦的话，串进来的数字说的是别的事，而界面上一模一样。
       *
       * ⚠️ `run_id` 缺（老后端 / 后端回滚）时**放行**，退回上一版行为（宁可显示一份可能过期的汇总，
       * 也不要整个面板消失）。
       */
      if (summary.run_id && summary.run_id !== activeRunIdRef.current) return
      setRunSummary(summary)
      if (options?.finalizeSteps) setGenerationSteps(options.finalizeSteps(summary))
    },
    [],
  )

  const captureStreamError = useCallback(
    (error: unknown, options?: { fallback?: string; elapsedSeconds?: number }) => {
      const requestId = getErrorRequestId(error)
      setErrorRequestId(requestId)
      const label = getStepFailureLabel(generationSteps)
      const message = generationSteps.length
        ? formatGenerationFailure(label, options?.elapsedSeconds ?? generationElapsed, error)
        : getErrorMessage(error, options?.fallback ?? '生成失败')
      return { message, requestId }
    },
    [generationElapsed, generationSteps],
  )

  const copyErrorRequestId = useCallback(() => {
    if (!errorRequestId) return
    void navigator.clipboard?.writeText(errorRequestId)
  }, [errorRequestId])

  const createPrdHandlers = useCallback(
    (): Pick<StreamHandlers, 'onStage' | 'onRunSummary'> => ({
      onStage: (stage) => {
        setGenerationSteps(mapPrdPhaseToSteps(stage.stage))
        // 审查阶段没有正文输出：不解释用户会以为卡死（实测踩过）
        setGenerationHint(stage.stage === 'reviewing' ? REVIEW_STEP_HINT : stage.detail)
      },
      // ⚠️ 终态只能靠 run_summary 收束：审查通过时后端**不发** rewriting，
      // 只看 phase 的话第三步会永远停在"待办"
      onRunSummary: (summary) => handleRunSummary(summary, { finalizeSteps: finalizePrdStepsFromSummary }),
    }),
    [handleRunSummary],
  )

  const createApiDocsHandlers = useCallback(
    (): Pick<StreamHandlers, 'onRunSummary' | 'onDone'> => ({
      // 接口文档没有"机器审查"这一步（后端只对 PRD 做双智能体），所以 done 即两步结束
      onDone: () => setGenerationSteps(markApiDocsStepsDone()),
      onRunSummary: (summary) => handleRunSummary(summary),
    }),
    [handleRunSummary],
  )

  const createPromptsHandlers = useCallback(
    (): Pick<StreamHandlers, 'onRunSummary' | 'onDone'> => ({
      onDone: () => setGenerationSteps(markPromptsStepDone()),
      onRunSummary: (summary) => handleRunSummary(summary),
    }),
    [handleRunSummary],
  )

  /**
   * AI 优化（单请求）。
   *
   * 它跟三份产物一样会发 `run_summary`，所以**必须接同一套观测** ——
   * 原先没接，导致优化这条路上既没有汇总（用户看不到这一节花了多少）、
   * 失败了也没有 request_id 可复制，只能报一句"失败了"。
   */
  const createOptimizeHandlers = useCallback(
    (): Pick<StreamHandlers, 'onRunSummary'> => ({
      onRunSummary: (summary) => handleRunSummary(summary),
    }),
    [handleRunSummary],
  )

  const createClarificationHandlers = useCallback(
    (): Pick<StreamHandlers, 'onContextUsage' | 'onRunSummary'> => ({
      onContextUsage: handleContextUsage,
      // ⚠️ 澄清**刻意不走** `handleRunSummary` 的 run 归属：它不带 `X-Run-ID`（不是产物、
      // 也不分片），所以后端回给它的帧里没有 `run_id`，按 run 拦只会把澄清自己的账也拦掉。
      // 它照旧直接写 —— 产物那一趟开始时 `resetGeneration()` 会清掉，不会串到产物面板上。
      onRunSummary: (summary) => setRunSummary(summary),
    }),
    [handleContextUsage],
  )

  return {
    runSummary,
    contextUsage,
    generationSteps,
    generationElapsed,
    generationHint,
    errorRequestId,
    clarificationRounds,
    activeRunId,
    resetGeneration,
    resetErrorMeta,
    resetClarification,
    startRun,
    startTimer,
    stopTimer,
    clearSteps,
    beginPrdGeneration,
    beginApiDocsGeneration,
    beginPromptsGeneration,
    beginOptimizeGeneration,
    handleContextUsage,
    handleRunSummary,
    captureStreamError,
    copyErrorRequestId,
    createPrdHandlers,
    createApiDocsHandlers,
    createPromptsHandlers,
    createOptimizeHandlers,
    createClarificationHandlers,
  }
}
