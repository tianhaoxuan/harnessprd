/**
 * 版本侧栏 + 审阅面板的合成体 —— App 只需要渲染这一个组件（需求 §七）。
 *
 * ## 为什么要有这一层
 *
 * `App` 里已经有 2500 多行编排逻辑了。版本功能再散进去（一个 hook 调用 + 一个侧栏
 * JSX + 一个"任务空闲就刷新"的 effect），就是第四处需要同步维护的状态。
 * 收在这里之后，`App` 只多做一件事：把 `DocumentReview` 换成它，并多传 `sessionId` /
 * `docType` / `jobIdle`。
 *
 * ## 预览态是**这一层**的概念
 *
 * `DocumentVersionPanel` 不知道正文是什么，`DocumentReview` 不知道版本存在 ——
 * "现在展示的到底是编辑器里那份还是某一版历史"只有这里知道。所以：
 *
 * - `content` 传下去的是 `preview?.content ?? content`（预览时看历史正文）；
 * - `onContentChange` 在预览态**直接 return**（只读预览不许改到工作稿）；
 * - 恢复成功后才把新正文通过 `onContentChange` 写回工作稿。
 *
 * ⚠️ 需求原文把 `DocumentReview` 的那两个 prop 叫 `onSaveEdit` / `versionPanel`，
 * 而代码里实际是 `onContentChange` 与五个新 prop（见 `DocumentReviewProps`）。
 * 这里按**代码事实**接线。
 */

import { useEffect, useRef, useState } from 'react'

import DocumentReview, { type DocumentReviewProps } from './DocumentReview'
import DocumentVersionPanel from './DocumentVersionPanel'
import QualityReportPanel, { QualityReportEmpty } from './QualityReportPanel'
import ReviewResultPanel from './ReviewResultPanel'
import { useDocumentVersions } from '../hooks/useDocumentVersions'
import type { DocumentType, DocumentVersionDetail, VersionSourceKind } from '../types/document'
import type { JobReview } from '../types/job'
import type { QualityGateResult } from '../types/qualityGate'

export interface DocumentReviewWithVersionsProps
  extends Omit<
    DocumentReviewProps,
    | 'content'
    | 'onContentChange'
    | 'versionPanel'
    | 'isPreviewMode'
    | 'previewLabel'
    | 'onRestorePreview'
    | 'isRestoring'
  > {
  sessionId: string | undefined
  docType: DocumentType
  /** 当前工作稿正文，与 App 的 state 双向绑定。 */
  content: string
  onContentChange: (content: string) => void
  /** 没有任务在跑时为 `true` —— 由它从 false 翻到 true 触发一次列表刷新。 */
  jobIdle: boolean
  /**
   * 会话快照里那份 PRD 审查结论（`App` 的 `prdReviewResult`）。
   *
   * 只在 §6.5 的**老数据回退**档用得上：v1 是迁移导入的、`metadata` 里没有 review 时，
   * 退回这份。传 `undefined` 也能工作（那就是不显示）。
   */
  reviewResult?: JobReview | null
  /**
   * 挂不挂版本功能（默认挂）。
   *
   * `generating-*` 那三屏传 `false`：需求 §十二 要求它们**没有版本侧栏**，
   * 而且生成期间列表本来就没变化、轮询它只是白发请求。
   *
   * ⚠️ 为什么是"同一个组件 + 开关"而不是"两处 JSX"：调用点那一大串 props
   * （title / actions / 四个观测插槽……）有五十多行，复制一份出来维护必然长歪。
   * 关掉开关时**不发任何版本请求、也不渲染侧栏**，与直接用 `DocumentReview` 等价。
   */
  versionsEnabled?: boolean
}

/**
 * 侧栏该显示哪份审查结论（需求 §6.5）。抽成纯函数是为了能单独验这张表。
 *
 * 返回 `null` = 不显示这一段。
 */
export function selectSidebarReview({
  focused,
  focusedKind,
  focusedVersionNo,
  fallback,
}: {
  focused: DocumentVersionDetail | null
  focusedKind: VersionSourceKind | undefined
  focusedVersionNo: number | null
  fallback?: JobReview | null
}): JobReview | null {
  if (!focused) return null
  // checkpoint / restore 出来的新版不是"被审过的稿子"：前者不继承 review，
  // 后者继承的那份针对的是历史正文 —— 两种情况都不该显示。
  if (focusedKind === 'checkpoint' || focusedKind === 'restore') return null

  const fromMetadata = focused.metadata.review
  if (fromMetadata) return fromMetadata
  // 老数据回退：v1 是迁移导入的（source_kind=import），metadata 里没有 review，
  // 而会话快照里那份还在。只在 v1 上退 —— 其它版本没有 review 就是真的没有。
  if (focusedVersionNo === 1) return fallback ?? null
  return null
}

