import { useEffect, useRef, useState } from 'react'
import { AlertCircle, ArrowDown, Bot, User } from 'lucide-react'

import type { ChatMessage } from '../types'
import MarkdownBody from './Markdown'

/** 距底部多少 px 以内算"贴着底部"。超出就不自动跟随，免得把正在看历史的用户拽走。 */
const PIN_THRESHOLD_PX = 48

interface MessageListProps {
  messages: ChatMessage[]
  /** 外层类名。默认撑满父容器高度 */
  className?: string
}

/**
 * 对话消息列表：用户右侧、AI 左侧，AI 正文渲染 Markdown，新消息自动滚到底。
 *
 * 自动滚动的取舍：**只在用户本来就贴着底部时才跟随**。否则流式输出会不断把
 * 正在往上翻历史的用户拽回底部 —— 那是比"不自动滚动"更烦人的行为。
 * 不跟随时右下角给一个「最新」按钮，给用户一条回去的路。
 */
export default function MessageList({ messages, className = '' }: MessageListProps) {
  const containerRef = useRef<HTMLDivElement>(null)
  const [pinned, setPinned] = useState(true)

  // 只渲染 user / ai。
  // 设计里**没有 `system` 角色**（系统提示词是给模型的指令，后端 `_serialize_messages`
  // 已经丢掉了 SystemMessage），所以这里是**防御性**的：将来服务端多出任何未知 role，
  // 都不会被当成正文渲染出来。
  const visible = messages.filter((message) => message.role === 'user' || message.role === 'ai')

  function handleScroll() {
    const el = containerRef.current
    if (!el) return
    setPinned(el.scrollHeight - el.scrollTop - el.clientHeight < PIN_THRESHOLD_PX)
  }

  // 用 `scrollTop` 赋值而不是 `scrollIntoView`：流式输出时 messages 每几十毫秒变一次，
  // `scrollIntoView({behavior:'smooth'})` 会反复重启平滑动画，画面一直在抖。
  // 直接赋值是瞬时的，高频调用也稳。
  useEffect(() => {
    if (!pinned) return
    const el = containerRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [messages, pinned])

  function jumpToLatest() {
    const el = containerRef.current
    if (!el) return
    el.scrollTop = el.scrollHeight
    setPinned(true)
  }

  return (
    <div className={`relative ${className}`}>
      <div
        ref={containerRef}
        onScroll={handleScroll}
        role="log"
        aria-live="polite"
        className="h-full overflow-y-auto px-1"
        data-testid="message-list"
      >
        {visible.length === 0 ? (
          <p className="py-16 text-center text-sm text-slate-400">还没有消息</p>
        ) : (
          <ol className="flex flex-col gap-4 py-2">
            {visible.map((message) => (
              <MessageRow key={message.id} message={message} />
            ))}
          </ol>
        )}
      </div>

      {!pinned && visible.length > 0 && (
        <button
          type="button"
          onClick={jumpToLatest}
          className="absolute bottom-3 left-1/2 inline-flex -translate-x-1/2 items-center gap-1 rounded-full border border-slate-200 bg-white px-3 py-1 text-xs text-slate-600 shadow-md transition hover:bg-slate-50"
        >
          <ArrowDown className="h-3.5 w-3.5" aria-hidden />
          最新
        </button>
      )}
    </div>
  )
}

function MessageRow({ message }: { message: ChatMessage }) {
  const isUser = message.role === 'user'
  // AI 的 `content` 是结构化 JSON 原文（要回传给后端），界面看的是抠出来的 `display`。
  // 用户消息没有 display，两者等价。
  const text = message.display ?? message.content

  return (
    <li
      className={['flex gap-3', isUser ? 'flex-row-reverse' : 'flex-row'].join(' ')}
      data-role={message.role}
    >
      <span
        className={[
          'mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-full',
          isUser ? 'bg-slate-200 text-slate-600' : 'bg-primary-100 text-primary-700',
        ].join(' ')}
        aria-hidden
      >
        {isUser ? <User className="h-4 w-4" /> : <Bot className="h-4 w-4" />}
      </span>

      <div
        className={[
          'flex min-w-0 max-w-[85%] flex-col gap-1',
          isUser ? 'items-end' : 'items-start',
        ].join(' ')}
      >
        <div
          className={[
            'rounded-xl px-4 py-2.5 text-sm shadow-sm',
            isUser
              ? 'rounded-tr-sm bg-primary-600 text-white'
              : 'rounded-tl-sm border border-slate-200 bg-white text-slate-800',
          ].join(' ')}
        >
          {isUser ? (
            // 用户消息按纯文本渲染：保留 Shift+Enter 的换行，且不解析 Markdown
            //（用户随手打的 `*` `#` 不该变成标题或斜体）
            <p className="whitespace-pre-wrap break-words">{text}</p>
          ) : (
            <MarkdownBody>{text}</MarkdownBody>
          )}

          {message.streaming && (
            <span
              className="ml-0.5 inline-block h-3.5 w-1.5 animate-pulse rounded-sm bg-primary-500 align-text-bottom"
              aria-label="正在生成"
            />
          )}
        </div>

        {message.error && (
          <p className="flex items-center gap-1 text-xs text-rose-600">
            <AlertCircle className="h-3.5 w-3.5 shrink-0" aria-hidden />
            {message.error}
          </p>
        )}
      </div>
    </li>
  )
}
