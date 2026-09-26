/**
 * RAG 检索结果的展示与确认面板。
 *
 * ## 它在流程里的位置
 *
 * 点「生成接口文档」→ **先检索**（`/retrieve-api-docs-rag`）→ 本面板让用户看清"要给模型喂哪些材料"
 * → 确认后才真正调 `/generate-api-docs-stream`；**取消则一个字都不生成**。
 *
 * ## 与 `SummaryDiffPanel` 的区别（默认勾选策略刻意相反）
 *
 * 那个面板默认**全不勾**：回填是模型对事实的改写，误改会直接进文档，必须逐条表态。
 * 这里是**参考资料**，多带一份规范不会把文档写错、只会让措辞更贴规范，所以默认**全勾**，
 * 用户只需把明显不相关的摘掉。两者都不是"全选=自动接受"，因为这里没有"接受错误事实"的风险。
 *
 * ## 为什么值得看一眼
 *
 * 检索是词法匹配（BM25 近似），**同义改写召回不到、用词撞车又会召回不相关的块**。
 * 不把结果摊开给用户看，"检索到了什么"就是个黑盒 —— 生成出来的接口文档为什么长得怪，
 * 也就无从排查。
 */

import { useMemo, useState } from 'react'
import { BookOpen, Check, ChevronDown, ChevronRight, FileCode2, X } from 'lucide-react'

import type { RagHit } from '../services/api'

interface RagHitsPanelProps {
  hits: RagHit[]
  /** 参与检索的块总数（为 0 说明语料缺失，而不是"没匹配上"） */
  corpusSize: number
  busy?: boolean
  onConfirm: (selected: RagHit[]) => void
  onCancel: () => void
}

const KIND_STYLE: Record<string, string> = {
  规范: 'bg-indigo-100 text-indigo-800',
  历史接口示例: 'bg-amber-100 text-amber-800',
}

/** 正文预览默认收起：命中往往几百上千字，全摊开会把按钮挤到屏幕外。 */
function HitBody({ content }: { content: string }) {
  const [open, setOpen] = useState(false)
  const preview = content.length > 320 ? `${content.slice(0, 320)}…` : content
  return (
    <div className="mt-1">
      <pre className="max-h-40 overflow-auto whitespace-pre-wrap break-words rounded bg-white/70 px-2 py-1 font-mono text-xs leading-relaxed text-slate-600">
        {open ? content : preview}
      </pre>
      {content.length > 320 && (
        <button
          type="button"
          onClick={() => setOpen((value) => !value)}
          className="mt-1 inline-flex items-center gap-1 text-xs text-slate-500 hover:text-slate-700"
        >
          {open ? (
            <ChevronDown className="h-3 w-3" aria-hidden />
          ) : (
            <ChevronRight className="h-3 w-3" aria-hidden />
          )}
          {open ? '收起' : `展开全部（${content.length} 字）`}
        </button>
      )}
    </div>
  )
}

