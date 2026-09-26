import { useEffect, useMemo, useRef, useState } from 'react'
import {
  AlertCircle,
  Check,
  Copy,
  Loader2,
  Lock,
  Pencil,
  Sparkles,
} from 'lucide-react'

import type { DocStatus } from '../types'
import MarkdownBody from './Markdown'

/**
 * 文档审核面板：生成中只读流式预览，生成后可编辑，支持 AI 优化与一键复制。
 *
 * 出处：`docs/状态数据设计.md` §2.3（`DocumentState`）、`docs/会话持久化方案.md` §7.4、
 * F4.8 / F4.12 / F8.6。
 *
 * ## 这个组件**不做**的事（都是刻意的）
 *
 * | 不做 | 谁做 | 为什么 |
 * | --- | --- | --- |
 * | 调接口 | 调用方（`onOptimize` / `actions`） | 组件要保持可测、可离线渲染；接口形状（SSE）与重试策略属于编排层 |
 * | 决定"通过 / 打回" | 调用方（`actions`） | 审核动作是**状态变更**，只能走 `POST /sessions/{id}/events`。组件给出按钮位置，语义由调用方定 |
 * | 持久化 | 调用方 | 同上；组件只把编辑结果通过 `onContentChange` 报上去 |
 *
 * ## 生成中为什么用 Markdown 而不是 textarea
 *
 * 产物是 Markdown，生成中把它原始文本摆出来就是给用户看 `## 第 5 章` 这种记号 ——
 * 与对话侧那个"直接渲染原始 JSON"的坑（`HANDOFF.md` §4 坑 #15）是同一类错误。
 * 所以 `generating` 期间一律走只读的 Markdown 渲染 + 光标。
 *
 * ⚠️ 一个已知的小别扭：Markdown 渲染出来的标题在生成过程中会**逐字符跳动**
 * （`##` 加半个标题时已按标题排版）。产物是分片生成的，无法避免；
 * 换成纯文本预览又会牺牲最终观感。取舍是"宁愿跳、也要看得懂"。
 */

// ---------------------------------------------------------------- 节解析

/** 正文里的一个标题及其所属正文。 */
export interface DocumentSection {
  /** **标题原文**（含 `#` 前缀，已去掉行尾空白）。
   *  ⚠️ 它会**逐字**作为 `optimize-document-stream` 的 `section` 参数 ——
   *  服务端要求"与文档里的标题逐字一致"，所以不要在这里 trim 掉 `#`。 */
  heading: string
  /** 标题层级（`#` 的个数） */
  level: number
  /** 标题行之后、到下一个**同级或更高级**标题为止的正文（已 trim） */
  body: string
}

