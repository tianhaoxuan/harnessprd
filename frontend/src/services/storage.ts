/**
 * 浏览器侧存储封装。
 *
 * **不要绕过它直接写 `localStorage`** —— 这里落实的是 `docs/会话持久化方案.md` 的约定：
 *
 * | 约定 | 出处 |
 * | --- | --- |
 * | key 命名 `harnessprd:{env}:{schema}:{kind}[:{session_id}]` | §4.2 |
 * | 统一信封 `{ schema, env, saved_at, payload }` | §4.1 |
 * | schema / env 不符 → **直接丢弃**，不写迁移链 | §8.2 |
 * | `saved_at` 超过 7 天 → 清除 | §8.2 |
 *
 * 为什么可以粗暴丢弃：浏览器侧数据**不是权威**（权威在服务端），丢了最坏只是多打一次接口。
 * 服务端数据不能丢，所以那边的策略完全不同（`docs/状态数据设计.md` §6.2）。
 *
 * ⚠️ 关于表单：按 §5.1，表单的**主**存储是服务端（前端防抖 800ms 写
 * `PUT /sessions/{id}/form`），localStorage 只是**同步失败时的降级兜底**。
 * 会话接口就绪后，这里的草稿会降级成兜底角色 —— 所以它先按规范实现，将来不用重写。
 *
 * ## 本模块存什么、不存什么
 *
 * | kind | 存什么 | 谁在写 |
 * | --- | --- | --- |
 * | `session` | 整份会话（id / 表单 / 消息 / 三份产物 / 视图状态） | `App.tsx` 的 `saveSession()` |
 * | `draft` | 表单草稿 | `App.tsx`（防抖 800ms） |
 *
 * ⚠️ **本模块只管 key 约定、信封、过期与泛型读写**，不认识"会话"这个概念的字段 ——
 * `SessionData` 的形状定义在 `App.tsx`，因为那是**产品决定**（哪些字段要留住），
 * 不是存储约定。这样 storage.ts 不需要随会话结构变化而改。
 *
 * ⚠️ **旧的 `conversation-pointer` / `conversation:{id}` 两把键已不再被读取**
 * （合并成了单键 `session`）。项目尚未发布，所以**不做数据迁移**：旧键会在 7 天后
 * 按 §8.2 自然过期。本地还留着一份旧记录的话，刷新后会看到空会话 —— 重填一次即可。
 */

const NAMESPACE = 'harnessprd'

/** 浏览器侧数据结构版本。**改信封或 payload 结构时必须 +1**（旧 key 自然失效）。 */
const SCHEMA_VERSION = 1

/** 环境标识，防止开发环境的数据污染线上（§4.2）。Vite 的 mode：development / production。 */
const ENV: string = import.meta.env.MODE

/** 陈旧缓存无价值，超过这个时间直接清（§8.2）。 */
const MAX_AGE_MS = 7 * 24 * 60 * 60 * 1000

/** 表单草稿的 kind。会话接口接上后它会变成 `draft:{sessionId}`（§3.2）。 */
export const FORM_DRAFT_KIND = 'draft'

type Envelope<T> = {
  schema: number
  env: string
  saved_at: string
  payload: T
}

function keyFor(kind: string, sessionId?: string): string {
  return [NAMESPACE, ENV, SCHEMA_VERSION, kind, sessionId].filter(Boolean).join(':')
}

function isEnvelope(value: unknown): value is Envelope<unknown> {
  if (typeof value !== 'object' || value === null) return false
  const candidate = value as Record<string, unknown>
  return (
    typeof candidate.schema === 'number' &&
    typeof candidate.env === 'string' &&
    typeof candidate.saved_at === 'string' &&
    'payload' in candidate
  )
}

/**
 * 读一条本地数据。任何不符合约定的情况都返回 `null` 并顺手清掉那个 key。
 *
 * 读取失败不该让页面崩 —— 它只是缓存，不是权威数据。
 */