/**
 * 侧栏该显示哪份**结构校验**结论（04 篇）。
 *
 * 与 `selectSidebarReview` 的三处不同，都是刻意的：
 *
 * 1. **不挑 doc_type**：质量报告三份产物都有（审查意见只有 PRD 有）；
 * 2. **不挑 source_kind**：`checkpoint` / `restore` 出来的新版 metadata 里没有 gate
 *    （checkpoint 不继承 metadata），所以自然就没有 —— 不需要额外判据。
 *    但 `optimize` **有**：优化改的就是正文，gate 每次都会覆盖，所以照样显示；
 * 3. **没有"老数据回退"**：v1 是迁移导入的，那时还没有 gate 这个东西，
 *    没有就是真的没有 —— 不拿别处的结论顶替（顶替就是撒谎）。
 */
export function selectSidebarQualityGate(
  focused: DocumentVersionDetail | null,
): QualityGateResult | null {
  const gate = focused?.metadata.quality_gate
  if (!gate || !Array.isArray(gate.checks)) return null
  return gate
}

export default function DocumentReviewWithVersions({
  sessionId,
  docType,
  content,
  onContentChange,
  jobIdle,
  reviewResult,
  versionsEnabled = true,
  ...reviewProps
}: DocumentReviewWithVersionsProps) {
  const versions = useDocumentVersions({
    sessionId,
    docType,
    editorContent: content,
    enabled: versionsEnabled && Boolean(sessionId),
  })

  /** 正在等待详情返回的那一版（列表项显示 loading）。版本号/hook 不必知道它。 */
  const [pendingPreviewId, setPendingPreviewId] = useState<string | null>(null)

  const handleSelectVersion = (versionId: string) => {
    setPendingPreviewId(versionId)
    void versions.openPreview(versionId).finally(() => setPendingPreviewId(null))
  }

  /**
   * 任务从"在跑"变成"空闲"时刷一次列表。
   *
   * ⚠️ 只在**边沿**触发（不是 `jobIdle === true` 就一直刷）：优化是流式的，
   * 盯着 `jobIdle` 的布尔值会让侧栏在整个生成过程中反复请求列表接口。
   */
  const prevJobIdle = useRef(jobIdle)
  useEffect(() => {
    if (!prevJobIdle.current && jobIdle) void versions.refreshList()
    prevJobIdle.current = jobIdle
  }, [jobIdle, versions.refreshList])

  const preview = versions.preview
  const currentItem = versions.versions.find((item) => item.is_current)
  const focused = preview ?? versions.currentDetail
  const review =
    docType === 'prd'
      ? selectSidebarReview({
          focused,
          focusedKind: preview?.source_kind ?? currentItem?.source_kind,
          focusedVersionNo: preview?.version_no ?? versions.currentVersionNo,
          fallback: reviewResult,
        })
      : null
  const qualityGate = selectSidebarQualityGate(focused)

  /**
   * 侧栏底部那一块：审查意见（PRD）在上、质量报告（三产物）在下 —— 04 篇 §8 的位置要求。
   *
   * ⚠️ 只在**确实知道在看哪一版**时才传 `reviewSlot`：`DocumentVersionPanel` 会为它渲染
   * 一圈边框，传个空 fragment 会在侧栏底部留一个空盒子。没有 gate 时给的是
   * `QualityReportEmpty`（一句"暂无"），不是空白 —— 空白会让人以为界面坏了。
   */
  const sidebarExtras = focused ? (
    <>
      {review ? <ReviewResultPanel review={review} /> : null}
      {qualityGate ? (
        <QualityReportPanel result={qualityGate} className={review ? 'mt-2' : undefined} />
      ) : (
        <QualityReportEmpty className={review ? 'mt-2' : undefined} />
      )}
    </>
  ) : undefined

  return (
    <DocumentReview
      {...reviewProps}
      content={preview?.content ?? content}
      isPreviewMode={Boolean(preview)}
      previewLabel={preview ? `预览 v${preview.version_no}` : undefined}
      isRestoring={versions.isRestoring}
      onRestorePreview={() => {
        void versions.restorePreview().then((restored) => {
          // 恢复成功才写回工作稿；失败返回 null（错误已由 hook 记进 error）
          if (restored !== null) onContentChange(restored)
        })
      }}
      onContentChange={(value) => {
        // 预览态是只读的：不许把历史正文写进工作稿
        if (preview) return
        onContentChange(value)
      }}
      versionPanel={
        versionsEnabled ? (
          <DocumentVersionPanel
            versions={versions.versions}
            currentVersionNo={versions.currentVersionNo}
            isLoading={versions.isLoadingList}
            error={versions.error}
            isCheckpointing={versions.isCheckpointing}
            savingNoteVersionId={versions.savingNoteVersionId}
            loadingPreviewId={pendingPreviewId}
            selectedVersionId={preview?.id ?? null}
            // 预览态下不允许另存：那时编辑器里显示的是历史正文，另存会把它当成工作稿固化
            canCheckpoint={!preview && content.trim().length > 0}
            onSelectVersion={handleSelectVersion}
            onCheckpoint={() => void versions.checkpoint()}
            onSaveVersionNote={(versionId, changeNote) =>
              versions.saveVersionNote(versionId, changeNote)
            }
            onRetry={() => void versions.refreshList()}
            reviewSlot={sidebarExtras}
          />
        ) : undefined
      }
    />
  )
}
