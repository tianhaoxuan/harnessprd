/**
 * 会话存储服务 —— 后端 `/api/session/*` 四个接口的前端封装 + 防抖保存。
 *
 * ## 为什么单独一个文件、不塞进 `api.ts`
 *
 * `api.ts` 里是 `/api/v1/conversation/*` 那套**无状态补全**接口（生成、SSE 流）。
 * 本文件是**会话快照的整体存取**（工作台 state 的存/取/删），两者前缀、语义、
 * 失败处理都不同：
 *
 * | | `api.ts` | 本文件 |
 * | --- | --- | --- |
 * | 前缀 | `/api/v1`（`API_BASE_URL`） | `/api/session` |
 * | 失败 | **抛异常**，由页面决定怎么提示 | **只 `console.error`，绝不抛** |
 *
 * ⚠️ **失败不抛**是刻意的（需求明确要求）：会话存储是**旁路能力** ——
 * 后端挂了、网络断了，用户照样应当能在页面上把东西做完（本地还有一份）。
 * 一旦这里抛出去，一个保存失败就会把页面交互打断，那是把"存储不可用"升级成了"应用不可用"。
 * 代价是调用方拿到的是 `null` / `[]` / `false`，**必须自己判空**。
 *
 * ## 快照类型
 *
 * `SessionData` 直接从 `App.tsx` **按类型导入**（`import type`，编译后不留运行时依赖）——
 * 这样"前端类型与工作台快照完全兼容"是**编译器保证**的，而不是靠两边各写一份然后祈祷不漂移。
 *
 * ## 防抖与 flush
 *
 * `debouncedSaveSession()` 只保留**最后一次**待写内容（后到的覆盖先到的），
 * 静默 `delay` 毫秒后写一次。`flushPendingSave()` 立刻把待写内容发出去，
 * 用在 `pagehide` / 组件卸载之前 —— 不 flush 的话，用户最后 1 秒的编辑会随页面一起消失。
 */

import type { SessionData } from '../App'
import type { DocKind } from '../types'
import type { ArtifactLineage } from '../types/lineage'

/** 会话存储接口的前缀。**不在 `/api/v1` 下**（后端 `api/session.py` 自带这个前缀）。 */
export const SESSION_API_BASE = '/api/session'

/** 防抖默认延迟（毫秒）。 */
export const DEFAULT_SAVE_DELAY_MS = 1000

/** `GET /api/session/list` 的列表项（**不含** `session_data`）。 */
export interface SessionSummary {
  id: string
  title: string
  /** 三套入口之一，与工作台的 `EntryMode` 同值 */
  entry_mode: string
  /** 当前阶段，与工作台的 `ViewState` 同值 */
  current_stage: string
  status: string
  created_at: string
  updated_at: string
}

/** `POST /api/session/save` 的响应：新建与更新统一是这两项。 */
export interface SessionSaveResult {
  id: string
  title: string
}

/** `loadSession()` 的结果：元信息 + **解析好的**工作台快照。 */
export interface LoadedSession {
  id: string
  title: string
  updatedAt: string
  /** 解析后的工作台 state。直接喂回工作台的恢复逻辑即可。 */
  data: SessionData
  /** 服务端给的 JSON **原文**（逐字节）。需要原样转存时用它，别用 `JSON.stringify(data)`。 */
  raw: string
  /** 上次生成被打断的是哪份产物；`null` = 没有被打断。 */
  interruptedKind: DocKind | null
  /**
   * 这次读取发生了降级时，原始的 `viewState`（形如 `generating-prd`）。
   * `null` = 没降级。
   */
  downgradedFrom: string | null
}

interface RequestOutcome<T> {
  ok: boolean
  status: number
  data: T | null
}

/**
 * 发一个请求并解析 JSON。**所有失败都收敛在这里**（不抛）。
 *
 * `keepalive` 用于 flush 那条路径：请求可以在页面卸载之后继续发完。
 * 其他请求不开它（会占用浏览器的 keepalive 配额，且没必要）。
 */
