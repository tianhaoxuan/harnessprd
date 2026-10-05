/**
 * 页面结构的**行编辑器**（05 篇）—— `页面名｜模块1,模块2｜备注` 的替代品。
 *
 * 与 `MvpFeatureRowEditor` 同构，差别只有三处：
 *
 * 1. 三列是 `页面名` / `主要模块` / `备注`，只有**页面名必填**（模块与备注都可省）；
 * 2. 「主要模块」是**一个输入框、逗号分隔**（逗号由用户打，不是给解析器看的语法糖：
 *    模块名本身就是人写给人看的清单，逗号是最自然的写法）；
 * 3. 只剩一行时删除同样退化成"清空这一行"。
 *
 * 详细的取舍见 `MvpFeatureRowEditor` 的文件头，这里不重复。
 */

import { Plus, Trash2 } from 'lucide-react'

import { emptyUiPageRow, type UiPageRow } from '../../types/structuredForm'

export interface UiPageRowEditorProps {
  rows: UiPageRow[]
  onChange: (rows: UiPageRow[]) => void
  /** 提交后由 `validate()` 给出的逐行错误（第 N 行 → 文案）。 */
  rowErrors?: Record<number, string[]>
  errors?: string[]
  disabled?: boolean
}

const CELL =
  'w-full rounded border border-slate-300 bg-white px-2 py-1.5 text-sm text-slate-800 outline-none transition focus:border-primary-400 focus:ring-2 focus:ring-primary-100 disabled:bg-slate-50'

const COLUMNS: Array<{ key: keyof UiPageRow; label: string; placeholder: string; required: boolean }> = [
  { key: 'name', label: '页面名称', placeholder: '周报草稿页', required: true },
  { key: 'modules', label: '主要模块', placeholder: '逗号分隔，如：列表,编辑器,导出条', required: false },
  { key: 'notes', label: '备注', placeholder: '/reports/:id', required: false },
]

export default function UiPageRowEditor({
  rows,
  onChange,
  rowErrors = {},
  errors = [],
  disabled = false,
}: UiPageRowEditorProps) {
  const patch = (index: number, key: keyof UiPageRow, value: string) => {
    onChange(rows.map((row, i) => (i === index ? { ...row, [key]: value } : row)))
  }

  const remove = (index: number) => {
    if (rows.length <= 1) {
      onChange([emptyUiPageRow()])
      return
    }
    onChange(rows.filter((_, i) => i !== index))
  }

  return (
    <div data-testid="ui-page-row-editor" className="flex flex-col gap-2">
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
            data-testid={`ui-page-row-${index}`}
            className={[
              'grid gap-2 rounded-lg border p-2 md:grid-cols-[1.1fr_1.6fr_1fr_auto] md:items-start',
              rowError.length > 0 ? 'border-rose-300 bg-rose-50/40' : 'border-slate-200 bg-white',
            ].join(' ')}
          >
            {COLUMNS.map((column) => (
              <label key={column.key} className="flex flex-col gap-1 md:contents">
                <span className="text-xs text-slate-500 md:hidden">{column.label}</span>
                <input
                  type="text"
                  data-testid={`ui-page-${column.key}-${index}`}
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
                data-testid={`ui-page-remove-${index}`}
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
        <ul data-testid="ui-page-row-errors" className="flex flex-col gap-0.5">
          {errors.map((message) => (
            <li key={message} className="text-xs text-rose-600">
              {message}
            </li>
          ))}
        </ul>
      )}

      <button
        type="button"
        data-testid="ui-page-add-row"
        onClick={() => onChange([...rows, emptyUiPageRow()])}
        disabled={disabled}
        className="inline-flex w-fit items-center gap-1.5 rounded-lg border border-slate-300 px-3 py-1.5 text-xs text-slate-600 transition hover:bg-slate-50 disabled:cursor-not-allowed disabled:text-slate-300"
      >
        <Plus className="h-3.5 w-3.5" aria-hidden />
        添加页面
      </button>
    </div>
  )
}
