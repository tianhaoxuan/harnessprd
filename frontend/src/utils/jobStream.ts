/**
 * Job 进度流的读取器：`GET /api/jobs/{id}/stream`（SSE）。
 *
 * ## 与 `api.ts` 的 `readStream` 是什么关系
 *
 * 同一套**帧解析**（复用 `parseFrame`：`\r` 容忍、冒号后空格、非 JSON 负载），
 * 但**读取循环是这一份自己的**，因为三件事都不同：
 *
 * | | `readStream`（`/conversation/*`，POST） | 本函数（`/api/jobs/*`，GET） |
 * | --- | --- | --- |
 * | 事件名 | `chunk` / `stage` / `done` / `error` | `snapshot` / `text_delta` / `phase` / `review` / `done` / `error` |
 * | 终止信号 | 必须收到 `done` 帧，否则抛错 | **`data: [DONE]` 哨兵**；没收到 `done` 也算正常结束（任务可能只是被取消） |
 * | 首帧 | 无 | **`snapshot`**（已有全文） |
 *
 * ⚠️ **不能拿 `readStream` 凑合**：它会因为"没收到 `done`"抛错，而 Job 流在
 * "订阅时任务已经收尾"的情况下会重放一个 `done`（有），但在"任务被取消"时只发 `error`
 * 或干脆什么都没有 —— 用那边会把正常结束当失败。
 *
 * ## 两处最容易写错、这里专门处理
 *
 * 1. **`TextDecoder` 必须带 `{stream: true}`**：一个汉字 3 字节，可能被切在两个网络
 *    分片之间；不带会解出 `�`，且只在长文本里偶发。
 * 2. **帧会跨分片**：必须留残余缓冲，只派发完整的帧（`\n\n` 分隔）。
 */

import { StreamError, parseFrame } from '../services/api'
import type { RunSummary } from '../types'
import type {
  JobArtifact,
  JobDoneEvent,
  JobPhaseEvent,
  JobReview,
  JobSnapshotEvent,
  JobStreamHandlers,
} from '../types/job'

/** Job 的 SSE 不是 `/api/v1` 下的（与 `jobApi.JOB_API_BASE` 同一个前缀）。 */
const JOB_STREAM_BASE = '/api/jobs'

/**
 * 订阅一个任务的进度，读到底（或被 `signal` 中断）才返回。
 *
 * @param jobId 任务 id
 * @param handlers 回调集合（`onSnapshot` 是首帧，见 `JobStreamHandlers` 的说明）
 * @param signal **必须传**：组件卸载 / 新订阅之前要 abort 掉旧连接。
 *   不 abort 的后果不是报错，而是**多条流同时往同一份 state 写**——
 *   表现是界面上文字来回跳（旧任务和新任务的增量交错），极难查。
 */
