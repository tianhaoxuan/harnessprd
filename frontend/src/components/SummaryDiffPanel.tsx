/**
 * 结构化摘要的差异确认面板。
 *
 * ## 它解决什么
 *
 * 澄清对话结束后，服务端会把对话里**用户明确说过**的补充与修正回填进结构化摘要
 * （`POST /conversation/sync-summary-from-conversation`）。回填是模型做的，
 * 所以**不能直接生效** —— 先让用户看清"哪一项要变成什么"，确认了才写回去。
 *
 * ## 为什么要有 diff 而不是只给"已更新"
 *
 * 模型回填最常见的两种错法都**看不出来**：
 *
 * 1. 把用户没说过的东西"推断"进摘要（看起来像用户确认过的事实）；
 * 2. 把原本好好的字段改坏（措辞漂移、把数组压成字符串）。
 *
 * 只提示"摘要已更新"，用户没有任何依据判断该不该接受。逐条列出「字段路径 / 变更类型 / 变更前 / 变更后」，
 * 才能让"点确认"变成一次真正的决定。
 *
 * ## 变更类型与颜色
 *
 * | 类型 | 含义 | 颜色 |
 * | --- | --- | --- |
 * | 新增 | 这一项原来没有（或为空），回填后有了值 | 绿 |
 * | 修改 | 原来有值，回填后值变了 | 蓝 |
 * | 删除 | 原来有值，回填后没了 | 红 |
 *
 * ⚠️ **当前服务端的回填规则下「删除」不会出现**：`_merge_summary()` 不写空值、也不删键
 * （要删字段属于人工操作）。这一档留着是因为面板是通用的 —— 哪天规则改成允许清空，
 * 这里不用动；真出现了红色，也说明规则被改过。
 *
 * 刻意**不做的**：不在这里写回后端（那就是两处都在改摘要）、不自动勾选全部
 * （默认全选等于变相自动接受，用户会直接点确认）。
 */

import { useMemo, useState } from 'react'
import { AlertCircle, Check, MinusCircle, Pencil, PlusCircle, X } from 'lucide-react'

/** 一条变更。`path` 是点号路径，数组下标写在方括号里（如 `mvp_features[0].name`）。 */
export interface SummaryChange {
  path: string
  kind: 'added' | 'modified' | 'removed'
  before: unknown
  after: unknown
}

interface SummaryDiffPanelProps {
  changes: SummaryChange[]
  /** 正在提交（调用方在写回摘要 / 开始生成），用来禁用按钮 */
  busy?: boolean
  /** 面板标题里显示的东西，一般是产品名 */
  title?: string
  onConfirm: (accepted: SummaryChange[]) => void
  onReject: () => void
}

const KIND_META: Record<
  SummaryChange['kind'],
  { label: string; icon: typeof PlusCircle; chip: string; card: string; text: string }
> = {
  added: {
    label: '新增',
    icon: PlusCircle,
    chip: 'bg-emerald-100 text-emerald-800',
    card: 'border-emerald-200 bg-emerald-50/60',
    text: 'text-emerald-900',
  },
  modified: {
    label: '修改',
    icon: Pencil,
    chip: 'bg-sky-100 text-sky-800',
    card: 'border-sky-200 bg-sky-50/60',
    text: 'text-sky-900',
  },
  removed: {
    label: '删除',
    icon: MinusCircle,
    chip: 'bg-rose-100 text-rose-800',
    card: 'border-rose-200 bg-rose-50/60',
    text: 'text-rose-900',
  },
}

/** 把任意值渲染成一行可读文本；空值给一个显式的记号，避免"看起来是空字符串"。 */
function renderValue(value: unknown): string {
  if (value === undefined) return '（无）'
  if (value === null) return 'null'
  if (typeof value === 'string') return value.trim() ? value : '（空字符串）'
  if (Array.isArray(value)) {
    if (value.length === 0) return '（空数组）'
    return value.map((item) => (typeof item === 'object' ? JSON.stringify(item) : String(item))).join('；')
  }
  if (typeof value === 'object') {
    const keys = Object.keys(value as Record<string, unknown>)
    if (keys.length === 0) return '（空对象）'
    return JSON.stringify(value)
  }
  return String(value)
}

