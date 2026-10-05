/**
 * Generation Job 的 HTTP 封装（2 条）：创建任务、查快照。
 *
 * ## 为什么用 `fetch` 而不是 `api.ts` 的 axios 实例
 *
 * `api.ts` 的 `http` 实例 `baseURL` 是 `/api/v1`，而 Job 的路径是 `/api/jobs`
 * （需求给定的、**不在版本段下**，与 `/api/session` 同一类）。共用一个实例就得
 * 为它开一个 `baseURL: '/'` 的例外，反而更容易写错。
 *
 * 订阅进度（`GET /api/jobs/{id}/stream`）在 `utils/jobStream.ts` —— 那条必须手动读流，
 * 与这里的两个 JSON 请求不是一回事。
 *
 * ## 失败一律抛异常
 *
 * 与 `sessionService.ts`（**失败不抛**）**刻意相反**：会话保存是旁路能力，存不上不该
 * 打断用户；而"创建任务失败"是**用户点了生成却没生成**，必须让人看见 ——
 * 静默返回 `null` 会让界面停在一个永远不会开始的"生成中"。
 *
 * 错误文案带后端 `detail`（FastAPI 的 `{detail: ...}`）：409（已有任务在跑）与
 * 422（payload 缺必需项）的 detail 是给用户看的，丢掉它用户只能看到"失败了"。
 */

import type { JobArtifact, JobSnapshot } from '../types/job'

/** Job 接口的前缀。**不在 `/api/v1` 下**（后端 `api/jobs.py` 自带这个前缀）。 */
export const JOB_API_BASE = '/api/jobs'

/** 带上后端 `detail` 的 Error。`status` 供调用方按状态码分叉（409 要特殊提示）。 */
export class JobApiError extends Error {
  readonly status: number

  constructor(message: string, status: number) {
    super(message)
    this.name = 'JobApiError'
    this.status = status
  }
}

/** 读后端的错误体并把 `detail` 拼进文案。解析不出来时退回状态码。 */
async function toJobError(response: Response): Promise<JobApiError> {
  let detail = ''
  try {
    const body: unknown = await response.json()
    const raw =
      typeof body === 'object' && body !== null && 'detail' in body
        ? (body as { detail: unknown }).detail
        : body
    detail = typeof raw === 'string' ? raw : raw ? JSON.stringify(raw) : ''
  } catch {
    detail = ''
  }
  return new JobApiError(
    `生成任务请求失败（HTTP ${response.status}）${detail ? `：${detail}` : ''}`,
    response.status,
  )
}

/**
 * 创建一个生成任务，返回 `job_id`。
 *
 * ⚠️ **这个请求不等生成**：后端只登记任务就返回（生成在后台协程里跑）。
 * 所以拿到 `job_id` 之后必须去订阅 `GET /api/jobs/{id}/stream`，
 * 否则界面上什么都不会发生。
 *
 * `payload` 的形状按 artifact 分（后端 `job_models.PAYLOAD_REQUIRED_KEYS`）：
 * - `prd`：`requirements_summary`（必需）、`conversation_messages`
 * - `api-docs`：`prd_content`（必需）、`conversation_messages`
 * - `prompts`：`prd_content`（必需）、`api_docs_content`、`conversation_messages`
 *
 * 缺必需项后端返 **422**（开流前就拦下），不会创建一个必然失败的任务。
 */
export async function createJob(
  sessionId: string,
  artifact: JobArtifact,
  payload: Record<string, unknown>,
): Promise<{ job_id: string }> {
  const response = await fetch(JOB_API_BASE, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
    body: JSON.stringify({ session_id: sessionId, artifact, payload }),
  })
  if (!response.ok) throw await toJobError(response)
  return (await response.json()) as { job_id: string }
}

/**
 * 查任务快照。
 *
 * 用途有两个，都很关键：
 * 1. **刷新后重连之前**先问一句"它现在什么状态" —— 已完成的任务不需要订阅
 *    （订阅只会收到一次重放，白开一条连接）；
 * 2. 页面进入时把**已有草稿**取回来（虽然订阅的首帧 `snapshot` 也会给，
 *    但先查一次能在"订阅之前"就把界面摆对）。
 */
export async function getJob(jobId: string): Promise<JobSnapshot> {
  const response = await fetch(`${JOB_API_BASE}/${encodeURIComponent(jobId)}`, {
    headers: { Accept: 'application/json' },
  })
  if (!response.ok) throw await toJobError(response)
  return (await response.json()) as JobSnapshot
}
