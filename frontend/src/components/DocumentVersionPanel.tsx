/**
 * 版本历史侧栏（需求 §六）：列表、来源标签、时间、用户备注，以及「保存为新版本」。
 *
 * ## 它**不做**的事（都是刻意的）
 *
 * | 不做 | 谁做 | 为什么 |
 * | --- | --- | --- |
 * | 调接口 | `useDocumentVersions`（通过 props 传进来的回调） | 组件保持纯展示，可离线渲染 |
 * | 判断"能不能另存" | 调用方（`canCheckpoint`） | 本组件看不到编辑器里的正文 |
 * | 展示 `content_preview` | 谁都不做 | 后端说它不是摘要；需求 §一 的"不展示"清单里点名了它 |
 *
 * ## 备注与 checkpoint 是**两件事**（需求 §六 的"分离"）
 *
 * checkpoint 只负责"把这一刻的正文固化成新版本"，**不收集备注** —— 那时用户还没法
 * 引用"第几版"。想给某一版加说明，事后点该版右侧的「备注」按钮写 `change_note`。
 * 任意版本都能备注，包括 current。
 *
 * ## 响应式
 *
 * `md` 以上固定挂在 `DocumentReview` 右侧（`w-72`）；窄屏折叠成「版本历史」一行，
 * 点开才展开。用**同一份 DOM** + 两套 class（`hidden` / `md:flex`），不是两个布局 ——
 * 两份 DOM 意味着两份要同步维护的列表。
 */

import { useState } from 'react'
import type { ReactNode } from 'react'
import { ChevronDown, ChevronRight, Loader2, MessageSquare, Plus } from 'lucide-react'

import {
  VERSION_SOURCE_LABEL,
  hasChangeNote,
  type DocumentVersionListItem,
} from '../types/document'

export interface DocumentVersionPanelProps {
  versions: DocumentVersionListItem[]
  currentVersionNo: number | null
  isLoading: boolean
  /** 最近一次失败的原因（列表 / 预览 / checkpoint / 备注**共用**一个位置） */
  error: string | null
  isCheckpointing: boolean
  /** 正在保存备注的那一版 id */
  savingNoteVersionId: string | null
  /** 点在途的那一版（列表项显示 loading） */
  loadingPreviewId?: string | null
  onSelectVersion: (versionId: string) => void
  onCheckpoint: () => void
  onSaveVersionNote: (versionId: string, changeNote: string) => void | Promise<void>
  onRetry: () => void
  /** 编辑器里有正文才允许另存（空正文后端会返 400"无内容可保存"）。 */
  canCheckpoint: boolean
  /** 正在预览的那一版（高亮它）。`null` = 编辑态。 */
  selectedVersionId?: string | null
  /** 侧栏下半段：PRD 的审查意见（仅 `docType === 'prd'` 由上层传）。 */
  reviewSlot?: ReactNode
  className?: string
}

/** `2026-10-05T10:11:06.279+00:00` → `13:20`（当天）或 `10-05 13:20`（不是当天）。 */
function formatStamp(iso: string): string {
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return ''
  const time = date.toLocaleTimeString('zh-CN', {
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  })
  const now = new Date()
  const sameDay =
    date.getFullYear() === now.getFullYear() &&
    date.getMonth() === now.getMonth() &&
    date.getDate() === now.getDate()
  if (sameDay) return time
  const day = `${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`
  return `${day} ${time}`
}