/**
 * 展开成"逐条变更"。**对象与数组都要摊平**，否则用户看到的是
 * `technical_constraints: {…一大坨…}`，等于没给 diff。
 *
 * 数组按**下标**比对（不做元素级 LCS 匹配）：摘要里的数组短且顺序稳定（功能、页面），
 * 按位比对更可预测；引入序列匹配反而会让"第 2 条功能换了措辞"显示成一大片增删。
 */
export function diffSummaries(before: unknown, after: unknown, prefix = ''): SummaryChange[] {
  const changes: SummaryChange[] = []

  if (Array.isArray(before) || Array.isArray(after)) {
    const left = Array.isArray(before) ? before : []
    const right = Array.isArray(after) ? after : []
    const length = Math.max(left.length, right.length)
    for (let index = 0; index < length; index += 1) {
      const path = `${prefix}[${index}]`
      if (index >= left.length) {
        changes.push({ path, kind: 'added', before: undefined, after: right[index] })
      } else if (index >= right.length) {
        changes.push({ path, kind: 'removed', before: left[index], after: undefined })
      } else {
        changes.push(...diffSummaries(left[index], right[index], path))
      }
    }
    return changes
  }

  const beforeIsObject = typeof before === 'object' && before !== null
  const afterIsObject = typeof after === 'object' && after !== null
  if (beforeIsObject || afterIsObject) {
    const left = (beforeIsObject ? before : {}) as Record<string, unknown>
    const right = (afterIsObject ? after : {}) as Record<string, unknown>
    const keys = [...new Set([...Object.keys(left), ...Object.keys(right)])]
    for (const key of keys) {
      const path = prefix ? `${prefix}.${key}` : key
      if (!(key in left)) {
        changes.push({ path, kind: 'added', before: undefined, after: right[key] })
      } else if (!(key in right)) {
        changes.push({ path, kind: 'removed', before: left[key], after: undefined })
      } else {
        changes.push(...diffSummaries(left[key], right[key], path))
      }
    }
    return changes
  }

  if (before !== after) {
    changes.push({ path: prefix, kind: 'modified', before, after })
  }
  return changes
}

