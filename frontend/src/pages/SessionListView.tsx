/**
 * 方案列表页（`/v2/list`）。
 *
 * 数据来自 `sessionService.listSessions()`（后端 `/api/session/list`，只回摘要、按更新时间倒序）。
 * 删除走 `deleteSession()`，**先弹二次确认**再动手 —— 方案里是用户聊了很久才攒出来的内容，
 * 点错一下就没了的代价太大。
 *
 * ## 两个刻意的处理
 *
 * 1. **加载失败与"真的没有方案"在界面上分开说。** `sessionService` 的约定是失败不抛异常、
 *    只 `console.error`（那是上一层的刻意设计：存储不可用不该打断页面）。代价是调用方拿到
 *    空数组时分不清"没有数据"和"没取到"。这里用 `loaded` 标记区分：取过一次且为空 →
 *    "还没有方案"；取过一次但后端明显不通 → 额外提示去看控制台，而不是让人对着空列表发呆。
 * 2. **更新时间用本地时区显示**（`toLocaleString`）。后端存的是 UTC ISO8601，
 *    直接显示会让人以为方案是八小时前建的。
 */
import { useCallback, useEffect, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'

import { ENTRY_MODES } from '../types'
import { deleteSession, listSessions, type SessionSummary } from '../services/sessionService'

/** 阶段 → 中文。**与 `types/index.ts` 的 `ViewState` 一一对应**（少一个就会出现空白格）。 */
const STAGE_LABELS: Record<string, string> = {
  form: '填写表单',
  chatting: 'AI 对话',
  'generating-prd': 'PRD 生成中',
  'review-prd': 'PRD 待审核',
  'generating-api-docs': '接口文档生成中',
  'review-api-docs': '接口文档待审核',
  'generating-prompts': '提示词生成中',
  'review-prompts': '提示词待审核',
  done: '已完成',
}

/** 入口模式 → 中文。从 `ENTRY_MODES` 取，**不另抄一份**（抄了就会漂移）。 */
function entryLabel(mode: string): string {
  return ENTRY_MODES.find((item) => item.id === mode)?.label ?? mode
}

function stageLabel(stage: string): string {
  return STAGE_LABELS[stage] ?? stage
}

/** `updated_at` 是 UTC ISO8601；转成本地时间给人看。解析不了就原样显示，不崩。 */
function formatTime(value: string): string {
  const parsed = new Date(value)
  if (Number.isNaN(parsed.getTime())) return value
  return parsed.toLocaleString('zh-CN', { hour12: false })
}

export default function SessionListView() {
  const navigate = useNavigate()
  const [items, setItems] = useState<SessionSummary[]>([])
  const [loading, setLoading] = useState(true)
  const [loaded, setLoaded] = useState(false)
  const [busyId, setBusyId] = useState<string | null>(null)
  const [pendingDelete, setPendingDelete] = useState<SessionSummary | null>(null)

  const refresh = useCallback(async () => {
    setLoading(true)
    const rows = await listSessions()
    setItems(rows)
    setLoading(false)
    setLoaded(true)
  }, [])

  useEffect(() => {
    void refresh()
  }, [refresh])

  const confirmDelete = useCallback(async () => {
    if (!pendingDelete) return
    const target = pendingDelete
    setBusyId(target.id)
    const ok = await deleteSession(target.id)
    setBusyId(null)
    setPendingDelete(null)
    // 删完**刷新列表**（而不是本地 filter 掉）：以后端为准，也不会出现"删了但还在"的假象
    if (ok) void refresh()
    else window.alert('删除失败 —— 这条方案可能已经不在了，刷新后可见。')
  }, [pendingDelete, refresh])

  return (
    <div className="mx-auto flex min-h-full max-w-3xl flex-col gap-5 p-8">
      <header className="flex flex-wrap items-center gap-3">
        <div className="flex-1">
          <h1 className="text-xl font-semibold text-slate-900">我的方案</h1>
          <p className="text-sm text-slate-500">按最近更新时间排序</p>
        </div>
        <Link
          to="/v2"
          data-testid="list-back-workbench"
          className="rounded-lg border border-slate-300 px-3 py-1.5 text-xs text-slate-600 transition hover:bg-slate-50"
        >
          返回工作台
        </Link>
        <Link
          to="/v2"
          data-testid="list-new"
          className="rounded-lg bg-primary-600 px-3 py-1.5 text-xs font-medium text-white transition hover:bg-primary-700"
        >
          新建方案
        </Link>
      </header>

      {loading && !loaded ? (
        <p className="rounded-xl border border-slate-200 bg-white p-6 text-sm text-slate-500 shadow-sm">
          正在读取方案…
        </p>
      ) : items.length === 0 ? (
        <div
          data-testid="list-empty"
          className="flex flex-col items-center gap-3 rounded-xl border border-dashed border-slate-300 bg-white p-10 text-center shadow-sm"
        >
          <p className="text-sm text-slate-600">还没有保存过方案。</p>
          <p className="text-xs text-slate-400">
            方案是工作台的整份状态（表单、对话、三份产物）。在工作台里做完一轮，它就会被存下来。
          </p>
          {/* 取不到数据时也走这里：说清"可能是没连上"，而不是让人对着空列表猜 */}
          <p className="text-xs text-slate-400">
            如果刚刚明明保存过，请看浏览器控制台 —— 会话服务失败时只打日志、不会弹错。
          </p>
          <Link
            to="/v2"
            className="mt-1 rounded-lg bg-primary-600 px-4 py-2 text-sm font-medium text-white transition hover:bg-primary-700"
          >
            新建方案
          </Link>
        </div>
      ) : (
        <ul className="flex flex-col gap-3">
          {items.map((item) => (
            <li
              key={item.id}
              data-testid={`session-row-${item.id}`}
              className="flex flex-wrap items-center gap-3 rounded-xl border border-slate-200 bg-white p-4 shadow-sm"
            >
              <div className="min-w-0 flex-1">
                <p className="truncate text-sm font-medium text-slate-800" title={item.title}>
                  {item.title || '（无标题）'}
                </p>
                <p className="mt-1 flex flex-wrap items-center gap-2 text-xs text-slate-500">
                  <span className="rounded bg-slate-100 px-1.5 py-0.5">{entryLabel(item.entry_mode)}</span>
                  <span className="rounded bg-primary-50 px-1.5 py-0.5 text-primary-700">
                    {stageLabel(item.current_stage)}
                  </span>
                  <span>更新于 {formatTime(item.updated_at)}</span>
                </p>
              </div>
              <div className="flex items-center gap-2">
                <button
                  type="button"
                  data-testid={`session-edit-${item.id}`}
                  onClick={() => navigate(`/v2/${item.id}`)}
                  className="rounded-lg bg-primary-600 px-3 py-1.5 text-xs font-medium text-white transition hover:bg-primary-700"
                >
                  继续编辑
                </button>
                <button
                  type="button"
                  data-testid={`session-delete-${item.id}`}
                  onClick={() => setPendingDelete(item)}
                  disabled={busyId === item.id}
                  className="rounded-lg border border-rose-200 px-3 py-1.5 text-xs text-rose-600 transition hover:bg-rose-50 disabled:cursor-not-allowed disabled:text-slate-300"
                >
                  {busyId === item.id ? '删除中…' : '删除'}
                </button>
              </div>
            </li>
          ))}
        </ul>
      )}

      {/* 二次确认：方案是攒出来的，删掉没有回收站 */}
      {pendingDelete && (
        <div
          data-testid="delete-confirm"
          className="fixed inset-0 z-20 flex items-center justify-center bg-slate-900/30 p-4"
          role="dialog"
          aria-modal="true"
        >
          <div className="w-full max-w-sm rounded-xl border border-slate-200 bg-white p-5 shadow-lg">
            <h2 className="text-sm font-medium text-slate-900">删除这条方案？</h2>
            <p className="mt-2 text-xs text-slate-500">
              「{pendingDelete.title || '（无标题）'}」会被永久删除，没有回收站，也无法撤销。
            </p>
            <div className="mt-4 flex justify-end gap-2">
              <button
                type="button"
                data-testid="delete-cancel"
                onClick={() => setPendingDelete(null)}
                className="rounded-lg border border-slate-300 px-3 py-1.5 text-xs text-slate-600 transition hover:bg-slate-50"
              >
                取消
              </button>
              <button
                type="button"
                data-testid="delete-confirm-ok"
                onClick={() => void confirmDelete()}
                disabled={busyId !== null}
                className="rounded-lg bg-rose-600 px-3 py-1.5 text-xs font-medium text-white transition hover:bg-rose-700 disabled:cursor-not-allowed disabled:bg-slate-300"
              >
                {busyId !== null ? '删除中…' : '确认删除'}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