async function requestJson<T>(
  path: string,
  init: RequestInit & { keepalive?: boolean } = {},
): Promise<RequestOutcome<T>> {
  try {
    const response = await fetch(`${SESSION_API_BASE}${path}`, {
      headers: { 'Content-Type': 'application/json', ...(init.headers ?? {}) },
      ...init,
    })
    if (!response.ok) {
      return { ok: false, status: response.status, data: null }
    }
    // DELETE 之类可能没有正文；解析失败不该让整条调用崩掉
    const text = await response.text()
    return { ok: true, status: response.status, data: text ? (JSON.parse(text) as T) : null }
  } catch (error) {
    console.error('[sessionService] 请求失败：', path, error)
    return { ok: false, status: 0, data: null }
  }
}

/**
 * 列会话摘要。失败返回 **`[]`**（不是 `null`）—— 调用方多半直接 `.map()`，
 * 给空数组能让页面显示"暂无会话"，而不是一片空白或报错。
 */
export async function listSessions(): Promise<SessionSummary[]> {
  const outcome = await requestJson<{ items: SessionSummary[] }>('/list')
  if (!outcome.ok) {
    console.error('[sessionService] 列表失败，状态码：', outcome.status)
    return []
  }
  return outcome.data?.items ?? []
}

/**
 * 取单条会话（含完整快照）。
 *
 * 三种"没有"都返回 `null`，但日志级别不同：
 * - **404**（记录被删了） → `console.warn`：这是正常业务结果，不是故障
 * - 其他失败 → `console.error`
 * - 快照 JSON 解析失败 → `console.error`（存进去的东西读不回来，必须让人看见）
 */
export async function loadSession(id: string): Promise<LoadedSession | null> {
  const outcome = await requestJson<{
    id: string
    title: string
    updated_at: string
    session_data: string
    interrupted_kind: DocKind | null
    downgraded_from: string | null
  }>(`/${encodeURIComponent(id)}`)
  if (!outcome.ok) {
    if (outcome.status === 404) console.warn('[sessionService] 会话已不存在：', id)
    else console.error('[sessionService] 读取失败，状态码：', outcome.status)
    return null
  }
  const payload = outcome.data
  if (!payload) {
    console.error('[sessionService] 读取返回了空正文：', id)
    return null
  }
  try {
    return {
      id: payload.id,
      title: payload.title,
      updatedAt: payload.updated_at,
      data: JSON.parse(payload.session_data) as SessionData,
      raw: payload.session_data,
      interruptedKind: payload.interrupted_kind ?? null,
      downgradedFrom: payload.downgraded_from ?? null,
    }
  } catch (error) {
    console.error('[sessionService] 快照不是合法 JSON，无法解析：', id, error)
    return null
  }
}

/**
 * 保存（即时）。`id` 留空 = 新建，后端返回新 id。
 *
 * 失败返回 `null` —— **调用方必须判空**：`null` 意味着"这次没存上"，
 * 拿它当 `{id}` 用（比如 `result.id`）会直接 `TypeError` 把页面打崩。
 */
export async function saveSession(
  data: SessionData,
  id?: string,
): Promise<SessionSaveResult | null> {
  const outcome = await requestJson<SessionSaveResult>('/save', {
    method: 'POST',
    body: JSON.stringify(id ? { id, session_data: data } : { session_data: data }),
  })
  if (!outcome.ok || !outcome.data) {
    console.error('[sessionService] 保存失败，状态码：', outcome.status)
    return null
  }
  return outcome.data
}

/**
 * 交付包（05 篇）：`GET /api/session/{id}/export` → zip 字节流。
 *
 * ⚠️ **不能走 `requestJson`**：那条路会 `response.json()`，而这里是二进制。
 * 文件名优先取 `Content-Disposition` 的 `filename*=UTF-8''`（后端给的中文名），
 * 拿不到就退回一个安全的默认名 —— 联合类型里 `fetch` 的 `headers.get` 一定存在，
 * 所以不必判空。
 */
export async function fetchDeliveryZip(
  id: string,
): Promise<{ blob: Blob; fileName: string }> {
  const response = await fetch(`${SESSION_API_BASE}/${encodeURIComponent(id)}/export`)
  if (!response.ok) {
    throw new Error(`导出失败（HTTP ${response.status}）`)
  }
  const disposition = response.headers.get('Content-Disposition') ?? ''
  const encoded = /filename\*=UTF-8''([^;]+)/i.exec(disposition)?.[1]
  const plain = /filename="([^"]+)"/i.exec(disposition)?.[1]
  const fileName = encoded ? decodeURIComponent(encoded) : (plain ?? 'harnessprd-delivery.zip')
  return { blob: await response.blob(), fileName }
}

