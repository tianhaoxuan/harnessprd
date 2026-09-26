import { useEffect, useRef, useState } from 'react'
import type { KeyboardEvent } from 'react'
import { Send } from 'lucide-react'

/** 输入框长到这么高就不再长了，改为内部滚动 */
const MAX_HEIGHT_PX = 200

interface ChatInputProps {
  /**
   * 发送回调。
   *
   * 返回 Promise 时：**失败会把内容还回输入框** —— 否则用户刚打的一段话就没了，
   * 得重打一遍。不返回 Promise（或自己吞掉错误）则不会回填。
   */
  onSend: (text: string) => void | Promise<void>
  disabled?: boolean
  placeholder?: string
}

/**
 * 对话输入框：Enter 发送、Shift+Enter 换行、高度自适应、发送后清空。
 */
export default function ChatInput({
  onSend,
  disabled = false,
  placeholder = '输入你的回答…（Enter 发送，Shift+Enter 换行）',
}: ChatInputProps) {
  const [value, setValue] = useState('')
  const textareaRef = useRef<HTMLTextAreaElement>(null)

  // 高度自适应。
  // ⚠️ **必须先把 height 归零再读 scrollHeight**：不归零的话，删字时 scrollHeight
  // 不会小于当前的 height，输入框就只会长、不会缩回去。
  useEffect(() => {
    const el = textareaRef.current
    if (!el) return
    el.style.height = 'auto'
    el.style.height = `${Math.min(el.scrollHeight, MAX_HEIGHT_PX)}px`
  }, [value])

  async function submit() {
    const text = value.trim()
    if (!text || disabled) return

    setValue('') // 先清空，手感上是即时反馈
    try {
      await onSend(text)
    } catch {
      setValue(text) // 发送失败就还给用户，别让他重打
    }
  }

  function handleKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key !== 'Enter') return

    // ⚠️ 中文 / 日文输入法：正在组合候选词时按 Enter 是「确认候选」，不是「发送」。
    // 不判断 isComposing 的话，用输入法打中文时每选一个词就会把消息发出去 ——
    // 中文产品上这是致命的可用性问题。
    if (event.nativeEvent.isComposing) return

    // 第二道保险：部分 Windows 输入法在组合状态下把 Enter 报成 keyCode 229，
    // 而 isComposing 未必为 true。两道都留着 —— 代价为零，漏判的后果是
    // 用户话打一半就被发出去。
    if (event.keyCode === 229) return

    // Shift+Enter 交给浏览器插入换行
    if (event.shiftKey) return

    event.preventDefault()
    void submit()
  }

  const canSend = value.trim().length > 0 && !disabled

  return (
    <div className="flex items-end gap-2 rounded-xl border border-slate-200 bg-white p-2 shadow-sm transition focus-within:border-primary-300 focus-within:ring-2 focus-within:ring-primary-100">
      <textarea
        ref={textareaRef}
        value={value}
        rows={1}
        disabled={disabled}
        placeholder={placeholder}
        aria-label="对话输入"
        onChange={(event) => setValue(event.target.value)}
        onKeyDown={handleKeyDown}
        className="min-h-9 flex-1 resize-none bg-transparent px-2 py-1.5 text-sm leading-relaxed text-slate-900 outline-none placeholder:text-slate-400 disabled:cursor-not-allowed disabled:opacity-60"
      />

      <button
        type="button"
        onClick={() => void submit()}
        disabled={!canSend}
        aria-label="发送"
        className="inline-flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-primary-600 text-white transition hover:bg-primary-700 focus:outline-none focus-visible:ring-2 focus-visible:ring-primary-400 disabled:cursor-not-allowed disabled:bg-slate-300"
      >
        <Send className="h-4 w-4" aria-hidden />
      </button>
    </div>
  )
}
