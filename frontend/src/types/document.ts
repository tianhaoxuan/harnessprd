/**
 * 文档槽位与版本链的对外类型 —— 与后端 `backend/api/schemas.py` 的「文档槽位与版本链」
 * 那一段一一对应（6 条接口在 `services/documentApi.ts`）。
 *
 * ## 两处与需求原文不一致的地方（按代码事实写）
 *
 * 1. **审查结论的类型叫 `JobReview`，不叫 `PrdReviewResult`** —— 后者在本仓库里
 *    不存在。定义在 `types/job.ts`，就是 App 里 `prdReviewResult` 那份 state 的类型。
 *    这里直接复用，不另抄一份（后端 `metadata.review` 的形状与 SSE `review` 事件同源）。
 * 2. **`RunSummary` 从 `types/index.ts` 复用**（与需求一致）。
 *
 * ## ⚠️ 三个"键可能不在"的字段
 *
 * 后端刻意用"**省略键**"而不是"给 null"来表达"没有"：
 *
 * | 字段 | 什么时候不在 |
 * | --- | --- |
 * | `DocumentVersionListItem.change_note` | 用户没给这一版写备注 |
 * | `DocumentVersionMetadata.*` | 那一版没产生过这个信息 |
 * | `DocumentSlotSummary.updated_at` | 槽位**还没有任何版本** |
 *
 * 所以这些字段都是可选的，读的时候要 `?? null` 而不是假定存在。
 */

import type { RunSummary } from './index'
import type { JobReview } from './job'

/**
 * 槽位类型（= 后端的 `doc_type`）。
 *
 * ⚠️ 与前端内部到处在用的 `DocKind`（`'prd' | 'api' | 'prompts'`）**不是一套**：
 * 对外是需求给定的 `api-docs`，内部视图键是 `api`。两者之间**必须**走
 * `DOC_META[kind].docType`（唯一映射处）—— 手写 `'api-docs'` 字面量最容易在
 * 接口文档那一份上写错，而错了只会表现为"这一份永远没有版本"。
 */
export type DocumentType = 'prd' | 'api-docs' | 'prompts'

/**
 * 一版是**怎么来的**（后端 `VersionSourceKind` 同值）。
 *
 * ⚠️ `optimize` / `auto_save` **不会出现在历史行上**：后端对这两种只就地改 current
 * 的正文，既不加版本号、也不改那一行出生时的 `source_kind`（需求 §4 的写入规则）。
 * 所以列表里实际见到的只有 `generate` / `checkpoint` / `restore` / `import` 四种；
 * 另两个取值留着是因为后端枚举里有，前端显示逻辑必须能处理它们（不要假定不会出现）。
 */
export type VersionSourceKind =
  | 'generate'
  | 'optimize'
  | 'auto_save'
  | 'checkpoint'
  | 'restore'
  | 'import'

/** 来源标签的中文文案（需求 §六 的表）。 */
export const VERSION_SOURCE_LABEL: Record<VersionSourceKind, string> = {
  generate: '生成',
  optimize: '优化',
  auto_save: '编辑',
  checkpoint: '另存',
  restore: '恢复',
  import: '导入',
}

/** `GET /api/session/{id}/documents` 的一项：一个槽位的摘要（不含正文）。 */
export interface DocumentSlotSummary {
  doc_type: DocumentType
  /**
   * 槽位行 id。**非空**：`GET .../documents` 会顺带把还没建过的槽位建出来，
   * 所以三种 `doc_type` 永远各有一行（需求 §6.1 要的形状）。
   */
  document_id: string
  current_version_id: string | null
  current_version_no: number | null
  /** 尚无版本时为 `null`（**不是**槽位行的创建时间）。 */
  updated_at: string | null
}

/** 版本列表项（不含全文）。 */
export interface DocumentVersionListItem {
  id: string
  version_no: number
  source_kind: VersionSourceKind
  created_at: string
  /**
   * 正文前 200 字。
   *
   * ⚠️ 后端明确说了它**不是自动摘要**，需求 §一 的"不展示"清单里也点名了它 ——
   * 界面**不要**拿它当摘要渲染。真正的"这一版是什么"由用户备注承担。
   */
  content_preview: string
  is_current: boolean
  /** 用户备注。**没备注时键不存在**（不是 null）。 */
  change_note?: string | null
}

/** `metadata` —— 后端"未产生的字段一律省略"，所以全是可选的。 */
export interface DocumentVersionMetadata {
  run_summary?: RunSummary
  review?: JobReview
  change_note?: string
  restored_from_version_id?: string
  restored_from_version_no?: number
  /** Job 失败但留下了半成品时是 `'failed'`（那一版的正文是残的）。 */
  job_status?: string
}

/** 单版详情（含全文），供只读预览。 */
export interface DocumentVersionDetail {
  id: string
  version_no: number
  source_kind: VersionSourceKind
  content: string
  /** 生成这一版的 `generation_jobs.id`；checkpoint / restore 为 null。 */
  source_job_id: string | null
  /** 切换前的那一版；首版为 null。 */
  parent_version_id: string | null
  metadata: DocumentVersionMetadata
  created_at: string
  is_current: boolean
}

/** `GET .../versions` 的响应：列表 + 槽位身份。 */
export interface DocumentVersionListResponse {
  doc_type: DocumentType
  document_id: string
  current_version_id: string | null
  /** 按 `version_no` **降序**。 */
  versions: DocumentVersionListItem[]
}

/** `POST .../versions/checkpoint` 的回执。 */
export interface CheckpointResult {
  version_id: string
  version_no: number
  document_id: string
}

/** `POST .../versions/{id}/restore` 的回执（多一个 content，供镜像同步）。 */
export interface RestoreResult {
  version_id: string
  version_no: number
  document_id: string
  content: string
}

/** `PATCH .../versions/{id}/note` 的回执。 */
export interface VersionNoteResult {
  version_id: string
  version_no: number
  /** 清空备注后是 `null`。 */
  change_note: string | null
}

/** 这一版有没有用户备注（`null` 与"键不存在"都算没有）。 */
export function hasChangeNote(version: DocumentVersionListItem): boolean {
  return typeof version.change_note === 'string' && version.change_note.trim().length > 0
}
