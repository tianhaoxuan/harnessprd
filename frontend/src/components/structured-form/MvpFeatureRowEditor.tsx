/**
 * MVP 功能的**行编辑器**（05 篇）—— 把"每行 `功能名｜描述｜价值`"的多行文本
 * 换成真正可以增删改的列表。
 *
 * ## 它解决的问题（需求 §二）
 *
 * 改造前用户要**记住**竖线约定，写错了只能在提交校验时才看到红字；而这个约定本身
 * 是给解析器看的，不是给人看的。现在三列各有一个输入框，格式不可能错。
 *
 * ## 只有一行时，删除 = 清空
 *
 * 删到 0 行会渲染成一个没有任何输入框的空块，用户会以为界面坏了、也没法再加回来。
 * 所以只剩一行时那颗按钮退化成"清空这一行"（需求 §六）。
 *
 * ## 纯受控
 *
 * 行数据与增删都由 `App`/`StructuredForm` 持有，本组件不存 state ——
 * 草稿、示例填充、老数据迁移都发生在那一层，组件只负责画。
 */

import { Plus, Trash2 } from 'lucide-react'

import { type MvpFeatureRow } from '../../types/structuredForm'

export interface MvpFeatureRowEditorProps {
  rows: MvpFeatureRow[]
  onChange: (rows: MvpFeatureRow[]) => void
  /** 提交后由 `validate()` 给出的逐行错误（第 N 行 → 文案）。 */
  rowErrors?: Record<number, string[]>
  /** 表单级错误（例如"请至少填写一个 MVP 功能"）。 */
  errors?: string[]
  disabled?: boolean
}

const CELL =
  'w-full rounded border border-slate-300 bg-white px-2 py-1.5 text-sm text-slate-800 outline-none transition focus:border-primary-400 focus:ring-2 focus:ring-primary-100 disabled:bg-slate-50'

const COLUMNS: Array<{ key: keyof MvpFeatureRow; label: string; placeholder: string; required: boolean }> = [
  { key: 'name', label: '功能名称', placeholder: '绑定群聊并汇总', required: true },
  { key: 'description', label: '功能描述', placeholder: '选定群聊与时间范围后触发汇总', required: true },
  { key: 'user_value', label: '用户价值', placeholder: '省掉每天翻记录', required: false },
]

export default function MvpFeatureRowEditor({
  rows,
  onChange,
  rowErrors = {},
  errors = [],
  disabled = false,
}: MvpFeatureRowEditorProps) {
  const patch = (index: number, key: keyof MvpFeatureRow, value: string) => {
    onChange(rows.map((row, i) => (i === index ? { ...row, [key]: value } : row)))
  }

  /** 只剩一行时清空它，而不是删成 0 行（见文件头）。 */
  const remove = (index: number) => {
    if (rows.length <= 1) {
      onChange([{ name: '', description: '', user_value: '' }])
      return
    }
    onChange(rows.filter((_, i) => i !== index))
  }

  return (
    <div data-testid="mvp-row-editor" className="flex flex-col gap-2">
      {/* 桌面端三列网格的表头；移动端堆叠，所以表头也只在大屏出现 */}
      <div className="hidden gap-2 px-1 md:grid md:grid-cols-[1.1fr_1.6fr_1fr_auto]">
        {COLUMNS.map((column) => (
          <span key={column.key} className="text-xs font-medium text-slate-500">
            {column.label}
            {column.required ? <span className="text-rose-500"> *</span> : null}
          </span>
        ))}
        <span className="w-8" />
      </div>

      {rows.map((row, index) => {
        const rowError = rowErrors[index] ?? []
        return (
          <div
            key={index}
            data-testid={`mvp-row-${index}`}
            className={[
              'grid gap-2 rounded-lg border p-2 md:grid-cols-[1.1fr_1.6fr_1fr_auto] md:items-start',
              rowError.length > 0 ? 'border-rose-300 bg-rose-50/40' : 'border-slate-200 bg-white',
            ].join(' ')}
          >
            {COLUMNS.map((column) => (
              <label key={column.key} className="flex flex-col gap-1 md:contents">
                {/* 移动端才显示的小标签（桌面端由表头负责） */}
                <span className="text-xs text-slate-500 md:hidden">{column.label}</span>
                <input
                  type="text"
                  data-testid={`mvp-${column.key}-${index}`}
                  value={row[column.key]}
                  disabled={disabled}
                  placeholder={column.placeholder}
                  onChange={(event) => patch(index, column.key, event.target.value)}
                  className={CELL}
                />
              </label>
            ))}
            <div className="flex items-start justify-end">
              <button
                type="button"
                data-testid={`mvp-remove-${index}`}
                onClick={() => remove(index)}
                disabled={disabled}
                title={rows.length <= 1 ? '清空这一行' : '删除这一行'}
                aria-label={rows.length <= 1 ? '清空这一行' : '删除这一行'}
                className="inline-flex h-8 w-8 items-center justify-center rounded border border-slate-300 text-slate-500 transition hover:bg-rose-50 hover:text-rose-600 disabled:cursor-not-allowed disabled:text-slate-300"
              >
                <Trash2 className="h-3.5 w-3.5" aria-hidden />
              </button>
            </div>
            {rowError.length > 0 && (
              <p className="text-xs text-rose-600 md:col-span-4">{rowError.join('；')}</p>
            )}
          </div>
        )
      })}

      {errors.length > 0 && (
        <ul data-testid="mvp-row-errors" className="flex flex-col gap-0.5">
          {errors.map((message) => (
            <li key={message} className="text-xs text-rose-600">
              {message}
            </li>
          ))}
        </ul>
      )}

      <button
        type="button"
        data-testid="mvp-add-row"
        onClick={() => onChange([...rows, { name: '', description: '', user_value: '' }])}
        disabled={disabled}
        className="inline-flex w-fit items-center gap-1.5 rounded-lg border border-slate-300 px-3 py-1.5 text-xs text-slate-600 transition hover:bg-slate-50 disabled:cursor-not-allowed disabled:text-slate-300"
      >
        <Plus className="h-3.5 w-3.5" aria-hidden />
        添加功能
      </button>
    </div>
  )
}
