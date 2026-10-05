/**
 * Job 的 `artifact` ↔ 工作台的「产物 kind / 视图」映射（**纯函数**）。
 *
 * ## 为什么必须有这一层
 *
 * 两套命名在系统里同时存在，且**不是同义词**：
 *
 * | 概念 | 取值 | 出处 |
 * | --- | --- | --- |
 * | Job 的 artifact | `prd` / `api-docs` / `prompts` | 需求给定的对外命名 |
 * | 产物 kind（`DocKind`） | `prd` / `api` / `prompts` | 工作台内部、`DOC_META` 的键 |
 * | 视图（`ViewState`） | `generating-prd` / `review-api-docs` … | `DOC_META` |
 *
 * `api-docs` 与 `api` 的差别是最容易写错的一处：写错了不会报错，只会让任务把正文
 * 写进**另一份产物**（或让界面停在一个不存在的视图上）。
 * 所以映射只在这里定义一次，`hooks/useGenerationJob` 只用这里的函数。
 *
 * ## 视图名不在这里硬编
 *
 * `generating-*` / `review-*` 两个名字取自 `DOC_META` —— 那张表是唯一来源。
 * 在这里再抄一遍字符串，加第四个产物时就会漏掉这一处，而表现是"生成完了停在生成中"。
 */

import type { DocKind, ViewState } from '../types'
import type {
  GenerateJobArtifact,
  JobArtifact,
  JobDocType,
  JobPhase,
  OptimizeJobArtifact,
} from '../types/job'
import { DOC_META } from './docMeta'

/**
 * artifact → 产物 kind。
 *
 * ⚠️ **`api-docs` → `api`**（不是 `api-docs`）。这是本文件存在的主要理由。
 * 优化 artifact 也在这里（`optimize-prd` → `prd`）—— 它动的就是同一份文档，
 * 所以 `docKindFor` 对六种取值都成立，调用方不必先判"是不是优化"。
 */
const KIND_BY_ARTIFACT: Record<JobArtifact, DocKind> = {
  prd: 'prd',
  'api-docs': 'api',
  prompts: 'prompts',
  'optimize-prd': 'prd',
  'optimize-api-docs': 'api',
  'optimize-prompts': 'prompts',
}

/** 优化 artifact → 它对应的**生成** artifact（`optimize-prd` → `prd`）。 */
const CONTENT_ARTIFACT: Record<OptimizeJobArtifact, GenerateJobArtifact> = {
  'optimize-prd': 'prd',
  'optimize-api-docs': 'api-docs',
  'optimize-prompts': 'prompts',
}

/** 生成 artifact → 它对应的**优化** artifact。 */
const OPTIMIZE_ARTIFACT: Record<GenerateJobArtifact, OptimizeJobArtifact> = {
  prd: 'optimize-prd',
  'api-docs': 'optimize-api-docs',
  prompts: 'optimize-prompts',
}

/** 产物 kind → 优化任务的 `doc_type`（与后端 `DocKind` 同值）。 */
const DOC_TYPE_BY_KIND: Record<DocKind, JobDocType> = {
  prd: 'prd',
  api: 'api',
  prompts: 'prompts',
}

/**
 * 这条任务是不是"按指令优化一节"。
 *
 * ⚠️ 判据只有**前缀**这一处（与后端 `job_models.is_optimize_artifact` 同源）。
 * 散着写 `artifact.startsWith('optimize-')` 的话，将来加一种优化产物会漏掉其中一处，
 * 而表现是"优化任务被当成生成任务处理"—— 界面跳到 `generating-*`、一节片段被当成整篇，
 * 两处都不报错。
 */
export function isOptimizeArtifact(artifact: JobArtifact): artifact is OptimizeJobArtifact {
  return artifact.startsWith('optimize-')
}

/** 优化 artifact → 被优化的那份**生成** artifact。 */
export function contentArtifactFromOptimize(
  artifact: OptimizeJobArtifact,
): GenerateJobArtifact {
  return CONTENT_ARTIFACT[artifact]
}