const ATX_HEADING = /^(#{1,6})\s+(\S.*)$/
const CODE_FENCE = /^\s*(`{3,}|~{3,})/

/**
 * 找出所有标题行的行号与层级，**跳过代码块内部**。
 *
 * `splitSections` 与 `replaceSection` 共用这一份 —— 两处各自实现围栏配对的后果，
 * 就是"切出来的节"和"换掉的节"边界不一致。
 */
function scanHeadings(lines: string[]): { index: number; level: number }[] {
  const headings: { index: number; level: number }[] = []
  // 围栏状态：记住开栏用的是 ` 还是 ~，只有同种字符才能关栏（CommonMark 规则）
  let fence: string | null = null

  for (let index = 0; index < lines.length; index += 1) {
    const line = lines[index]
    const fenceMatch = CODE_FENCE.exec(line)
    if (fenceMatch) {
      const marker = fenceMatch[1][0]
      if (fence === null) fence = marker
      else if (fence === marker) fence = null
      continue
    }
    if (fence !== null) continue // 代码块内部，忽略一切标题语法

    const headingMatch = ATX_HEADING.exec(line)
    if (headingMatch) headings.push({ index, level: headingMatch[1].length })
  }
  return headings
}

/**
 * 把 Markdown 拆成「标题 + 正文」若干节。
 *
 * 存在的理由：AI 优化（F8.6）**只改一节**，需要把这个节的标题和现有正文一起发过去。
 * 不发现有正文的后果很严重 —— 模型只能凭反馈从零重写，**用户手改过的内容全丢**
 * （`会话持久化方案` §7.4 要求重生成前对已手改内容二次确认）。
 *
 * ⚠️ **必须跳过代码块。** 产物里大量出现 ```bash / ```sql，而 bash 里的注释就是 `#`：
 *
 *     ```bash
 *     # 安装依赖      ← 不跳过的话，这一行会被当成一级标题，切出一个假小节
 *     npm install
 *     ```
 *
 * 实测这类文档里 `#` 注释很常见，所以围栏配对是必需的，不是洁癖。
 *
 * @returns 按出现顺序排列的各节；没有标题时返回空数组（调用方需处理这种情况）。
 */
export function splitSections(markdown: string): DocumentSection[] {
  const lines = markdown.split('\n')
  const headings = scanHeadings(lines)

  return headings.map((heading, position) => {
    // 下一个**同级或更高级**的标题就是本节的边界（更深的是子节，归本节所有）
    const next = headings.slice(position + 1).find((item) => item.level <= heading.level)
    const end = next ? next.index : lines.length
    return {
      heading: lines[heading.index].trimEnd(),
      level: heading.level,
      body: lines.slice(heading.index + 1, end).join('\n').trim(),
    }
  })
}

/**
 * 用 `replacement` 换掉 `heading` 那一节（标题行到下一个同级/更高级标题之前）。
 *
 * 存在的理由：`optimize-document-stream` **只返回被修订的那一节**，不是整篇文档。
 * 所以拿到结果必须拼回去，否则界面上会出现两份文档、或者用户刚改的其它节被整篇覆盖。
 *
 * ⚠️ 服务端契约是「只输出修订后的『{section}』」，也就是**含标题**。但模型**偶尔只给正文** ——
 * 实测抓到的后果是：标题行被一起删掉，那一节变成一段无主正文挂在文档里
 * （断言 `## 第 6 章` 出现 0 次）。所以下面有一道兜底：新内容若自己不带标题，
 * 就把原标题补回去。**宁可标题保留、正文是模型给的**，也不要悄悄把结构弄坏。
 *
 * @returns 替换后的完整文档；`heading` 找不到时**原样返回** —— 宁可什么都不改，
 *          也不要把新内容追加到文档末尾（那会看起来像"优化成功"但实际是重复了一节）。
 */
export function replaceSection(markdown: string, heading: string, replacement: string): string {
  const lines = markdown.split('\n')
  const headings = scanHeadings(lines)
  const position = headings.findIndex((item) => lines[item.index].trimEnd() === heading.trimEnd())
  if (position === -1) return markdown

  const target = headings[position]
  const next = headings.slice(position + 1).find((item) => item.level <= target.level)
  const end = next ? next.index : lines.length

  const originalHeading = lines[target.index].trimEnd()
  const polished = replacement.trim()
  // 兜底：模型只给了**子节**正文（或干脆没给标题）时，把原标题补回来。
  //
  // ⚠️ 判据不能只是"第一行是不是标题" —— 实测模型给的是 `### 接口清单`
  // （比 `## 第 6 章 接口清单` **低一级**的子节标题），那句判断会认为"它自带标题"，
  // 于是 `## 第 6 章` 整行被删掉（断言 `## 第 6 章` 出现 **0** 次）。
  // 正确的判据是**层级**：首行必须是同级或更高级的标题，才算它替代了本节标题。
  const firstLine = polished.split('\n')[0] ?? ''
  const firstLevel = ATX_HEADING.exec(firstLine)?.[1].length
  const carriesHeading = firstLevel !== undefined && firstLevel <= target.level
  const newSection = carriesHeading ? polished : `${originalHeading}\n\n${polished}`

  // `head` 刻意**不含**原标题行 —— 新内容自己带标题（或上面刚补上）
  const head = lines.slice(0, target.index)
  const tail = lines.slice(end)
  const joined = [
    ...head,
    ...newSection.split('\n'),
    ...(tail.length > 0 ? [''] : []),
    ...tail,
  ]
  return joined.join('\n')
}

/**
 * 字数统计口径：**按非空白字符数**。
 *
 * ⚠️ 与 `backend/scripts/validate_prompts.py` 的检查口径一致（那边是
 * `len(re.sub(r"\s", "", ...))`）。用 `.length` 会把换行与缩进算进去，
 * 而 PRD 模板给的是**字数上限**（实测第 7 章会超 18%，见 `HANDOFF.md` §4 坑 #2）——
 * 两边口径不一致的话，界面上"没超"而校验脚本说"超了"。
 */
export function countContentChars(text: string): number {
  return text.replace(/\s/g, '').length
}

// ---------------------------------------------------------------- 类型

/** 底部自定义操作按钮。 */
export interface DocumentAction {
  /** React key，同时用于 `data-action` 便于测试定位 */
  key: string
  label: string
  onClick: () => void
  variant?: 'primary' | 'secondary' | 'danger'
  disabled?: boolean
  /** 悬停说明；`disabled` 时尤其需要（比如"改过就要二次确认"） */
  title?: string
}

/** AI 优化的请求。字段名对应 `POST /conversation/optimize-document-stream` 的入参。 */
export interface OptimizeRequest {
  /** 要修订的节标题，**逐字**来自正文 */
  section: string
  /** 该节的现有正文（标题 + 正文）。不传服务端就没法保留用户手改的内容 */
  currentContent: string
  /** 用户反馈 */
  feedback: string
}

interface DocumentReviewProps {
  /** 文档名，如「PRD（产品需求文档）」 */
  title: string
  /** 已产出的正文。生成中可以为空（此时看 `streamingContent`） */
  content: string
  /** 生成中正在累积的增量。非 `generating` 状态下会被忽略 */
  streamingContent?: string
  status: DocStatus
  /** 用户编辑正文。调用方**应当**把新值写回 `content`，否则编辑只存在于组件内部 */
  onContentChange?: (content: string) => void
  /** AI 优化（F8.6）。不传则不显示优化面板 */
  onOptimize?: (request: OptimizeRequest) => void | Promise<void>
  /** 复制结果回调（成功/失败都会调），便于埋点或上层提示 */
  onCopy?: (ok: boolean) => void
  /** 生成失败时的原因说明 */
  failureReason?: string
  /**
   * 这份正文**被模型单次输出上限截断了**（末尾是缺的）。
   *
   * 与 `failureReason` 分开：截断的文档**是有用的**（前面大半是好的、可以编辑、可以
   * 优化补齐），不该按"失败"处理；但不说的话用户会把半行表格当成内容短。
   */
  truncated?: boolean
  /**
   * 分片生成的进度文案，如「正在生成第 2/3 片：第 7 章（1 章）」。
   *
   * 分片后一次生成要几分钟（实测每片 25–30 秒），不显示进度的话用户看到的是
   * 一个长时间不动的"生成中"，会以为卡死。
   */
  progress?: string
  /** 底部自定义操作按钮（通过 / 打回 / 重生成……）。**忙碌时组件会自动禁用它们** */
  actions?: DocumentAction[]
  className?: string
}

// ---------------------------------------------------------------- 状态呈现

const STATUS_META: Record<DocStatus, { label: string; className: string }> = {
  not_started: { label: '尚未生成', className: 'bg-slate-100 text-slate-600' },
  generating: { label: '生成中', className: 'bg-primary-100 text-primary-800' },
  pending_review: { label: '待审核', className: 'bg-amber-100 text-amber-800' },
  approved: { label: '已通过', className: 'bg-emerald-100 text-emerald-800' },
  stale: { label: '需重生成', className: 'bg-orange-100 text-orange-800' },
  failed: { label: '生成失败', className: 'bg-rose-100 text-rose-700' },
}

/**
 * 哪些状态**可以编辑**。
 *
 * ⚠️ `approved` 刻意**不可编辑**：产物已通过审核，就地改会让"已通过"这句话与实际内容
 * 脱节。要改必须先打回（一次状态变更，走 `/sessions/{id}/events`）。
 * `failed` 也不可编辑 —— 半截内容就地改，容易把"生成失败"悄悄变成"有内容可用"。
 */
const EDITABLE: ReadonlySet<DocStatus> = new Set<DocStatus>(['pending_review', 'stale'])

const ACTION_VARIANTS: Record<NonNullable<DocumentAction['variant']>, string> = {
  primary:
    'bg-primary-600 text-white hover:bg-primary-700 focus-visible:ring-primary-400 disabled:bg-slate-300 disabled:text-white',
  secondary:
    'border border-slate-300 text-slate-700 hover:bg-slate-50 focus-visible:ring-primary-300 disabled:text-slate-400',
  danger:
    'border border-rose-300 text-rose-700 hover:bg-rose-50 focus-visible:ring-rose-300 disabled:text-rose-300',
}

// ---------------------------------------------------------------- 剪贴板

/**
 * 退化复制路径。
 *
 * 两条路径都要有：`navigator.clipboard` 只在**安全上下文**（https / localhost）可用，
 * 而本项目开发期正是 localhost、部署后可能是内网 http —— 那时它直接是 `undefined`；
 * 即便存在，非用户手势或权限被拒时 `writeText` 也会 reject。
 * `document.execCommand('copy')` 虽已标记废弃，但仍是这些场景下唯一可用的手段。
 *
 * ⚠️ **复制出去的换行在 Windows 上会变成 CRLF**：我们写进去的是 `\n`，但从剪贴板读回来
 * 是 `\r\n`（实测 `firstDiff` 正好落在第一个换行处）。这是 Windows 剪贴板约定 + Chrome 的
 * 规范化行为，**不是这里的 bug，也没法在这一层改** —— 所以别去"修"它。
 * 校验复制结果时按行尾归一化后再比。
 */
function legacyCopy(text: string): boolean {
  const holder = document.createElement('textarea')
  holder.value = text
  holder.setAttribute('readonly', '')
  // 不能用 `display:none` —— 那样选不中，execCommand 会失败
  holder.style.position = 'fixed'
  holder.style.top = '0'
  holder.style.opacity = '0'
  document.body.appendChild(holder)
  holder.select()
  let ok = false
  try {
    ok = document.execCommand('copy')
  } catch {
    ok = false
  }
  document.body.removeChild(holder)
  return ok
}

// ---------------------------------------------------------------- 组件

export default function DocumentReview({
  title,
  content,
  streamingContent = '',
  status,
  onContentChange,
  onOptimize,
  onCopy,
  failureReason,
  truncated = false,
  progress,
  actions = [],
  className = '',
}: DocumentReviewProps) {
  const isGenerating = status === 'generating'
  const isEditable = EDITABLE.has(status)

  /**
   * 编辑中的草稿。
   *
   * 用内部 state 而不是直接受控于 `content`：调用方**不一定**会把 `onContentChange`
   * 的值写回来（比如它只是想收集一份副本）。直接受控的话，那种情况下输入框会打不进字。
   */
  const [draft, setDraft] = useState(content)
  /**
   * **产物原文**（最后一次由生成产出、而不是用户敲出来的内容）。
   *
   * ⚠️ 「已手改」徽标必须拿 `draft` 和**它**比，不能和 `content` 比。
   * 踩过的坑：调用方按推荐做法把 `onContentChange` 的值写回 `content` 之后，
   * `draft === content` 恒成立 —— 徽标**永远不出现**（实测抓到的）。
   * 所以这里要能分辨"这次的 `content` 变化是新产物"还是"我们自己那次编辑的回显"。
   */
  const [baseline, setBaseline] = useState(content)
  /** 我们最后一次通过 `onContentChange` 报出去的值，用来识别上面那种回显。 */
  const lastEmitted = useRef<string | null>(null)

  useEffect(() => {
    if (lastEmitted.current !== null && content === lastEmitted.current) {
      // 这次 content 变化就是我们自己那次编辑的回显 —— 产物没变，基线不能跟着动
      return
    }
    setBaseline(content)
    setDraft(content)
    lastEmitted.current = null
  }, [content])

  /** 当前展示给用户的文本：生成中看增量，其余看定稿内容。 */
  const displayText = isGenerating ? streamingContent : content
  /** 生成中可能一个字都还没到，这时的空面板要显示"正在思考"而不是"尚未生成"。 */
  const showThinking = isGenerating && displayText.length === 0

  const [copyState, setCopyState] = useState<'idle' | 'ok' | 'error'>('idle')
  const copyTimer = useRef<number | null>(null)

  const [optimizing, setOptimizing] = useState(false)
  const [feedback, setFeedback] = useState('')

  // 节列表从**当前编辑中的文本**算 —— 发给服务端的 `current_content` 必须是
  // 用户手上这份，而不是最初生成的那份
  const sections = useMemo(() => splitSections(isEditable ? draft : displayText), [
    isEditable,
    draft,
    displayText,
  ])
  const [sectionIndex, setSectionIndex] = useState(0)
  useEffect(() => {
    // 节数变少（例如重新生成后结构不同）时把越界的选择拉回来，否则 select 会显示空白
    setSectionIndex((prev) => (prev < sections.length ? prev : 0))
  }, [sections.length])

  const busy = isGenerating || optimizing
  const edited = isEditable && draft !== baseline

  useEffect(
    () => () => {
      // 卸载时清掉反馈提示的定时器，别在已卸载的组件上 setState
      if (copyTimer.current !== null) window.clearTimeout(copyTimer.current)
    },
    [],
  )

  async function handleCopy() {
    if (!displayText) return
    let ok = false
    try {
      if (navigator.clipboard && typeof navigator.clipboard.writeText === 'function') {
        await navigator.clipboard.writeText(displayText)
        ok = true
      }
    } catch {
      ok = false // 权限被拒 / 非安全上下文：落到下面的退化路径
    }
    if (!ok) ok = legacyCopy(displayText)

    setCopyState(ok ? 'ok' : 'error')
    onCopy?.(ok)
    if (copyTimer.current !== null) window.clearTimeout(copyTimer.current)
    copyTimer.current = window.setTimeout(() => setCopyState('idle'), 2000)
  }

  async function handleOptimize() {
    if (!onOptimize || sections.length === 0) return
    const section = sections[sectionIndex]
    setOptimizing(true)
    try {
      await onOptimize({
        section: section.heading,
        // 标题 + 正文一起发：服务端只认"标题逐字一致"，而模型需要正文才能只改这一节
        currentContent: [section.heading, section.body].filter(Boolean).join('\n\n'),
        feedback: feedback.trim(),
      })
      setFeedback('')
    } finally {
      // 不论成败都要解锁：失败时内容由调用方通过 `failureReason` / `status` 反馈，
      // 组件自己吞掉异常不重抛（重抛会让调用方的 await 变成未捕获 rejection）
      setOptimizing(false)
    }
  }

  const statusMeta = STATUS_META[status]

  return (
    <section
      data-testid="document-review"
      data-status={status}
      className={[
        // `min-h-0` + `overflow-hidden`：面板里有一大块可滚动区域（编辑器 / 预览），
        // 少了这两个，内层内容会把面板顶破、盖到下面的兄弟元素上（实测截图确认过）。
        'flex min-h-0 flex-col gap-3 overflow-hidden rounded-xl border border-slate-200 bg-white shadow-sm',
        className,
      ].join(' ')}
    >
      {/* ---------- 头部：标题 + 状态 + 工具 ---------- */}
      <header className="flex flex-wrap items-center gap-x-3 gap-y-2 border-b border-slate-100 px-4 py-3">
        <h2 className="text-base font-medium text-slate-800">{title}</h2>
        <span
          data-testid="document-status"
          className={['rounded px-1.5 py-0.5 text-xs', statusMeta.className].join(' ')}
        >
          {statusMeta.label}
        </span>
        {edited && (
          <span
            data-testid="document-edited"
            className="inline-flex items-center gap-1 rounded bg-sky-100 px-1.5 py-0.5 text-xs text-sky-800"
            title="改过就要在重生成前二次确认（F4.12）"
          >
            <Pencil className="h-3 w-3" aria-hidden />
            已手改
          </span>
        )}
        {!isEditable && displayText.length > 0 && (
          <span
            className="inline-flex items-center gap-1 text-xs text-slate-400"
            title={
              status === 'approved'
                ? '已通过审核，如需修改请先打回'
                : '生成中 / 失败的内容是只读的'
            }
          >
            <Lock className="h-3 w-3" aria-hidden />
            只读
          </span>
        )}

        <div className="ml-auto flex items-center gap-2">
          <span className="text-xs tabular-nums text-slate-400" title="按非空白字符计">
            {countContentChars(displayText)} 字
          </span>
          <button
            type="button"
            data-testid="document-copy"
            onClick={() => void handleCopy()}
            disabled={displayText.length === 0}
            className="inline-flex items-center gap-1.5 rounded-lg border border-slate-300 px-2.5 py-1 text-xs text-slate-700 transition hover:bg-slate-50 focus:outline-none focus-visible:ring-2 focus-visible:ring-primary-300 disabled:cursor-not-allowed disabled:text-slate-400"
          >
            {copyState === 'ok' ? (
              <Check className="h-3.5 w-3.5 text-emerald-600" aria-hidden />
            ) : (
              <Copy className="h-3.5 w-3.5" aria-hidden />
            )}
            {copyState === 'ok' ? '已复制' : copyState === 'error' ? '复制失败' : '复制'}
          </button>
        </div>
      </header>

      {/* ---------- 失败原因 ---------- */}
      {status === 'failed' && (
        <div
          role="alert"
          className="mx-4 flex items-start gap-2 rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-xs text-rose-700"
        >
          <AlertCircle className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden />
          <span>
            {failureReason || '生成失败。'}
            <span className="text-rose-600/90">
              {' '}
              下面是<strong className="font-medium">当前保留的版本</strong>，重试请用底部操作按钮。
            </span>
          </span>
        </div>
      )}

      {/* ---------- 分片进度 ---------- */}
      {isGenerating && progress && (
        <p
          data-testid="document-progress"
          className="mx-4 rounded-lg border border-slate-200 bg-slate-50 px-3 py-2 text-xs text-slate-600"
        >
          {progress}
        </p>
      )}

      {/* ---------- 被输出上限截断 ---------- */}
      {/* 刻意**不用 `role="alert"`**：这不是错误、也不打断当前操作，而是一条
          必须看见的事实说明。用 alert 会与真正的失败提示混成同一类信号。 */}
      {truncated && (
        <div
          data-testid="document-truncated"
          className="mx-4 flex items-start gap-2 rounded-lg border border-amber-300 bg-amber-50 px-3 py-2 text-xs text-amber-800"
        >
          <AlertCircle className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden />
          <span>
            这份正文<strong className="font-medium">没有生成完</strong>
            ：上一次整份生成时输出撞上了模型单次上限，末尾是缺的（常见表现是最后一行表格或
            最后一个文件没写完）。前面的内容可用、也可以直接编辑；重新生成整份才会清掉这条提示。
          </span>
        </div>
      )}

      {/* ---------- 主体：只读 Markdown / 可编辑 textarea ---------- */}
      {isEditable ? (
        <textarea
          data-testid="document-editor"
          value={draft}
          onChange={(event) => {
            const next = event.target.value
            setDraft(next)
            // 记下"这是我们自己报出去的"，下一次 content 变成这个值时就知道是回显而非新产物
            lastEmitted.current = next
            onContentChange?.(next)
          }}
          spellCheck={false}
          // `min-h-[12rem]` 而不是更大的值：这是**可以缩小的下限**，不是固定高度。
          // 给太大（比如 24rem）会在窄视口里把面板顶破 —— 内层撑破外层是 flex 的经典坑，
          // 实测在 1200×850 的窗口下就复现了。
          className="mx-4 min-h-[12rem] flex-1 resize-y rounded-lg border border-slate-300 bg-white px-3 py-2 font-mono text-xs leading-relaxed text-slate-800 outline-none transition focus:border-primary-400 focus:ring-2 focus:ring-primary-100"
        />
      ) : (
        <div
          data-testid="document-viewer"
          className="mx-4 min-h-0 flex-1 overflow-y-auto rounded-lg border border-slate-200 bg-slate-50/60 px-3 py-2"
        >
          {showThinking ? (
            <p className="flex items-center gap-2 py-8 text-sm text-slate-400">
              <Loader2 className="h-4 w-4 animate-spin" aria-hidden />
              正在生成…
            </p>
          ) : displayText.length === 0 ? (
            <p className="py-8 text-center text-sm text-slate-400">
              {status === 'not_started' ? '还没有生成这份文档。' : '暂时没有内容。'}
            </p>
          ) : (
            <>
              <MarkdownBody>{displayText}</MarkdownBody>
              {isGenerating && (
                <span
                  data-testid="document-streaming-cursor"
                  aria-label="正在生成"
                  className="ml-0.5 inline-block h-3.5 w-1.5 animate-pulse rounded-sm bg-primary-500 align-text-bottom"
                />
              )}
            </>
          )}
        </div>
      )}

      {/* ---------- AI 优化（F8.6）----------
          ⚠️ `approved` 时**不给**优化入口：这一状态本身就不可编辑（见 `EDITABLE` 的说明），
          而优化是"改内容"——留着它等于一边说"要改先打回"、一边又开了个改的后门
          （而且优化成功会把"已通过"撤掉，用户会莫名发现通过没了）。 */}
      {onOptimize && !isGenerating && status !== 'approved' && sections.length > 0 && (
        <div className="mx-4 rounded-lg border border-slate-200 bg-slate-50 px-3 py-3">
          <div className="flex flex-wrap items-center gap-2">
            <label
              htmlFor="optimize-section"
              className="inline-flex items-center gap-1 text-xs font-medium text-slate-700"
            >
              <Sparkles className="h-3.5 w-3.5 text-primary-600" aria-hidden />
              AI 优化
            </label>
            <select
              id="optimize-section"
              data-testid="optimize-section"
              value={sectionIndex}
              onChange={(event) => setSectionIndex(Number(event.target.value))}
              disabled={busy}
              className="max-w-full flex-1 rounded border border-slate-300 bg-white px-2 py-1 text-xs text-slate-700 outline-none focus:border-primary-400 disabled:bg-slate-100"
            >
              {sections.map((section, index) => (
                <option key={`${section.heading}-${index}`} value={index}>
                  {section.heading.replace(/^#+\s*/, '')}
                </option>
              ))}
            </select>
          </div>
          <textarea
            data-testid="optimize-feedback"
            value={feedback}
            rows={2}
            disabled={busy}
            placeholder="想让 AI 改什么？例如「表格缺优先级列，请补上」"
            onChange={(event) => setFeedback(event.target.value)}
            className="mt-2 w-full resize-y rounded border border-slate-300 bg-white px-2 py-1.5 text-xs leading-relaxed text-slate-700 outline-none focus:border-primary-400 focus:ring-2 focus:ring-primary-100 disabled:bg-slate-100"
          />
          <div className="mt-2 flex items-center gap-2">
            <button
              type="button"
              data-testid="optimize-submit"
              onClick={() => void handleOptimize()}
              disabled={busy || feedback.trim().length === 0}
              className="inline-flex items-center gap-1.5 rounded-lg bg-primary-600 px-3 py-1.5 text-xs font-medium text-white transition hover:bg-primary-700 focus:outline-none focus-visible:ring-2 focus-visible:ring-primary-400 disabled:cursor-not-allowed disabled:bg-slate-300"
            >
              {optimizing ? (
                <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden />
              ) : (
                <Sparkles className="h-3.5 w-3.5" aria-hidden />
              )}
              {optimizing ? '优化中…' : '优化这一节'}
            </button>
            <span className="text-xs text-slate-400">
              只改选中的这一节；现有正文会一起发给模型，手改的内容不会被覆盖掉。
            </span>
          </div>
        </div>
      )}

      {/* ---------- 底部自定义操作按钮 ---------- */}
      {actions.length > 0 && (
        <footer className="flex flex-wrap items-center gap-2 border-t border-slate-100 px-4 py-3">
          {actions.map((action) => (
            <button
              key={action.key}
              type="button"
              data-action={action.key}
              onClick={action.onClick}
              // 忙碌时**一律**禁用：生成/优化期间点"通过"会把半截产物审成定稿
              disabled={busy || action.disabled}
              title={action.title}
              className={[
                'rounded-lg px-3 py-1.5 text-xs font-medium transition focus:outline-none focus-visible:ring-2 disabled:cursor-not-allowed',
                ACTION_VARIANTS[action.variant ?? 'secondary'],
              ].join(' ')}
            >
              {action.label}
            </button>
          ))}
        </footer>
      )}
    </section>
  )
}