/**
 * 三份产物的版本与上游关系（05 篇）。失败返回 `null`（**调用方必须判空**）。
 *
 * 只读、幂等：审核页打开或任务收尾后拉一次，用来显示"上游更新了，这一份可能过期"。
 * 文案由后端给（见 `types/lineage.ts` 的说明），前端不自己拼。
 */
export async function fetchArtifactLineage(id: string): Promise<ArtifactLineage | null> {
  const outcome = await requestJson<ArtifactLineage>(`/${encodeURIComponent(id)}/artifact-lineage`)
  if (!outcome.ok || !outcome.data) {
    console.warn('[sessionService] 取产物溯源信息失败，状态码：', outcome.status)
    return null
  }
  return outcome.data
}

/** 删除。返回是否删掉（404 也算 `false`，不当异常）。 */
export async function deleteSession(id: string): Promise<boolean> {
  const outcome = await requestJson<{ ok: boolean }>(`/${encodeURIComponent(id)}`, {
    method: 'DELETE',
  })
  if (!outcome.ok) {
    if (outcome.status === 404) console.warn('[sessionService] 删除时已不存在：', id)
    else console.error('[sessionService] 删除失败，状态码：', outcome.status)
    return false
  }
  return outcome.data?.ok === true
}

// ---------------------------------------------------------------- 防抖

interface PendingSave {
  id: string
  data: SessionData
}

let pending: PendingSave | null = null
let pendingTimer: number | null = null

/**
 * 防抖保存（默认 1 秒）。**只在已有会话 id 时用** —— 新建会话必须先有 id，
 * 否则每次编辑都会在后端建一条新记录（列表里会冒出一堆半成品）。
 *
 * 没有 `id` 时不排队、不报错，只留一条 `console.debug`：调用方在"还没建会话"的阶段
 * 每敲一个字就调它是正常写法，不该刷一屏错误。
 *
 * ⚠️ **后到的内容覆盖先到的**（只存最新一份）：防抖的意义就是"只写最后那个状态"，
 * 排队写三次中间态除了多三次网络往返没有任何价值。
 */
export function debouncedSaveSession(data: SessionData, id?: string, delay = DEFAULT_SAVE_DELAY_MS): void {
  if (!id) {
    console.debug('[sessionService] 尚无会话 id，防抖保存跳过（先 saveSession 建一条）')
    return
  }
  pending = { id, data }
  if (pendingTimer !== null) window.clearTimeout(pendingTimer)
  pendingTimer = window.setTimeout(() => {
    pendingTimer = null
    void flushPendingSave()
  }, Math.max(0, delay))
}

/**
 * 立刻把防抖队列里待写的内容发出去。`pagehide` / 组件卸载前调用。
 *
 * - 没有待写内容 → 直接返回 `null`（不发请求）
 * - 有 → 取消计时器并**立刻**保存（`keepalive`，页面卸载后请求仍可发完）
 *
 * ⚠️ 在 `beforeunload` 里**不能 `await`**（浏览器不等你）。所以：
 * 能 `await` 的场景（路由切换、组件卸载）请 `await flushPendingSave()`；
 * 真在 `pagehide` 里就 fire-and-forget —— `keepalive` 会兜住发送这一步。
 */
export async function flushPendingSave(): Promise<SessionSaveResult | null> {
  if (pendingTimer !== null) {
    window.clearTimeout(pendingTimer)
    pendingTimer = null
  }
  const queued = pending
  pending = null
  if (!queued) return null
  const outcome = await requestJson<SessionSaveResult>('/save', {
    method: 'POST',
    body: JSON.stringify({ id: queued.id, session_data: queued.data }),
    keepalive: true,
  })
  if (!outcome.ok || !outcome.data) {
    console.error('[sessionService] flush 保存失败，状态码：', outcome.status)
    return null
  }
  return outcome.data
}
