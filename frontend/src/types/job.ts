/**
 * Generation Job 的前端类型 —— 与后端 `api/jobs.py` 的契约一一对应。
 *
 * ## 为什么单独一个文件
 *
 * Job 的类型被三层共用：`services/jobApi`（HTTP）、`utils/jobStream`（SSE）、
 * `hooks/useGenerationJob`（编排）。放在任意一层里都会让另外两层反向依赖它
 * —— 而这三层的关系是"并列的传输/解析/编排"，谁也不该是别人的上游。
 *
 * ## 命名口径
 *
 * 与后端**逐字对齐**（`artifact` 取值、`draft_content` 这种 snake_case 字段名）。
 * 刻意不做 camelCase 转换：Job 的载荷是**协议**，转一层就多一处"哪边改了没跟着改"
 * 的漂移点，而它不像 `GENERATE_*_REQUEST` 那样要拼进业务对象。
 */

import type { RunSummary } from '.'
import type { PrdGenerationStage } from '../services/api'

/** **整篇生成**的三种产物。 */
export type GenerateJobArtifact = 'prd' | 'api-docs' | 'prompts'

/** **按指令优化一节**的三种产物（F8.6）。与上面三者一一对应。 */
export type OptimizeJobArtifact = 'optimize-prd' | 'optimize-api-docs' | 'optimize-prompts'

/** 用户点的动作：整篇生成，或按指令优化某一节。后端 `job_models.JOB_ARTIFACTS` 同值。 */
export type JobArtifact = GenerateJobArtifact | OptimizeJobArtifact

/** 优化任务的 `doc_type`（= 后端 `services/state.py` 的 `DocKind`）。 */
export type JobDocType = 'prd' | 'api' | 'prompts'

/** 任务状态。`pending` 与 `running` 都算"在跑"（后端也是这么判重的）。 */
export type JobStatus = 'pending' | 'running' | 'completed' | 'failed' | 'cancelled'

/**
 * 任务当前在哪个阶段。
 *
 * - `prd`：`writing` → `reviewing` →（有问题则）`rewriting` → … → `done`
 * - `api-docs` / `prompts`：创建即 `generating`，完成时 `done`
 */
export type JobPhase = 'writing' | 'reviewing' | 'rewriting' | 'generating' | 'done'

/** SSE `phase` 事件里的**对外名字**（与 `JobPhase` 不是一套，见后端 `job_runner.PHASE_MAP`）。 */
export type JobPhaseEventName = 'writer_started' | 'review_started' | 'rewrite_started' | 'done'

/** 一条审查意见。形状直接取既有的 `PrdGenerationStage.issues` 元素 —— 不另写一份。 */
export type JobReviewIssue = NonNullable<PrdGenerationStage['issues']>[number]

/**
 * 审查结论（后端 `review_json` / SSE 的 `review` 事件）。
 *
 * ⚠️ `review_skipped=true` 时 `passed` **仍是 `true`**：那是"审核没跑成、按通过处理"，
 * 界面必须把这个区别显示出来（否则"通过"看起来像真的审过了）。
 */
export interface JobReview {
  passed: boolean
  issues: JobReviewIssue[]
  /**
   * 一句话总结（05 篇：接口文档 / 提示词的审查输出里带）。
   *
   * ⚠️ PRD 那一条**没有**这个字段（它的提示词输出 `{ok, issues}`）—— 所以是可选的，
   * 界面读不到就不显示那一行。
   */
  summary?: string | null
  /** 审到第几稿 */
  round?: number | null
  /** 这一稿是哪个模型审的（服务端给，便于发现"第二双眼睛"静默回落） */
  review_model?: string | null
  /** 审核输出解析不了、本次按通过处理 */
  review_skipped?: boolean
}

/** 收尾结果（后端 `result_json`）。失败时只有 `content` 与 `run_summary`。 */
export interface JobResult {
  /** 生成：产物**全文**；优化：把改过的那一节**拼回整篇**之后的结果 */
  content?: string
  review?: JobReview | null
  revision_applied?: boolean | null
  /** 是否被模型输出上限截断（分片路径下是"任一片被截断"） */
  truncated?: boolean | null
  finish_reason?: string | null
  run_summary?: RunSummary
  /** 优化任务改的是哪一节（逐字标题行）。生成任务没有这个键 */
  section?: string | null
}

