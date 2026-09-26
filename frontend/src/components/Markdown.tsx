import Markdown, { type Components } from 'react-markdown'
import remarkGfm from 'remark-gfm'

/**
 * Markdown 元素的样式映射 —— **对话气泡与文档预览共用这一份**。
 *
 * 抽出来的理由与提示词外置同理：两处各写一份必然漂移，而两边的视觉本来就该一致
 * （同一份 PRD 在对话里和在预览区里长得不一样，纯属 bug）。
 *
 * 刻意**不引 `@tailwindcss/typography`**：只要一个 `prose` 类就能省掉下面这些，
 * 但那是一个只为样式而加的依赖，而且主题色还得再覆盖一遍。直接用组件映射，
 * 样式跟本站的 `slate` / `primary` 调色板天然一致。
 *
 * ⚠️ 改这里的字号 / 间距会**同时**影响对话页与文档页 —— 这是有意的。
 */
export const MARKDOWN_COMPONENTS: Components = {
  p: ({ node, ...props }) => <p className="my-2 leading-relaxed first:mt-0 last:mb-0" {...props} />,
  strong: ({ node, ...props }) => <strong className="font-semibold text-slate-900" {...props} />,
  em: ({ node, ...props }) => <em className="italic" {...props} />,
  del: ({ node, ...props }) => <del className="text-slate-400" {...props} />,
  ul: ({ node, ...props }) => <ul className="my-2 list-disc space-y-1 pl-5" {...props} />,
  ol: ({ node, ...props }) => <ol className="my-2 list-decimal space-y-1 pl-5" {...props} />,
  li: ({ node, ...props }) => <li className="leading-relaxed" {...props} />,
  h1: ({ node, ...props }) => (
    <h1 className="mt-3 mb-1.5 text-base font-semibold text-slate-900" {...props} />
  ),
  h2: ({ node, ...props }) => (
    <h2 className="mt-3 mb-1.5 text-base font-semibold text-slate-900" {...props} />
  ),
  h3: ({ node, ...props }) => (
    <h3 className="mt-2.5 mb-1 text-sm font-semibold text-slate-900" {...props} />
  ),
  h4: ({ node, ...props }) => (
    <h4 className="mt-2 mb-1 text-sm font-semibold text-slate-800" {...props} />
  ),
  blockquote: ({ node, ...props }) => (
    <blockquote className="my-2 border-l-2 border-slate-300 pl-3 text-slate-600" {...props} />
  ),
  hr: ({ node, ...props }) => <hr className="my-3 border-slate-200" {...props} />,
  a: ({ node, ...props }) => (
    <a
      className="text-primary-700 underline underline-offset-2"
      target="_blank"
      rel="noreferrer"
      {...props}
    />
  ),
  code: ({ node, className, children, ...props }) => {
    // 行内代码与代码块靠 className 区分：代码块会带 `language-xxx`
    const isBlock = typeof className === 'string' && className.startsWith('language-')
    if (isBlock) {
      return (
        <code className={['font-mono text-xs', className].filter(Boolean).join(' ')} {...props}>
          {children}
        </code>
      )
    }
    return (
      <code
        className="rounded bg-slate-100 px-1 py-0.5 font-mono text-[0.8em] text-slate-800"
        {...props}
      >
        {children}
      </code>
    )
  },
  pre: ({ node, ...props }) => (
    <pre className="my-2 overflow-x-auto rounded-lg bg-slate-900 p-3 text-slate-100" {...props} />
  ),
  // 产物里表格很密集（PRD / 接口文档），所以表格样式必须给全。
  // ⚠️ 没有 remark-gfm 的话表格语法根本不会被解析，这些映射也就用不上。
  table: ({ node, ...props }) => (
    <div className="my-3 overflow-x-auto">
      <table className="w-full border-collapse text-left text-xs" {...props} />
    </div>
  ),
  th: ({ node, ...props }) => (
    <th
      className="border border-slate-200 bg-slate-50 px-2 py-1 font-medium text-slate-700"
      {...props}
    />
  ),
  td: ({ node, ...props }) => (
    <td className="border border-slate-200 px-2 py-1 align-top text-slate-700" {...props} />
  ),
}

interface MarkdownBodyProps {
  /** Markdown 源文本 */
  children: string
  /** 外层容器的类名（内边距、最大高度等由调用方决定） */
  className?: string
}

/**
 * 渲染一段 Markdown 正文。
 *
 * 只负责"把 Markdown 变成带本站样式的 HTML"这一件事：滚动、边框、背景色都留给调用方，
 * 因为对话气泡与文档面板在这几点上完全不同。
 */
export default function MarkdownBody({ children, className = '' }: MarkdownBodyProps) {
  return (
    <div className={className}>
      <Markdown remarkPlugins={[remarkGfm]} components={MARKDOWN_COMPONENTS}>
        {children}
      </Markdown>
    </div>
  )
}