export default function RagHitsPanel({
  hits,
  corpusSize,
  busy = false,
  onConfirm,
  onCancel,
}: RagHitsPanelProps) {
  // 默认全勾（见文件头：参考资料多带一份的风险远低于漏带）
  const [selected, setSelected] = useState<Set<string>>(
    () => new Set(hits.map((hit) => `${hit.source}#${hit.title}`)),
  )

  const grouped = useMemo(() => {
    const map = new Map<string, RagHit[]>()
    for (const hit of hits) {
      const list = map.get(hit.kind) ?? []
      list.push(hit)
      map.set(hit.kind, list)
    }
    return [...map.entries()]
  }, [hits])

  const toggle = (key: string) => {
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return next
    })
  }

  if (hits.length === 0) {
    return (
      <section
        data-testid="rag-hits"
        className="rounded-xl border border-slate-200 bg-white px-4 py-3 text-sm text-slate-600 shadow-sm"
      >
        {corpusSize === 0
          ? '检索语料为空（服务端没读到规范 / 历史示例文件）—— 本次将不带参考资料生成。'
          : '没有检索到与本次接口相关的规范或历史示例 —— 本次将不带参考资料生成。'}
        <button
          type="button"
          onClick={() => onConfirm([])}
          disabled={busy}
          className="ml-3 rounded-lg border border-slate-300 px-3 py-1 text-xs text-slate-600 transition hover:bg-slate-50 disabled:opacity-50"
        >
          继续生成
        </button>
      </section>
    )
  }

  const allSelected = selected.size === hits.length

  return (
    <section
      data-testid="rag-hits"
      className="space-y-3 rounded-xl border border-slate-200 bg-white p-4 shadow-sm"
    >
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        <span className="text-sm font-semibold text-slate-800">
          检索到 {hits.length} 段参考资料（语料共 {corpusSize} 块）
        </span>
        <span className="text-xs text-slate-400">
          确认后才会生成接口文档；取消则什么都不生成
        </span>
        <button
          type="button"
          onClick={() => setSelected(allSelected ? new Set() : new Set(hits.map((hit) => `${hit.source}#${hit.title}`)))}
          disabled={busy}
          className="ml-auto rounded-lg border border-slate-300 px-3 py-1 text-xs text-slate-600 transition hover:bg-slate-50 disabled:opacity-50"
        >
          {allSelected ? '全不选' : '全选'}
        </button>
      </div>

      {grouped.map(([kind, items]) => (
        <div key={kind} className="space-y-2">
          <div className="flex items-center gap-1.5 text-xs font-medium text-slate-600">
            {kind === '规范' ? (
              <BookOpen className="h-3.5 w-3.5" aria-hidden />
            ) : (
              <FileCode2 className="h-3.5 w-3.5" aria-hidden />
            )}
            {kind}（{items.length}）
          </div>
          <ul className="space-y-2">
            {items.map((hit) => {
              const key = `${hit.source}#${hit.title}`
              const checked = selected.has(key)
              return (
                <li
                  key={key}
                  className={[
                    'rounded-lg border px-3 py-2 transition',
                    checked ? 'border-primary-200 bg-primary-50/40' : 'border-slate-200 bg-slate-50/60',
                  ].join(' ')}
                >
                  <label className="flex cursor-pointer items-start gap-2">
                    <input
                      type="checkbox"
                      checked={checked}
                      disabled={busy}
                      onChange={() => toggle(key)}
                      className="mt-0.5 h-3.5 w-3.5 shrink-0"
                      aria-label={`使用${kind}：${hit.title}`}
                    />
                    <div className="min-w-0 flex-1">
                      <div className="flex flex-wrap items-center gap-2">
                        <span
                          className={`rounded px-1.5 py-0.5 text-xs ${KIND_STYLE[hit.kind] ?? 'bg-slate-100 text-slate-700'}`}
                        >
                          {hit.kind}
                        </span>
                        <span className="text-sm text-slate-700">{hit.title}</span>
                        <code className="font-mono text-xs text-slate-400">{hit.source}</code>
                        <span className="ml-auto text-xs text-slate-400">相关度 {hit.score}</span>
                      </div>
                      <HitBody content={hit.content} />
                    </div>
                  </label>
                </li>
              )
            })}
          </ul>
        </div>
      ))}

      <div className="flex flex-wrap items-center gap-2 border-t border-slate-100 pt-3">
        <button
          type="button"
          data-testid="rag-confirm"
          onClick={() =>
            onConfirm(hits.filter((hit) => selected.has(`${hit.source}#${hit.title}`)))
          }
          disabled={busy}
          className="inline-flex items-center gap-1.5 rounded-lg bg-primary-600 px-4 py-2 text-sm font-medium text-white transition hover:bg-primary-700 disabled:cursor-not-allowed disabled:opacity-60"
        >
          <Check className="h-4 w-4" aria-hidden />
          {busy ? '正在生成…' : `确认（${selected.size} 段）并生成接口文档`}
        </button>
        <button
          type="button"
          data-testid="rag-cancel"
          onClick={onCancel}
          disabled={busy}
          className="inline-flex items-center gap-1.5 rounded-lg border border-slate-300 px-4 py-2 text-sm text-slate-600 transition hover:bg-slate-50 disabled:opacity-50"
        >
          <X className="h-4 w-4" aria-hidden />
          取消（不生成）
        </button>
      </div>
    </section>
  )
}