export function readLocal<T>(kind: string, sessionId?: string): T | null {
  const key = keyFor(kind, sessionId)
  let raw: string | null
  try {
    raw = window.localStorage.getItem(key)
  } catch {
    // 隐私模式 / 存储被禁用时 getItem 会抛
    return null
  }
  if (!raw) return null

  let parsed: unknown
  try {
    parsed = JSON.parse(raw)
  } catch {
    window.localStorage.removeItem(key)
    return null
  }

  if (!isEnvelope(parsed)) {
    window.localStorage.removeItem(key)
    return null
  }

  // 信封里的 schema 大于当前代码支持的 → 是新版前端写的，旧代码读不懂，清掉（§8.2）
  if (parsed.schema !== SCHEMA_VERSION) {
    window.localStorage.removeItem(key)
    return null
  }
  // 环境不符 → 忽略（§8.2）
  if (parsed.env !== ENV) {
    window.localStorage.removeItem(key)
    return null
  }
  // 超过 7 天 → 清掉（§8.2）
  const savedAt = Date.parse(parsed.saved_at)
  if (Number.isNaN(savedAt) || Date.now() - savedAt > MAX_AGE_MS) {
    window.localStorage.removeItem(key)
    return null
  }

  return parsed.payload as T
}

/** 写一条本地数据（自动套信封）。存储不可用时静默失败 —— 它只是缓存。 */
export function writeLocal<T>(kind: string, payload: T, sessionId?: string): void {
  const envelope: Envelope<T> = {
    schema: SCHEMA_VERSION,
    env: ENV,
    saved_at: new Date().toISOString(),
    payload,
  }
  try {
    window.localStorage.setItem(keyFor(kind, sessionId), JSON.stringify(envelope))
  } catch {
    // 配额满 / 存储被禁用：忽略。不要因此打断用户填表。
  }
}

/** 删除一条本地数据。 */
export function clearLocal(kind: string, sessionId?: string): void {
  try {
    window.localStorage.removeItem(keyFor(kind, sessionId))
  } catch {
    /* 同上 */
  }
}

// ---------------------------------------------------------------- 表单草稿

/** 读表单草稿。键 = 题目 id，值 = 作答（与后端 `form: dict[str, str]` 同形）。 */
export function readFormDraft(): Record<string, string> | null {
  const payload = readLocal<Record<string, string>>(FORM_DRAFT_KIND)
  if (!payload || typeof payload !== 'object') return null
  // 只保留字符串值，防止旧版本写进别的东西
  const cleaned: Record<string, string> = {}
  for (const [k, v] of Object.entries(payload)) {
    if (typeof v === 'string') cleaned[k] = v
  }
  return Object.keys(cleaned).length > 0 ? cleaned : null
}

export function writeFormDraft(values: Record<string, string>): void {
  writeLocal(FORM_DRAFT_KIND, values)
}

/** 清掉表单草稿。设计 §5.4：**提交成功后**立即删除 —— 同步成功前不要删，否则会丢输入。 */
export function clearFormDraft(): void {
  clearLocal(FORM_DRAFT_KIND)
}

// ---------------------------------------------------------------- 会话（单一键）

/**
 * 会话记录的 kind。完整 key：`harnessprd:{env}:{schema}:session`。
 *
 * ## 为什么是**一个**键，而不是"指针 + 按 sessionId 分片的记录"
 *
 * 早先的实现是两把键：`conversation-pointer` 存 id、`conversation:{id}` 存内容。
 * 那个形状是为了对齐设计里「读会话指针 → `GET /sessions/{id}` → 按 `snapshot.state` 渲染」
 * 的恢复路径。但代价是**两把键必须一起动**：只写记录不写指针就找不到它，
 * 只写指针不写记录就是悬空指针 —— 一类完全可以避免的 bug。
 *
 * 现在合并成一把键：**`sessionId` 就存在记录里**。恢复路径完全一样
 * （`loadSession()` 拿到 `sessionId` → 换成 `GET /sessions/{id}`），少一次读、少一类失步。
 *
 * ⚠️ 键仍走 `harnessprd:{env}:{schema}:{kind}` 约定（`会话持久化方案` §4.2），
 * 值仍套 `{schema, env, saved_at, payload}` 信封（§4.1）—— 环境隔离、
 * schema 版本失效、7 天过期这三件事一个都没丢，因为读写还是走 `readLocal` / `writeLocal`。
 */
export const SESSION_KIND = 'session'

/**
 * 当前环境下的会话键。
 *
 * ⚠️ 它是**算出来的**而不是写死的字面量：写死 `'harnessprd:session'` 会让开发环境与线上
 * 共用一份数据，而这正是 §4.2 那条 key 约定要防的事。
 */
export function sessionKey(): string {
  return keyFor(SESSION_KIND)
}
