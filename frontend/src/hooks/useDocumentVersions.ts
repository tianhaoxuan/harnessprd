/**
 * 文档版本列表 / 预览 / checkpoint / restore / 备注的**状态机**。
 *
 * 它是 `DocumentVersionPanel` 与 `DocumentReview` 之间唯一的那层胶水：
 * 组件只管画，接口只管发，而"现在在预览哪一版、能不能点、点完要不要刷新列表"
 * 全部收在这里。
 *
 * ## 三条刻意的行为约定（都有后果，别顺手改）
 *
 * 1. **点 current 行 = 取消预览**，回到编辑态。current 就是用户手上那份，
 *    把它拉成"只读预览"等于平白禁掉编辑（而需求要的是"点当前行回到当前稿编辑"）。
 * 2. **checkpoint 后 closePreview**：新版本成了 current，而预览态还停在旧版本上会让人
 *    以为"另存没生效"。
 * 3. **saveVersionNote 不 refreshList**：一次备注不该让整个列表闪一下并重新排序。
 *    就地改那一行的 `change_note` 即可 —— 备注不参与排序。
 *
 * ## 过期响应的处理（`epoch`）
 *
 * `sessionId` / `docType` 变了（切换方案、切到另一份产物）之后，**在途的旧请求必须作废**。
 * 不做的话，用户快速切两次就会看到"接口文档的版本列表里出现了 PRD 的版本号" ——
 * 响应本身没错，错在它已经不属于当前这一屏了。
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'

import {
  DocumentApiError,
  checkpointVersion,
  fetchVersionDetail,
  fetchVersionList,
  restoreVersion,
  updateVersionNote,
} from '../services/documentApi'
import type {
  DocumentType,
  DocumentVersionDetail,
  DocumentVersionListItem,
} from '../types/document'

export interface UseDocumentVersionsArgs {
  sessionId: string | undefined
  docType: DocumentType
  /** 编辑器里的当前全文 —— checkpoint 会把它一起发上去。 */
  editorContent: string
  /** `false` 时整个 hook 不发任何请求（generating-* 页、还没有会话时）。 */
  enabled: boolean
}

export interface UseDocumentVersionsReturn {
  versions: DocumentVersionListItem[]
  /** current 那一版的版本号；一版都没有时为 `null`。 */
  currentVersionNo: number | null
  /** 正在预览的版本详情；`null` = 编辑态。 */
  preview: DocumentVersionDetail | null
  previewVersionId: string | null
  /**
   * **current 那一版的详情**（含 `metadata`）。
   *
   * 需求 §6.5 要按"当前聚焦版本"的 `metadata.review` 显示审查意见，而列表项里
   * **没有 metadata** —— 所以列表拉回来之后要再补一次 current 的详情。它有 200 字预览
   * 之外的全文，但只在 current 变化时才变，代价是一次小请求。
   */
  currentDetail: DocumentVersionDetail | null
  isLoadingList: boolean
  isLoadingPreview: boolean
  isCheckpointing: boolean
  isRestoring: boolean
  /** 正在保存备注的那一版 id（按钮据此显示 loading）；没有则为 `null`。 */
  savingNoteVersionId: string | null
  error: string | null
  refreshList: () => Promise<void>
  openPreview: (versionId: string) => Promise<void>
  closePreview: () => void
  checkpoint: () => Promise<void>
  /** 成功返回新 current 的正文（调用方据此刷编辑器）；失败返回 `null`。 */
  restorePreview: () => Promise<string | null>
  saveVersionNote: (versionId: string, changeNote: string) => Promise<void>
}

/** 把异常转成给人看的一句话。非 `DocumentApiError` 也兜住（网络层抛的是 TypeError）。 */
function messageOf(error: unknown): string {
  if (error instanceof DocumentApiError) return error.message
  if (error instanceof Error) return `文档版本请求失败：${error.message}`
  return '文档版本请求失败（未知错误）'
}