export default function SummaryDiffPanel({
  changes,
  busy = false,
  title,
  onConfirm,
  onReject,
}: SummaryDiffPanelProps) {
  // 默认**全不选**：全选等于变相自动接受，用户会直接点确认，diff 就白做了
  const [accepted, setAccepted] = useState<Set<string>>(new Set())

  const counts = useMemo(() => {
    const result = { added: 0, modified: 0, removed: 0 }
    for (const change of changes) result[change.kind] += 1
    return result
  }, [changes])

  const toggle = (path: string) => {
    setAccepted((prev) => {
      const next = new Set(prev)
      if (next.has(path)) next.delete(path)
      else next.add(path)
      return next
    })
  }

  const allAccepted = changes.length > 0 && accepted.size === changes.length
  const toggleAll = () => {
    setAccepted(allAccepted ? new Set() : new Set(changes.map((change) => change.path)))
  }

  if (changes.length === 0) {
    return (
      <section
        data-testid="summary-diff"
        className="rounded-xl border border-slate-200 bg-white px-4 py-3 text-sm text-slate-600 shadow-sm"
      >
        对话里没有发现需要回填的内容 —— 摘要保持原样，可以直接生成。
        <button
          type="button"
          onClick={onReject}
          disabled={busy}
          className="ml-3 rounded-lg border border-slate-300 px-3 py-1 text-xs text-slate-600 transition hover:bg-slate-50 disabled:opacity-50"
        >
          知道了
        </button>
      </section>
    )
  }

  return (
    <section
      data-testid="summary-diff"
      className="space-y-3 rounded-xl border border-slate-200 bg-white p-4 shadow-sm"
    >
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        <span className="text-sm font-semibold text-slate-800">
          对话里发现 {changes.length} 处可以回填进结构化摘要
          {title ? `（${title}）` : ''}
        </span>
        <span className="text-xs text-slate-400">
          默认全不勾选 —— 只勾你确认要写回去的
        </span>
        <button
          type="button"
          onClick={toggleAll}
          disabled={busy}
          className="ml-auto rounded-lg border border-slate-300 px-3 py-1 text-xs text-slate-600 transition hover:bg-slate-50 disabled:opacity-50"
        >
          {allAccepted ? '全不选' : '全选'}
        </button>
      </div>

      <div className="flex flex-wrap gap-2 text-xs">
        {counts.added > 0 && (
          <span className="rounded bg-emerald-100 px-2 py-0.5 text-emerald-800">
            新增 {counts.added}
          </span>
        )}
        {counts.modified > 0 && (
          <span className="rounded bg-sky-100 px-2 py-0.5 text-sky-800">修改 {counts.modified}</span>
        )}
        {counts.removed > 0 && (
          <span className="rounded bg-rose-100 px-2 py-0.5 text-rose-800">删除 {counts.removed}</span>
        )}
      </div>

      <ul className="max-h-[50vh] space-y-2 overflow-auto pr-1">
        {changes.map((change) => {
          const meta = KIND_META[change.kind]
          const Icon = meta.icon
          const checked = accepted.has(change.path)
          return (
            <li
              key={change.path}
              className={[
                'rounded-lg border px-3 py-2 transition',
                meta.card,
                checked ? 'ring-2 ring-primary-300' : '',
              ].join(' ')}
            >
              <label className="flex cursor-pointer items-start gap-2">
                <input
                  type="checkbox"
                  checked={checked}
                  disabled={busy}
                  onChange={() => toggle(change.path)}
                  className="mt-0.5 h-3.5 w-3.5 shrink-0"
                  aria-label={`接受对 ${change.path} 的${meta.label}`}
                />
                <div className="min-w-0 flex-1">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className={`rounded px-1.5 py-0.5 text-xs font-medium ${meta.chip}`}>
                      <Icon className="mr-1 inline h-3 w-3 align-[-2px]" aria-hidden />
                      {meta.label}
                    </span>
                    <code className={`font-mono text-xs ${meta.text}`}>{change.path}</code>
                  </div>
                  <div className="mt-1 grid gap-1 sm:grid-cols-2">
                    <div className="text-xs text-slate-500">
                      <span className="text-slate-400">变更前：</span>
                      <span className="break-words text-slate-600">{renderValue(change.before)}</span>
                    </div>
                    <div className="text-xs text-slate-500">
                      <span className="text-slate-400">变更后：</span>
                      <span className={`break-words ${meta.text}`}>{renderValue(change.after)}</span>
                    </div>
                  </div>
                </div>
              </label>
            </li>
          )
        })}
      </ul>

      <p className="flex items-start gap-1.5 text-xs text-slate-400">
        <AlertCircle className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden />
        只勾选你确认的项。没勾的保持原值；拒绝则整份摘要不变。
      </p>

      <div className="flex flex-wrap items-center gap-2 border-t border-slate-100 pt-3">
        <button
          type="button"
          data-testid="summary-diff-confirm"
          onClick={() => onConfirm(changes.filter((change) => accepted.has(change.path)))}
          disabled={busy || accepted.size === 0}
          className="inline-flex items-center gap-1.5 rounded-lg bg-primary-600 px-4 py-2 text-sm font-medium text-white transition hover:bg-primary-700 disabled:cursor-not-allowed disabled:opacity-60"
        >
          <Check className="h-4 w-4" aria-hidden />
          {busy ? '正在处理…' : `确认更新（${accepted.size} 项）并生成 PRD`}
        </button>
        <button
          type="button"
          data-testid="summary-diff-reject"
          onClick={onReject}
          disabled={busy}
          className="inline-flex items-center gap-1.5 rounded-lg border border-slate-300 px-4 py-2 text-sm text-slate-600 transition hover:bg-slate-50 disabled:opacity-50"
        >
          <X className="h-4 w-4" aria-hidden />
          拒绝更改（用原摘要生成）
        </button>
      </div>
    </section>
  )
}