export async function readJobStream(
  jobId: string,
  handlers: JobStreamHandlers,
  signal?: AbortSignal,
): Promise<void> {
  const response = await fetch(`${JOB_STREAM_BASE}/${encodeURIComponent(jobId)}/stream`, {
    headers: { Accept: 'text/event-stream', 'Cache-Control': 'no-cache' },
    ...(signal ? { signal } : {}),
  })
  if (!response.ok) {
    // 404 会带后端 detail（"生成任务不存在"）—— 它是给人看的，别丢
    let detail = ''
    try {
      const body: unknown = await response.json()
      const raw =
        typeof body === 'object' && body !== null && 'detail' in body
          ? (body as { detail: unknown }).detail
          : body
      detail = typeof raw === 'string' ? raw : ''
    } catch {
      detail = ''
    }
    throw new Error(`订阅生成任务失败（HTTP ${response.status}）${detail ? `：${detail}` : ''}`)
  }
  if (!response.body) throw new Error('订阅生成任务失败：响应没有可读流（后端返回的不是 SSE？）')

  const reader = response.body.getReader()
  const decoder = new TextDecoder('utf-8')
  let buffer = ''
  /** 服务端发过 `done` / `error` 没有。用于把"没收到任何终态就断了"报成异常。 */
  let sawTerminal = false

  try {
    for (;;) {
      const { value, done } = await reader.read()
      if (done) break

      buffer += decoder.decode(value, { stream: true })
      const frames = buffer.split('\n\n')
      buffer = frames.pop() ?? '' // 最后一段可能不完整，留到下一分片再拼

      for (const frame of frames) {
        const parsed = parseFrame(frame)
        if (!parsed) continue

        // `[DONE]`：后端在流末尾发的裸哨兵（不是 JSON，所以走 `data` 的字符串分支）。
        if (parsed.event === 'message' && parsed.data === '[DONE]') {
          sawTerminal = true
          return
        }

        switch (parsed.event) {
          case 'snapshot': {
            const snap = parsed.data as JobSnapshotEvent
            if (snap && typeof snap.draft_content === 'string') handlers.onSnapshot?.(snap)
            break
          }
          case 'text_delta': {
            const piece = (parsed.data as { content?: string } | null)?.content ?? ''
            if (piece) handlers.onDelta?.(piece)
            break
          }
          case 'phase': {
            handlers.onPhase?.(parsed.data as JobPhaseEvent)
            break
          }
          case 'review': {
            const review = (parsed.data as { content?: unknown } | null)?.content
            if (review && typeof review === 'object') handlers.onReview?.(review as JobReview)
            break
          }
          case 'run_summary': {
            // 后端把 `type` 也塞在 data 里，这里剥掉再交给上层（与 `readStream` 同口径）
            const raw = (parsed.data ?? {}) as RunSummary & { type?: string }
            const { type: _ignored, ...summary } = raw
            handlers.onRunSummary?.(summary as RunSummary)
            break
          }
          case 'done': {
            sawTerminal = true
            handlers.onDone?.(parsed.data as JobDoneEvent)
            break
          }
          case 'error': {
            sawTerminal = true
            const failure = parsed.data as { message?: string; request_id?: string } | null
            // 抛 `StreamError`：上层要拿 `requestId` 让用户复制给维护者（与对话流一致）
            throw new StreamError(failure?.message ?? '生成失败，请重试', {
              code: 'JOB_ERROR',
              requestId: failure?.request_id ?? null,
            })
          }
          default:
            // 不认识的事件（后端将来加了新的）：不静默丢弃，留一条 debug 便于排查
            console.debug('[jobStream] 忽略未知事件：', parsed.event, parsed.data)
        }
      }
    }
  } catch (error) {
    await reader.cancel().catch(() => undefined)
    throw error
  }

  // 走到这里 = 流被关闭而没给 `[DONE]`。
  // 收到过 `done` / `error` 的算正常收尾（后端可能只发终止事件就断）；
  // **一个终态都没收到**才当异常 —— 那种情况下界面上会停在一个不动的"生成中"，
  // 必须报出来，否则用户以为还在跑。
  if (!sawTerminal) {
    throw new Error('进度流中断了（没有收到 done / error 事件），任务状态未知 —— 刷新页面可以重新接上')
  }
}

/**
 * 事件名清单（**只作文档与排查用**，协议改动时先看这里，再对照后端 `api/jobs.py`）。
 *
 * | 事件 | 载荷 | 什么时候发 |
 * | --- | --- | --- |
 * | `snapshot` | `{job_id, artifact, status, phase, draft_content, review}` | 订阅建立时第一条 |
 * | `text_delta` | `{content}` | 每个增量片段 |
 * | `phase` | `{phase, round?, detail?, issues?}` | 进入新阶段 |
 * | `review` | `{content: {passed, issues, review_model, review_skipped, round}}` | 审查有结论 |
 * | `run_summary` | 与 `/conversation/*` 同形 | 收尾前（**在 `done` 之前**） |
 * | `done` | `{artifact, content, final_prd?, review, revision_applied, truncated, finish_reason}` | 成功收尾 |
 * | `error` | `{message, request_id}` | 失败收尾 |
 * | `[DONE]` | — | 流结束（裸 `data:` 哨兵，不是 JSON） |
 */
export const JOB_STREAM_EVENTS = [
  'snapshot',
  'text_delta',
  'phase',
  'review',
  'run_summary',
  'done',
  'error',
  '[DONE]',
] as const

/** `artifact` 的中文名（错误提示里用；界面上的正式标题走 `DOC_META.title`）。 */
export const ARTIFACT_LABEL: Record<JobArtifact, string> = {
  prd: 'PRD',
  'api-docs': '接口文档',
  prompts: '提示词套件',
  'optimize-prd': 'PRD 优化',
  'optimize-api-docs': '接口文档优化',
  'optimize-prompts': '提示词套件优化',
}