export function useDocumentVersions({
  sessionId,
  docType,
  editorContent,
  enabled,
}: UseDocumentVersionsArgs): UseDocumentVersionsReturn {
  const [versions, setVersions] = useState<DocumentVersionListItem[]>([])
  const [preview, setPreview] = useState<DocumentVersionDetail | null>(null)
  const [currentDetail, setCurrentDetail] = useState<DocumentVersionDetail | null>(null)
  const [isLoadingList, setLoadingList] = useState(false)
  const [isLoadingPreview, setLoadingPreview] = useState(false)
  const [isCheckpointing, setCheckpointing] = useState(false)
  const [isRestoring, setRestoring] = useState(false)
  const [savingNoteVersionId, setSavingNoteVersionId] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  /** 生效的"会话 + 产物"组合。它一变，在途请求全部作废。 */
  const scopeKey = `${sessionId ?? ''}|${docType}`
  const epoch = useRef(0)
  const active = enabled && Boolean(sessionId)

  // ---------------------------------------------------------------- 拉列表

  const refreshList = useCallback(async () => {
    if (!enabled || !sessionId) return
    const mine = epoch.current
    setLoadingList(true)
    try {
      const body = await fetchVersionList(sessionId, docType)
      if (mine !== epoch.current) return
      setVersions(body.versions)
      setError(null)

      // current 的详情（§6.5 要它的 metadata）。**失败不报错**：列表本身是好的，
      // 为一份辅助信息把整块侧栏标红不值得；下一次 refreshList 会再试。
      if (body.current_version_id) {
        try {
          const detail = await fetchVersionDetail(sessionId, docType, body.current_version_id)
          if (mine === epoch.current) setCurrentDetail(detail)
        } catch {
          if (mine === epoch.current) setCurrentDetail(null)
        }
      } else if (mine === epoch.current) {
        setCurrentDetail(null)
      }
    } catch (err) {
      if (mine !== epoch.current) return
      setError(messageOf(err))
    } finally {
      if (mine === epoch.current) setLoadingList(false)
    }
  }, [enabled, sessionId, docType])

  // 挂载 / 换会话 / 换产物时：作废在途请求、清空这一屏的旧数据，再拉一次。
  useEffect(() => {
    epoch.current += 1
    setPreview(null)
    setCurrentDetail(null)
    setVersions([])
    setError(null)
    if (!active) return
    void refreshList()
    return () => {
      // 卸载 / 换 scope：让还在飞的响应落地时认出自己已过期
      epoch.current += 1
    }
  }, [scopeKey, active, refreshList])

  // ---------------------------------------------------------------- 预览

  const currentVersionNo = useMemo(
    () => versions.find((item) => item.is_current)?.version_no ?? null,
    [versions],
  )
  const currentVersionId = useMemo(
    () => versions.find((item) => item.is_current)?.id ?? null,
    [versions],
  )

  const closePreview = useCallback(() => setPreview(null), [])

  const openPreview = useCallback(
    async (versionId: string) => {
      // 点 current = 回编辑态（见文件头第 1 条）
      if (!versionId || versionId === currentVersionId) {
        setPreview(null)
        return
      }
      if (!enabled || !sessionId) return
      const mine = epoch.current
      setLoadingPreview(true)
      try {
        const detail = await fetchVersionDetail(sessionId, docType, versionId)
        if (mine !== epoch.current) return
        setPreview(detail)
        setError(null)
      } catch (err) {
        if (mine !== epoch.current) return
        setError(messageOf(err))
      } finally {
        if (mine === epoch.current) setLoadingPreview(false)
      }
    },
    [enabled, sessionId, docType, currentVersionId],
  )

  // ---------------------------------------------------------------- 写操作

  const checkpoint = useCallback(async () => {
    if (!enabled || !sessionId) return
    const mine = epoch.current
    setCheckpointing(true)
    try {
      await checkpointVersion(sessionId, docType, { content: editorContent })
      if (mine !== epoch.current) return
      await refreshList()
      if (mine === epoch.current) {
        setPreview(null)
        setError(null)
      }
    } catch (err) {
      if (mine === epoch.current) setError(messageOf(err))
    } finally {
      if (mine === epoch.current) setCheckpointing(false)
    }
  }, [enabled, sessionId, docType, editorContent, refreshList])

  const restorePreview = useCallback(async (): Promise<string | null> => {
    if (!enabled || !sessionId || !preview) return null
    const mine = epoch.current
    setRestoring(true)
    try {
      const result = await restoreVersion(sessionId, docType, preview.id)
      if (mine !== epoch.current) return null
      await refreshList()
      if (mine !== epoch.current) return null
      setPreview(null)
      setError(null)
      return result.content
    } catch (err) {
      if (mine === epoch.current) setError(messageOf(err))
      return null
    } finally {
      if (mine === epoch.current) setRestoring(false)
    }
  }, [enabled, sessionId, docType, preview, refreshList])

  const saveVersionNote = useCallback(
    async (versionId: string, changeNote: string) => {
      if (!enabled || !sessionId) return
      const mine = epoch.current
      setSavingNoteVersionId(versionId)
      try {
        const result = await updateVersionNote(sessionId, docType, versionId, changeNote)
        if (mine !== epoch.current) return
        // 就地改这一行：备注不参与排序，refreshList 只会让列表闪一下（见文件头第 3 条）。
        // 后端"没有备注就省略这个键"，这里跟着删键而不是写 null —— 同一套语义。
        setVersions((prev) =>
          prev.map((item) => {
            if (item.id !== versionId) return item
            const next = { ...item }
            if (result.change_note) next.change_note = result.change_note
            else delete next.change_note
            return next
          }),
        )
        setCurrentDetail((prev) => {
          if (!prev || prev.id !== versionId) return prev
          const metadata = { ...prev.metadata }
          if (result.change_note) metadata.change_note = result.change_note
          else delete metadata.change_note
          return { ...prev, metadata }
        })
        setError(null)
      } catch (err) {
        if (mine === epoch.current) setError(messageOf(err))
      } finally {
        if (mine === epoch.current) setSavingNoteVersionId(null)
      }
    },
    [enabled, sessionId, docType],
  )

  return {
    versions,
    currentVersionNo,
    preview,
    previewVersionId: preview?.id ?? null,
    currentDetail,
    isLoadingList,
    isLoadingPreview,
    isCheckpointing,
    isRestoring,
    savingNoteVersionId,
    error,
    refreshList,
    openPreview,
    closePreview,
    checkpoint,
    restorePreview,
    saveVersionNote,
  }
}