/** 生成 artifact → 它对应的优化 artifact。 */
export function optimizeArtifactFor(content: GenerateJobArtifact): OptimizeJobArtifact {
  return OPTIMIZE_ARTIFACT[content]
}

/** `doc_type`（服务层口径）→ 生成 artifact（`api` → `api-docs`）。 */
export function contentArtifactFromDocType(docType: JobDocType): GenerateJobArtifact {
  return docType === 'api' ? 'api-docs' : docType
}

/** `doc_type` → 它对应的优化 artifact。 */
export function optimizeArtifactForDocType(docType: JobDocType): OptimizeJobArtifact {
  return OPTIMIZE_ARTIFACT[contentArtifactFromDocType(docType)]
}

/** 任一 artifact 对应的优化 artifact（生成、优化两种输入都收）。 */
export function optimizeArtifactOf(artifact: JobArtifact): OptimizeJobArtifact {
  return isOptimizeArtifact(artifact) ? artifact : OPTIMIZE_ARTIFACT[artifact]
}

/** artifact 对应的产物 kind（`DOC_META` 的键、`writeDoc` 的参数）。 */
export function docKindFor(artifact: JobArtifact): DocKind {
  return KIND_BY_ARTIFACT[artifact]
}

/** artifact 对应的 `doc_type`（优化任务 payload 里那个字段）。 */
export function docTypeFor(artifact: JobArtifact): JobDocType {
  return DOC_TYPE_BY_KIND[docKindFor(artifact)]
}

/**
 * 任务在跑时该停在哪一屏（`generating-*`）。
 *
 * ⚠️ **只接受生成 artifact**：优化任务全程停在 `review-*`（用户就在审阅页上看这一节被改写）。
 * 类型上就挡掉"优化任务误调它"这种错 —— 那种错的表现是界面跳到"整篇生成中"，
 * 而正文里只有一节。
 */
export function generatingViewFor(artifact: GenerateJobArtifact): ViewState {
  return DOC_META[KIND_BY_ARTIFACT[artifact]].generating
}

/** 任务收尾（完成或失败）后落回哪一屏（`review-*`）。优化任务**跑的时候**也停在这里。 */
export function reviewViewFor(artifact: JobArtifact): ViewState {
  return DOC_META[KIND_BY_ARTIFACT[artifact]].review
}

/**
 * 视图状态是不是**正在生成某一份产物**；是的话返回它的 kind。
 *
 * 用途：刷新恢复时判断"快照里那一屏是不是生成中"（真正决定要不要重连的是
 * `activeJobId`，这个只用于把界面摆到对的位置）。
 */
export function docKindOfGeneratingView(view: ViewState): DocKind | null {
  for (const kind of Object.keys(DOC_META) as DocKind[]) {
    if (DOC_META[kind].generating === view) return kind
  }
  return null
}

/**
 * 任务的阶段 → 界面上那句"在干什么"。
 *
 * 只用于**文案**（服务端也会给 `detail`，优先用它的）；真正的步骤条状态由
 * `useGenerationObservability` 按 `PrdGenerationStage` 推。
 *
 * 优化任务没有阶段（单段流）：返回的是一句固定的"正在按你的反馈重写这一节…"。
 */
export function phaseLabel(artifact: JobArtifact, phase: JobPhase): string {
  if (isOptimizeArtifact(artifact)) return '正在按你的反馈重写这一节…'
  if (artifact !== 'prd') {
    return artifact === 'api-docs' ? '正在分片生成接口文档…' : '正在分片生成提示词套件…'
  }
  switch (phase) {
    case 'writing':
      return '正在撰写初稿…'
    case 'reviewing':
      return '写完了，正在审核（这一步没有正文输出，属正常）…'
    case 'rewriting':
      return '审核提了问题，正在重写…'
    case 'generating':
    case 'done':
    default:
      return '正在生成…'
  }
}
