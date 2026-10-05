/**
 * 路径选择页（需求 §五.1）—— 新建方案时**先选起点**，再进对应界面。
 *
 * ## 它取代了什么
 *
 * 改造前是 [App.tsx] 里那条**全局入口条**（`entry-mode-bar`）：钉在所有屏之上、
 * 三个按钮随时可切，于是：
 *
 * 1. 新人一进 `/v2` 就要在三个入口之间做一次**没有上下文的判断**（他还没看到表单长什么样）；
 * 2. 主路径用户每次都被另外两个入口陪着 —— 而它们与顶栏那条线性进度条的语义并不一致。
 *
 * 现在入口只在这里选一次；进去之后靠各子界面的「← 返回路径选择」或
 * review 屏的「← 返回导入入口」换路径。
 *
 * ## 纯展示
 *
 * 三个回调由 `App` 传入（那一层才持有 `entryMode` / `formSubView` / `viewState`）。
 * 本组件**不 import 任何服务**、不认识 `EntryMode` 的字面量，只负责画与转发 ——
 * 这样它能离线渲染，也不会把入口语义散到第二个文件里。
 *
 * 文案刻意**不出现** `entryMode` / `structured` 这类内部字段名（需求 §五.1）：
 * 面向产品用户说的是"从零创建 / 导入已有素材"。
 */

import { FileText, Files, FilePlus2, Sparkles } from 'lucide-react'

import { ENTRY_MODES, type EntryMode } from '../../types'

export interface ProjectEntryChooserProps {
  onChoose: (mode: EntryMode) => void
  /** 只读展示用；不传则三张卡都能点。 */
  className?: string
}

/** 卡片的可见文案。`hint` 从 `ENTRY_MODES` 取（唯一来源），不在这里再抄一份。 */
const CARD_COPY: Record<EntryMode, { title: string; detail: string }> = {
  structured: { title: '从零创建 PRD', detail: '填写需求 → AI 澄清 → 生成全流程' },
  'prd-shortcut': { title: '我有 PRD', detail: '粘贴 PRD，从接口文档接着往下走' },
  'prompts-debug': { title: '我有 PRD + 接口文档', detail: '两份都贴进来，直接生成提示词套件' },
}

function hintOf(mode: EntryMode): string {
  return ENTRY_MODES.find((item) => item.id === mode)?.hint ?? ''
}

export default function ProjectEntryChooser({ onChoose, className = '' }: ProjectEntryChooserProps) {
  const primary = CARD_COPY.structured
  const imports: EntryMode[] = ['prd-shortcut', 'prompts-debug']

  return (
    <section
      data-testid="project-entry-chooser"
      className={['rounded-xl border border-slate-200 bg-white p-6 shadow-sm', className].join(' ')}
    >
      <h2 className="text-base font-medium text-slate-800">新建 PRD 方案</h2>
      <p className="mt-1 text-sm text-slate-500">选择本次工作的起点</p>

      <div className="mt-5 grid gap-4 md:grid-cols-2">
        {/* ---------- 主路径：从零创建（深色底，视觉上就是"推荐"） ---------- */}
        <button
          type="button"
          data-testid="entry-choose-structured"
          onClick={() => onChoose('structured')}
          title={hintOf('structured')}
          className="flex flex-col items-start gap-2 rounded-xl bg-slate-900 p-5 text-left transition hover:bg-slate-800 focus:outline-none focus-visible:ring-2 focus-visible:ring-primary-400"
        >
          <FilePlus2 className="h-5 w-5 text-white/80" aria-hidden />
          <span className="text-sm font-medium text-white">{primary.title}</span>
          <span className="text-xs leading-relaxed text-white/70">{primary.detail}</span>
        </button>

        {/* ---------- 导入路径：两张次级卡片 ---------- */}
        <div className="flex flex-col gap-3" data-testid="entry-import-group">
          <div className="flex items-center gap-1.5 text-xs font-medium text-slate-500">
            <Sparkles className="h-3.5 w-3.5" aria-hidden />
            导入已有素材
          </div>
          {imports.map((mode) => (
            <button
              key={mode}
              type="button"
              data-testid={`entry-choose-${mode}`}
              onClick={() => onChoose(mode)}
              title={hintOf(mode)}
              className="flex items-start gap-3 rounded-xl border border-slate-200 bg-white p-4 text-left transition hover:border-primary-300 hover:bg-primary-50/40 focus:outline-none focus-visible:ring-2 focus-visible:ring-primary-300"
            >
              {mode === 'prd-shortcut' ? (
                <FileText className="mt-0.5 h-4 w-4 shrink-0 text-primary-600" aria-hidden />
              ) : (
                <Files className="mt-0.5 h-4 w-4 shrink-0 text-primary-600" aria-hidden />
              )}
              <span className="min-w-0">
                <span className="block text-sm font-medium text-slate-800">
                  {CARD_COPY[mode].title}
                </span>
                <span className="mt-0.5 block text-xs leading-relaxed text-slate-500">
                  {CARD_COPY[mode].detail}
                </span>
              </span>
            </button>
          ))}
        </div>
      </div>
    </section>
  )
}