export default function DocumentVersionPanel({
  versions,
  currentVersionNo,
  isLoading,
  error,
  isCheckpointing,
  savingNoteVersionId,
  loadingPreviewId = null,
  onSelectVersion,
  onCheckpoint,
  onSaveVersionNote,
  onRetry,
  canCheckpoint,
  selectedVersionId = null,
  reviewSlot,
  className = '',
}: DocumentVersionPanelProps) {
  const [mobileOpen, setMobileOpen] = useState(false)
  /** 哪一版正在编辑备注（`null` = 都没在编辑）。一次只开一个，避免几行都展开。 */
  const [noteEditingId, setNoteEditingId] = useState<string | null>(null)
  const [noteDraft, setNoteDraft] = useState('')

  const busy = isCheckpointing

  function beginNote(version: DocumentVersionListItem) {
    setNoteEditingId(version.id)
    setNoteDraft(version.change_note ?? '')
  }

  async function commitNote(versionId: string) {
    // `onSaveVersionNote` 可能是 async（真接口）也可能是同步的（测试替身），
    // `Promise.resolve` 把两种都收成同一条等待路径。
    await Promise.resolve(onSaveVersionNote(versionId, noteDraft))
    setNoteEditingId(null)
  }

  return (
    <aside
      data-testid="document-version-panel"
      className={[
        'flex shrink-0 flex-col overflow-hidden rounded-xl border border-slate-200 bg-white shadow-sm',
        'md:w-72',
        className,
      ].join(' ')}
    >
      {/* ---------- 头部：标题 + 保存为新版本（窄屏点它展开列表） ---------- */}
      <div className="flex items-center gap-2 border-b border-slate-100 px-3 py-2.5">
        <button
          type="button"
          data-testid="version-panel-toggle"
          onClick={() => setMobileOpen((prev) => !prev)}
          aria-expanded={mobileOpen}
          className="inline-flex items-center gap-1 text-sm font-medium text-slate-800 md:pointer-events-none"
        >
          {mobileOpen ? (
            <ChevronDown className="h-3.5 w-3.5 md:hidden" aria-hidden />
          ) : (
            <ChevronRight className="h-3.5 w-3.5 md:hidden" aria-hidden />
          )}
          版本历史
          {currentVersionNo !== null && (
            <span
              data-testid="version-current-no"
              className="rounded bg-slate-100 px-1.5 py-0.5 text-xs tabular-nums text-slate-500"
              title="当前生效的版本 —— 编辑与自动保存都落在它上面"
            >
              当前 v{currentVersionNo}
            </span>
          )}
        </button>
        <button
          type="button"
          data-testid="version-checkpoint"
          onClick={onCheckpoint}
          disabled={busy || !canCheckpoint}
          title={
            canCheckpoint
              ? '把编辑器里这一刻的正文固化成新版本（历史版本一律保留）'
              : '编辑器里还没有正文，没有可保存的内容'
          }
          className="ml-auto inline-flex items-center gap-1 rounded-lg border border-slate-300 px-2 py-1 text-xs text-slate-700 transition hover:bg-slate-50 disabled:cursor-not-allowed disabled:text-slate-400"
        >
          {isCheckpointing ? (
            <Loader2 className="h-3 w-3 animate-spin" aria-hidden />
          ) : (
            <Plus className="h-3 w-3" aria-hidden />
          )}
          保存为新版本
        </button>
      </div>

      {/* ---------- 列表（窄屏折叠） ---------- */}
      <div
        className={[
          mobileOpen ? 'flex' : 'hidden',
          'min-h-0 flex-1 flex-col overflow-y-auto md:flex',
        ].join(' ')}
      >
        {error && (
          <div
            role="alert"
            className="mx-3 mt-2 flex items-start gap-2 rounded-lg border border-rose-200 bg-rose-50 px-2 py-1.5 text-xs text-rose-700"
          >
            <span className="flex-1">{error}</span>
            <button
              type="button"
              data-testid="version-retry"
              onClick={onRetry}
              className="shrink-0 rounded border border-rose-300 px-1.5 py-0.5 text-xs text-rose-700 transition hover:bg-rose-100"
            >
              重试
            </button>
          </div>
        )}

        {isLoading && versions.length === 0 && (
          <p className="px-3 py-3 text-xs text-slate-400">正在载入版本…</p>
        )}

        {!isLoading && versions.length === 0 && !error && (
          <p data-testid="version-empty" className="px-3 py-3 text-xs text-slate-400">
            暂无版本历史。生成或点「保存为新版本」之后会出现在这里。
          </p>
        )}

        <ul className="flex flex-col">
          {versions.map((version) => {
            const isCurrent = version.is_current
            const isSelected = selectedVersionId === version.id
            const isPreviewLoading = loadingPreviewId === version.id
            const isSavingNote = savingNoteVersionId === version.id
            const editing = noteEditingId === version.id
            const note = hasChangeNote(version) ? version.change_note : null

            return (
              <li
                key={version.id}
                data-testid="version-item"
                data-version-no={version.version_no}
                className={[
                  'border-b border-slate-100 last:border-b-0',
                  isSelected ? 'bg-primary-50' : '',
                ].join(' ')}
              >
                {/* 主区域：点它进预览。备注按钮在**外面**（见下面），
                    所以点备注不会顺带触发预览 —— 不靠 stopPropagation 兜。 */}
                <div className="flex items-stretch">
                  <button
                    type="button"
                    data-testid="version-select"
                    onClick={() => onSelectVersion(version.id)}
                    className="flex min-w-0 flex-1 items-start gap-2 px-3 py-2 text-left transition hover:bg-slate-50"
                  >
                    <span
                      aria-hidden
                      className={[
                        'mt-1 h-1.5 w-1.5 shrink-0 rounded-full',
                        isCurrent ? 'bg-primary-600' : 'bg-slate-300',
                      ].join(' ')}
                    />
                    <span className="min-w-0 flex-1">
                      <span className="flex flex-wrap items-center gap-x-1.5 gap-y-0.5">
                        <span className="text-xs font-medium tabular-nums text-slate-800">
                          v{version.version_no}
                        </span>
                        {isCurrent && (
                          <span className="rounded bg-primary-100 px-1 py-0.5 text-[10px] text-primary-800">
                            当前
                          </span>
                        )}
                        <span className="text-xs text-slate-500">
                          {VERSION_SOURCE_LABEL[version.source_kind] ?? version.source_kind}
                        </span>
                        <span className="text-xs tabular-nums text-slate-400">
                          · {formatStamp(version.created_at)}
                        </span>
                        {isPreviewLoading && (
                          <Loader2 className="h-3 w-3 animate-spin text-slate-400" aria-hidden />
                        )}
                      </span>
                      {/* 已保存的备注。不展示 content_preview（需求 §一 的"不展示"清单）。 */}
                      {note && (
                        <span
                          data-testid="version-note"
                          className="mt-0.5 line-clamp-2 block text-xs text-slate-500"
                        >
                          {note}
                        </span>
                      )}
                    </span>
                  </button>

                  <div className="flex items-start py-2 pr-2">
                    <button
                      type="button"
                      data-testid="version-note-edit"
                      onClick={() => (editing ? setNoteEditingId(null) : beginNote(version))}
                      title={note ? '修改这一版的备注' : '给这一版补一句备注'}
                      className="inline-flex items-center gap-1 rounded border border-slate-300 px-1.5 py-0.5 text-[11px] text-slate-600 transition hover:bg-slate-50"
                    >
                      <MessageSquare className="h-3 w-3" aria-hidden />
                      备注
                    </button>
                  </div>
                </div>

                {/* 备注编辑区（在版本块内展开） */}
                {editing && (
                  <div className="px-3 pb-2.5">
                    <textarea
                      data-testid="version-note-input"
                      value={noteDraft}
                      rows={2}
                      autoFocus
                      placeholder="例如「定稿前备份」「这版是给客户看的那份」"
                      onChange={(event) => setNoteDraft(event.target.value)}
                      className="w-full resize-y rounded border border-slate-300 bg-white px-2 py-1 text-xs leading-relaxed text-slate-700 outline-none focus:border-primary-400 focus:ring-2 focus:ring-primary-100"
                    />
                    <div className="mt-1 flex items-center gap-2">
                      <button
                        type="button"
                        data-testid="version-note-save"
                        disabled={isSavingNote}
                        onClick={() => void commitNote(version.id)}
                        className="inline-flex items-center gap-1 rounded bg-primary-600 px-2 py-0.5 text-[11px] font-medium text-white transition hover:bg-primary-700 disabled:cursor-not-allowed disabled:bg-slate-300"
                      >
                        {isSavingNote && <Loader2 className="h-3 w-3 animate-spin" aria-hidden />}
                        保存
                      </button>
                      <button
                        type="button"
                        onClick={() => setNoteEditingId(null)}
                        className="rounded border border-slate-300 px-2 py-0.5 text-[11px] text-slate-600 transition hover:bg-slate-50"
                      >
                        取消
                      </button>
                      <span className="text-[11px] text-slate-400">清空内容再保存 = 删除备注</span>
                    </div>
                  </div>
                )}
              </li>
            )
          })}
        </ul>

        {/* 侧栏下半段：PRD 的审查意见（需求 §6.5）。由上层决定传不传。 */}
        {reviewSlot && <div className="mt-1 border-t border-slate-100 px-3 py-2">{reviewSlot}</div>}
      </div>
    </aside>
  )
}
