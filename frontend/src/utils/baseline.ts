/**
 * 需求基线（05 篇）：**确认**之后才允许生成接口文档与提示词，而**改过 PRD 就失效**。
 *
 * ## 为什么基线不是一个布尔值就够了
 *
 * 「已确认」必须绑定到**具体的那一版正文**上。只记一个 `true` 的话，用户在确认之后
 * 随手改两行 PRD，界面仍然显示"已确认基线" —— 而接口文档接下来会基于**改之前**的
 * 需求契约生成。这正是本篇要解决的问题（现状问题第 1 条）。
 *
 * 所以确认时记三样东西：`version_id`（哪一版）、`version_no`（给人看的编号）、
 * `contentHash`（那一版正文的指纹）。失效判据据此有两条：
 *
 * | 判据 | 抓到的场景 |
 * | --- | --- |
 * | 版本 id 变了 | 重新生成 / 恢复到别的版本 / 优化完成（版本号或版本行变了） |
 * | 正文指纹变了 | 用户**就地手改**（版本行没变，但正文不同了） |
 *
 * ## 为什么指纹不是 sha256
 *
 * 工单写的是"如 sha256 前 16 位"，但 `crypto.subtle.digest` 是**异步**的，而这里的比较
 * 发生在渲染期（编辑器每次 `onContentChange` 都要判一次）—— 为了一个变更检测去排队
 * 一次微任务、再引入"指纹还没算出来"的中间态，代价远大于收益。
 *
 * 这里用 FNV-1a 32 位的两轮拼成 16 个十六进制字符：**它不是密码学哈希**，
 * 只用来回答"这一版正文和确认时是不是同一份"。输入是用户自己的文档、没有对抗方，
 * 冲突概率对这个问题无关紧要。
 */

/** 会话里与需求基线相关的字段（`SessionData` 的子集）。 */
export interface BaselineSnapshot {
  prdBaselineConfirmed?: boolean
  prdBaselineConfirmedAt?: string | null
  prdBaselineVersionId?: string | null
  prdBaselineVersionNo?: number | null
  prdBaselineContentHash?: string | null
}

/**
 * 正文指纹：16 个十六进制字符（FNV-1a 32 位跑两遍，不同初值）。
 *
 * 纯函数、同步、O(n) 一遍扫描。**不用于安全用途**（见文件头）。
 */
export function contentFingerprint(text: string): string {
  const round = (seed: number): number => {
    let hash = seed
    for (let index = 0; index < text.length; index += 1) {
      hash ^= text.charCodeAt(index)
      // FNV 质数 16777619，用移位相加避免 32 位溢出警告
      hash = (hash + ((hash << 1) + (hash << 4) + (hash << 7) + (hash << 8) + (hash << 24))) >>> 0
    }
    return hash >>> 0
  }
  const first = round(0x811c9dc5).toString(16).padStart(8, '0')
  const second = round(0x01000193).toString(16).padStart(8, '0')
  return `${first}${second}`
}

/**
 * 基线与当前 PRD 是否还对得上（对不上 → 撤销确认）。
 *
 * @param baseline 确认时记下的那份快照
 * @param currentVersionId 当前 PRD 的版本 id（拿不到就传 `null`：那时只按指纹判）
 * @param currentContent 当前 PRD 正文
 */
export function shouldRevokeBaseline(
  baseline: BaselineSnapshot,
  currentVersionId: string | null,
  currentContent: string,
): boolean {
  if (baseline.prdBaselineConfirmed !== true) return false // 本来就没确认，没什么可撤销
  const confirmedId = baseline.prdBaselineVersionId ?? null
  // 版本行变了（重新生成 / 恢复到别的版本）→ 失效。id 拿不到时不判这一条，
  // 免得把"版本接口暂时没返回"当成"换版了"，那会让用户白点一次确认。
  if (confirmedId && currentVersionId && confirmedId !== currentVersionId) return true
  const confirmedHash = baseline.prdBaselineContentHash ?? null
  if (!confirmedHash) return false // 老数据没有指纹：只按版本 id 判，不凭空撤销
  return contentFingerprint(currentContent) !== confirmedHash
}

/**
 * 基线的**展示态**（横幅文案用）。
 *
 * `confirmed` 与否只由 `prdBaselineConfirmed` 决定；`revoked` 用来区分
 * "从来没确认过"（首次进入）与"确认过、后来失效了"（改过 PRD）——
 * 两句话不一样，前者要教用户怎么做，后者要解释为什么又弹出来了。
 */
export function describeBaseline(baseline: BaselineSnapshot): {
  confirmed: boolean
  label: string
  versionNo: number | null
  confirmedAt: string | null
} {
  const confirmed = baseline.prdBaselineConfirmed === true
  const versionNo = typeof baseline.prdBaselineVersionNo === 'number' ? baseline.prdBaselineVersionNo : null
  const confirmedAt = baseline.prdBaselineConfirmedAt ?? null
  const label = confirmed
    ? `已确认需求基线 · 基于 PRD ${versionNo ? `v${versionNo}` : '（未登记版本）'}${
        confirmedAt ? ` · ${formatBaselineTime(confirmedAt)}` : ''
      }`
    : '需求基线尚未确认'
  return { confirmed, label, versionNo, confirmedAt }
}

/** `2026-10-07T01:02:03+08:00` → `2026-10-07 01:02`（给人看的一行）。 */
export function formatBaselineTime(iso: string): string {
  const match = /^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2})/.exec(iso)
  return match ? `${match[1]} ${match[2]}` : iso
}

/**
 * 本地时间戳，带时区偏移（`2026-10-07T01:02:03+08:00`）。
 *
 * 为什么不用 `new Date().toISOString()`：那个给的是 **UTC**（`...Z`），
 * 而"我什么时候确认的"是给人看的字段 —— 存 UTC 会让界面把 09:00 显示成 01:00。
 * 后端 manifest 里也是带偏移的（`delivery_export_service._CST`），两端保持一致。
 */
export function localTimestamp(now = new Date()): string {
  const pad = (value: number): string => String(value).padStart(2, '0')
  const offsetMinutes = -now.getTimezoneOffset()
  const sign = offsetMinutes >= 0 ? '+' : '-'
  const absolute = Math.abs(offsetMinutes)
  return (
    `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}` +
    `T${pad(now.getHours())}:${pad(now.getMinutes())}:${pad(now.getSeconds())}` +
    `${sign}${pad(Math.floor(absolute / 60))}:${pad(absolute % 60)}`
  )
}