/**
 * 优化的公共载荷字段（`POST /api/jobs` 的 `payload`，artifact 为 `optimize-*`）。
 *
 * ⚠️ 四项缺一不可，且 `currentContent` 与 `documentContent` **不是一回事**：
 * 前者是**那一节**的现有正文（服务层要它才能保留用户手改的内容），
 * 后者是**优化前的整篇**（后端收尾时把新的一节拼回它）。
 * 只传前者的话，后端拼不回整篇 —— 而"把一节片段当成整篇存进快照"是数据丢失。
 */
export interface OptimizeJobPayload {
  doc_type: JobDocType
  /** 要改的节标题（逐字来自正文） */
  section: string
  /** 该节的现有正文（标题 + 正文） */
  current_content: string
  /** 优化前的整篇文档 */
  document_content: string
  /** 用户指令（= 服务层的 `feedback`） */
  instruction: string
  /** 可选的生成侧上下文（`prd_content` / `known_info` / `outline` …） */
  context?: Record<string, unknown>
}

/** `GET /api/jobs/{id}` 的响应。 */
export interface JobSnapshot {
  id: string
  session_id: string
  artifact: JobArtifact
  status: JobStatus
  phase: JobPhase
  /**
   * **权威草稿**：刷新时先用它把正文填上，页面不要留白。
   *
   * ⚠️ **含义随 artifact 变**：生成任务是"产物全文的增量"，优化任务是
   * "模型这一节写了多少"（**不是整篇**）。优化要靠 `section` + 会话里的整篇自己拼。
   */
  draft_content: string
  review?: JobReview | null
  result?: JobResult | null
  error?: string | null
  /** 优化任务改的是哪份产物（`prd` / `api` / `prompts`）；生成任务为 `null` */
  doc_type?: JobDocType | null
  /** 优化任务在改哪一节（逐字标题行）；生成任务为 `null` */
  section?: string | null
}

/** SSE 首帧 `snapshot` 的载荷（比 `JobSnapshot` 少 `result` —— 终稿走 `done` 或查询接口）。 */
export interface JobSnapshotEvent {
  type: 'snapshot'
  job_id: string
  artifact: JobArtifact
  status: JobStatus
  phase: JobPhase
  draft_content: string
  review?: JobReview | null
  /** 优化任务：改哪一节（见 `JobSnapshot.section` 的说明） */
  section?: string | null
}

/** SSE `phase` 事件的载荷。 */
export interface JobPhaseEvent {
  type: 'phase'
  phase: JobPhaseEventName
  round?: number | null
  detail?: string
  /** 重写时带上一轮的审核意见（"在改什么"） */
  issues?: JobReviewIssue[]
}

/** SSE `review` 事件的载荷。 */
export interface JobReviewEvent {
  type: 'review'
  content: JobReview
}

/** SSE `done` 事件的载荷。 */
export interface JobDoneEvent {
  type: 'done'
  artifact: JobArtifact
  /** 生成：产物**全文**；优化：把改过的那一节**拼回整篇**之后的结果 */
  content: string
  /** 仅 PRD 生成有（与 `content` 同值，需求点名的键） */
  final_prd?: string
  review?: JobReview | null
  revision_applied?: boolean | null
  truncated?: boolean | null
  finish_reason?: string | null
  /** 仅优化任务有：改的是哪一节 */
  section?: string | null
}

/** SSE `error` 事件的载荷。 */
export interface JobErrorEvent {
  type: 'error'
  message: string
  request_id?: string | null
}

/**
 * `readJobStream` 的回调集合。
 *
 * ⚠️ 与 `api.ts` 的 `StreamHandlers` **不是同一套**：那个是"裸文本 + chunk/stage/done"，
 * 这个是"任务事件 + snapshot/text_delta/phase/review/done"。两套混用会让排错时
 * 分不清是哪条链路在推 —— 所以类型上就分开。
 */
export interface JobStreamHandlers {
  /**
   * 订阅建立时的第一帧：**目前已有的全文**（`draft_content`）。
   *
   * ⚠️ 收到它**不要**从第一个字重新播打字机 —— 它可能有一万多字，重播等于让用户
   * 白等一遍；正确做法是立刻整篇显示，之后只把新到的 `text_delta` 追加在后面。
   */
  onSnapshot?: (snapshot: JobSnapshotEvent) => void
  /** 增量文字（每个分片推送一次）。 */
  onDelta?: (chunk: string) => void
  onPhase?: (event: JobPhaseEvent) => void
  onReview?: (review: JobReview) => void
  onDone?: (event: JobDoneEvent) => void
  /** 整趟汇总（后端在 `done` 之前发一帧，与 `conversation` 那套同顺序） */
  onRunSummary?: (summary: RunSummary) => void
}
