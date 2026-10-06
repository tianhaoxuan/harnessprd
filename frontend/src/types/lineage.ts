/**
 * 三份产物的版本与上游关系（05 篇）—— `GET /api/session/{id}/artifact-lineage`。
 *
 * ## 为什么前端不能自己算"过期"
 *
 * 判据是「下游产物的 `derived_from.prd_version_no` < **PRD 的当前版本号**」。
 * 前半截在接口文档那一版的 metadata 里（版本列表能拿到），后半截**拿不到** ——
 * 版本列表接口是按 `doc_type` 一次查一份的，站在接口文档页时前端手上没有 PRD 的版本号。
 *
 * 所以由后端一次给全，并把**整句文案**也一起给：界面上显示的那句话与交付包
 * README / manifest 里写的那句话来自同一个实现（`delivery_export_service.stale_entries`），
 * 两处各拼一遍的话，用户拿两份东西对照时会以为哪个算错了。
 */

export interface LineageDocument {
  doc_type: 'prd' | 'api-docs' | 'prompts'
  title: string
  /** 有没有正文（没生成过就是 `false`） */
  present: boolean
  version_no: number | null
  version_id: string | null
  generated_at: string | null
  /** 生成这一版时的上游版本快照（PRD 自己是 `null`） */
  derived_from: {
    prd_version_id?: string
    prd_version_no?: number
    api_docs_version_id?: string
    api_docs_version_no?: number
    /** 生成提示词时用户跳过了接口文档 */
    api_docs_skipped?: boolean
  } | null
}

export interface LineageStale {
  stale: boolean
  /** 后端给的整句提示（`stale` 为 false 时是 `null`） */
  message: string | null
}

export interface ArtifactLineage {
  session_id: string
  documents: Record<'prd' | 'api_docs' | 'prompts', LineageDocument>
  stale: Record<'api_docs' | 'prompts', LineageStale>
}
