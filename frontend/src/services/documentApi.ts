/**
 * 文档槽位与版本链的 HTTP 封装（6 条）—— 与后端 `backend/api/documents.py` 一一对应。
 *
 * | 函数 | 方法与路径 |
 * | --- | --- |
 * | `fetchDocumentSlots` | `GET /api/session/{id}/documents` |
 * | `fetchVersionList` | `GET .../documents/{doc_type}/versions` |
 * | `fetchVersionDetail` | `GET .../documents/{doc_type}/versions/{version_id}` |
 * | `checkpointVersion` | `POST .../versions/checkpoint` |
 * | `restoreVersion` | `POST .../versions/{version_id}/restore` |
 * | `updateVersionNote` | `PATCH .../versions/{version_id}/note` |
 *
 * ## 为什么用 `fetch` 而不是 `api.ts` 的 axios 实例
 *
 * 与 `jobApi.ts` 同一条理由：`api.ts` 的 `http` 实例 `baseURL` 是 `/api/v1`，而版本接口在
 * `/api/session/*`（需求给定的路径，**不在版本段下**，与 `/api/jobs/*` 同一族）。
 * 为了它给那个实例开一个 `baseURL: '/'` 的例外，反而更容易写错。
 *
 * ## 失败一律抛异常（与 `sessionService.ts` 刻意相反）
 *
 * `sessionService` 的自动保存是**旁路能力**，存不上不该打断用户，所以它失败不抛。
 * 而版本接口都是**用户点了按钮**才发的：静默失败会让他以为"保存了新版本""恢复成功了"，
 * 而实际上什么都没发生 —— 那比报错危险得多。所以这里一律抛 `DocumentApiError`
 * （带后端 `detail`），由 UI 显示。
 *
 * ## 400 与 404 的语义（后端的约定，UI 文案据此分叉）
 *
 * - **404**：会话 / 槽位 / 版本查不到（`DocumentNotFound`）；
 * - **400**：请求在当前状态下做不到（`InvalidDocumentRequest`）—— `doc_type` 非法、
 *   restore 的目标就是 current、无内容可保存。
 */

import type {
  CheckpointResult,
  DocumentSlotSummary,
  DocumentType,
  DocumentVersionDetail,
  DocumentVersionListResponse,
  RestoreResult,
  VersionNoteResult,
} from '../types/document'

/** 版本接口的前缀。**不在 `/api/v1` 下**（后端 `api/documents.py` 自带这个前缀）。 */
export const DOCUMENT_API_BASE = '/api/session'

/** 带上后端 `detail` 的 Error。`status` 供调用方按状态码分叉（400 / 404 的提示不同）。 */
export class DocumentApiError extends Error {
  readonly status: number

  constructor(message: string, status: number) {
    super(message)
    this.name = 'DocumentApiError'
    this.status = status
  }
}

/** 读后端的错误体并把 `detail` 拼进文案。解析不出来时退回状态码。 */
async function toDocumentError(response: Response): Promise<DocumentApiError> {
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
  return new DocumentApiError(
    `文档版本请求失败（HTTP ${response.status}）${detail ? `：${detail}` : ''}`,
    response.status,
  )
}

/** 发一个 JSON 请求并解析响应。`path` 是**紧跟 base 之后**的那一段。 */
async function requestJson<T>(path: string, init: RequestInit = {}): Promise<T> {
  const hasBody = init.body !== undefined
  const response = await fetch(`${DOCUMENT_API_BASE}${path}`, {
    ...init,
    headers: {
      Accept: 'application/json',
      ...(hasBody ? { 'Content-Type': 'application/json' } : {}),
      ...init.headers,
    },
  })
  if (!response.ok) throw await toDocumentError(response)
  return (await response.json()) as T
}

const slotPath = (sessionId: string): string => `/${encodeURIComponent(sessionId)}/documents`

const versionsPath = (sessionId: string, docType: DocumentType): string =>
  `${slotPath(sessionId)}/${encodeURIComponent(docType)}/versions`

/** 三种槽位的摘要。首次调用会**顺带建出三个空槽位行**（后端如此设计，见 §6.1）。 */
export async function fetchDocumentSlots(sessionId: string): Promise<DocumentSlotSummary[]> {
  const body = await requestJson<{ items: DocumentSlotSummary[] }>(slotPath(sessionId))
  return body.items
}

/** 版本列表（**不含全文**），按 `version_no` 降序。 */
export async function fetchVersionList(
  sessionId: string,
  docType: DocumentType,
): Promise<DocumentVersionListResponse> {
  return requestJson<DocumentVersionListResponse>(versionsPath(sessionId, docType))
}

/** 单版详情（**含全文**），供只读预览。 */
export async function fetchVersionDetail(
  sessionId: string,
  docType: DocumentType,
  versionId: string,
): Promise<DocumentVersionDetail> {
  return requestJson<DocumentVersionDetail>(
    `${versionsPath(sessionId, docType)}/${encodeURIComponent(versionId)}`,
  )
}

/**
 * 保存为新版本（checkpoint）。
 *
 * `content` 是**编辑器里的全文**。传空 / 全空白时**不发这个键** —— 后端在
 * "没有 current 版本、又没带 content"时返 400（无内容可保存），而不带 content 时它会
 * 退回到服务端 current 的正文。这样"只是打个标记"的调用不必把上万字传一遍。
 */
export async function checkpointVersion(
  sessionId: string,
  docType: DocumentType,
  body: { content?: string } = {},
): Promise<CheckpointResult> {
  const content = body.content
  const payload = typeof content === 'string' && content.trim() ? { content } : {}
  return requestJson<CheckpointResult>(`${versionsPath(sessionId, docType)}/checkpoint`, {
    method: 'POST',
    body: JSON.stringify(payload),
  })
}

/**
 * 恢复此版本：以该历史版正文**新建下一版**并设为 current（历史行不删不改）。
 *
 * 响应里的 `content` 就是新 current 的正文，调用方拿它直接刷新编辑器即可
 * （后端同时也已经把 `session_data` 镜像写回去了）。
 */
export async function restoreVersion(
  sessionId: string,
  docType: DocumentType,
  versionId: string,
): Promise<RestoreResult> {
  return requestJson<RestoreResult>(
    `${versionsPath(sessionId, docType)}/${encodeURIComponent(versionId)}/restore`,
    { method: 'POST' },
  )
}

/**
 * 给任意一版（含历史版与 current）写用户备注。
 *
 * 传空串 = **清空**备注（后端会把 `metadata.change_note` 这个键删掉，返回 `null`）。
 */
export async function updateVersionNote(
  sessionId: string,
  docType: DocumentType,
  versionId: string,
  changeNote: string,
): Promise<VersionNoteResult> {
  return requestJson<VersionNoteResult>(
    `${versionsPath(sessionId, docType)}/${encodeURIComponent(versionId)}/note`,
    { method: 'PATCH', body: JSON.stringify({ change_note: changeNote }) },
  )
}
