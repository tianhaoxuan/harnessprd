import { useMemo, useState } from 'react'
import type { FormEvent } from 'react'
import { AlertCircle, ChevronDown, ChevronRight, Send } from 'lucide-react'

import type { QuestionConfig, QuestionsConfig } from '../types'

/** 表单值：键 = 题目 **id**，值 = 作答。与后端 `form: dict[str, str]` 同形（扁平，不嵌套）。 */
export type FormValues = Record<string, string>

interface FormStepProps {
  /** 服务端下发的表单配置。前端**不允许**自备一份题目（`对话阶段设计` §8 第 7 项）。 */
  questions: QuestionsConfig
  /** 受控值，由父组件持有 —— 双向绑定。 */
  values: FormValues
  onFieldChange: (id: string, value: string) => void
  /** **校验通过后**才会被调用。 */
  onSubmit: (values: FormValues) => void
  submitting?: boolean
}

/**
 * 前端校验。
 *
 * 镜像服务端 `submit_form` 的 guard（`docs/状态机设计.md` §3.2）：
 * 「7 道必填题均已填写；全部字段通过 `form_submission.schema.json` 校验
 * （长度未超限、选择题值在选项内、无多余 key）」。
 *
 * 这里只做**必答 + 长度**这两条最容易拦的：选择题的"值在选项内"由控件本身保证，
 * "无多余 key"由前端构造 `values` 的方式保证。
 *
 * ⚠️ **权威校验在服务端。** 前端校验是体验（不用白跑一趟），不是正确性保证 ——
 * 绕过前端直接打接口一样要被服务端拦下。
 *
 * **导出**是给 `App.tsx` 的 `handleStartConversation` 复用的：那个入口将来也可能被
 * "重试开场"之类的地方调用，不能假设调用方一定校验过。规则只有这一份。
 */
export function validateForm(
  questions: QuestionConfig[],
  values: FormValues,
): Record<string, string> {
  const errors: Record<string, string> = {}
  for (const question of questions) {
    const value = (values[question.id] ?? '').trim()

    if (!value) {
      if (question.required) errors[question.id] = '这一题必答'
      continue
    }
    if (question.maxLength !== null && value.length > question.maxLength) {
      errors[question.id] = `最多 ${question.maxLength} 字，当前 ${value.length} 字`
    }
  }
  return errors
}

function controlClass(hasError: boolean, extra = ''): string {
  return [
    'w-full rounded-lg border bg-white px-3 py-2 text-sm text-slate-900 outline-none transition',
    'focus:ring-2 disabled:cursor-not-allowed disabled:bg-slate-50',
    hasError
      ? 'border-rose-300 focus:border-rose-400 focus:ring-rose-100'
      : 'border-slate-300 focus:border-primary-400 focus:ring-primary-100',
    extra,
  ]
    .filter(Boolean)
    .join(' ')
}

interface ControlProps {
  question: QuestionConfig
  value: string
  controlId: string
  errorId: string
  hasError: boolean
  disabled: boolean
  onChange: (value: string) => void
}

/** 按 `type` 渲染控件。四种：text / textarea / select / radio。 */
function renderControl({
  question,
  value,
  controlId,
  errorId,
  hasError,
  disabled,
  onChange,
}: ControlProps) {
  const describedBy = hasError ? errorId : undefined

  switch (question.type) {
    case 'textarea':
      return (
        <textarea
          id={controlId}
          value={value}
          rows={4}
          required={question.required}
          maxLength={question.maxLength ?? undefined}
          disabled={disabled}
          aria-invalid={hasError}
          aria-describedby={describedBy}
          onChange={(event) => onChange(event.target.value)}
          className={controlClass(hasError, 'resize-y leading-relaxed')}
        />
      )

    case 'select':
      return (
        <select
          id={controlId}
          value={value}
          required={question.required}
          disabled={disabled}
          aria-invalid={hasError}
          aria-describedby={describedBy}
          onChange={(event) => onChange(event.target.value)}
          className={controlClass(hasError, value ? '' : 'text-slate-400')}
        >
          <option value="">请选择…</option>
          {question.options.map((option) => (
            <option key={option} value={option} className="text-slate-900">
              {option}
            </option>
          ))}
        </select>
      )

    case 'radio':
      return (
        <div
          id={controlId}
          role="radiogroup"
          aria-invalid={hasError}
          aria-describedby={describedBy}
          className="flex flex-col gap-1.5"
        >
          {question.options.map((option, index) => {
            const optionId = `${controlId}-opt-${index}`
            const selected = value === option
            return (
              <label
                key={option}
                htmlFor={optionId}
                className={[
                  'flex cursor-pointer items-center gap-2.5 rounded-lg border px-3 py-2 text-sm transition',
                  selected
                    ? 'border-primary-400 bg-primary-50 text-primary-900'
                    : 'border-slate-200 bg-white text-slate-700 hover:border-slate-300 hover:bg-slate-50',
                  disabled ? 'cursor-not-allowed opacity-60' : '',
                ]
                  .filter(Boolean)
                  .join(' ')}
              >
                <input
                  id={optionId}
                  type="radio"
                  name={controlId}
                  value={option}
                  checked={selected}
                  disabled={disabled}
                  onChange={() => onChange(option)}
                  className="h-4 w-4 shrink-0 accent-primary-600"
                />
                {option}
              </label>
            )
          })}
        </div>
      )

    default:
      return (
        <input
          id={controlId}
          type="text"
          value={value}
          required={question.required}
          maxLength={question.maxLength ?? undefined}
          disabled={disabled}
          aria-invalid={hasError}
          aria-describedby={describedBy}
          onChange={(event) => onChange(event.target.value)}
          className={controlClass(hasError)}
        />
      )
  }
}

interface FieldProps {
  question: QuestionConfig
  value: string
  error?: string
  disabled: boolean
  onChange: (value: string) => void
}

function Field({ question, value, error, disabled, onChange }: FieldProps) {
  const controlId = `field-${question.id}`
  const errorId = `${controlId}-error`
  const hasError = Boolean(error)
  const isChoice = question.type === 'select' || question.type === 'radio'
  // 选择题没有"长度"概念（后端 maxLength 为 null），所以只有文本框显示计数器
  const showCounter = question.maxLength !== null && !isChoice
  // 单选组用 role=radiogroup + aria-labelledby，label 的 htmlFor 只对单体控件有效
  const labelId = `${controlId}-label`

  return (
    <div id={`anchor-${question.id}`} className="scroll-mt-24">
      <div className="flex items-baseline justify-between gap-3">
        <label
          id={labelId}
          htmlFor={question.type === 'radio' ? undefined : controlId}
          className="text-sm font-medium text-slate-800"
        >
          {question.label}
          {question.required && (
            <span className="ml-1 text-rose-600" title="必答" aria-label="必答">
              *
            </span>
          )}
        </label>
        {showCounter && (
          <span
            className={[
              'shrink-0 text-xs tabular-nums',
              hasError ? 'text-rose-600' : 'text-slate-400',
            ].join(' ')}
          >
            {value.length}/{question.maxLength}
          </span>
        )}
      </div>

      <p className="mt-0.5 text-xs leading-relaxed text-slate-500">{question.description}</p>

      <div className="mt-2" aria-labelledby={question.type === 'radio' ? labelId : undefined}>
        {renderControl({ question, value, controlId, errorId, hasError, disabled, onChange })}
      </div>

      {error && (
        <p id={errorId} className="mt-1.5 flex items-center gap-1 text-xs text-rose-600">
          <AlertCircle className="h-3.5 w-3.5 shrink-0" aria-hidden />
          {error}
        </p>
      )}
    </div>
  )
}

/**
 * 表单步骤：渲染 20 题并做前端校验。
 *
 * - 基础题平铺；**高级题默认折叠**（后端配置里的建议，`questions_config.json` 的 description）
 * - 必答题的标签后带红色星号
 * - 提交时校验，有错则展开高级题（若错在那边）并把第一个错误滚进视野
 */
export default function FormStep({
  questions,
  values,
  onFieldChange,
  onSubmit,
  submitting = false,
}: FormStepProps) {
  const [errors, setErrors] = useState<Record<string, string>>({})
  const [showAdvanced, setShowAdvanced] = useState(false)

  const allQuestions = useMemo(
    () => [...questions.base_questions, ...questions.advanced_questions],
    [questions],
  )
  const requiredQuestions = useMemo(
    () => allQuestions.filter((question) => question.required),
    [allQuestions],
  )
  const answeredRequired = requiredQuestions.filter(
    (question) => (values[question.id] ?? '').trim().length > 0,
  ).length
  const errorCount = Object.keys(errors).length

  function handleChange(id: string, value: string) {
    onFieldChange(id, value)
    // 用户一改就清掉这一题的报错，别让红字挂着碍眼
    if (errors[id]) {
      setErrors((prev) => {
        const next = { ...prev }
        delete next[id]
        return next
      })
    }
  }

  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const found = validateForm(allQuestions, values)
    setErrors(found)

    if (Object.keys(found).length === 0) {
      onSubmit(values)
      return
    }

    // 有错：如果错在折叠着的高级题里，先展开，否则用户看不到红字
    const firstBad = allQuestions.find((question) => found[question.id])
    if (!firstBad) return
    if (questions.advanced_questions.some((question) => question.id === firstBad.id)) {
      setShowAdvanced(true)
    }
    // 等展开完再滚，否则滚到的位置会因为布局变化而错位
    requestAnimationFrame(() => {
      document
        .getElementById(`anchor-${firstBad.id}`)
        ?.scrollIntoView({ behavior: 'smooth', block: 'center' })
    })
  }

  return (
    <form onSubmit={handleSubmit} noValidate className="flex flex-col gap-5">
      <section className="rounded-xl border border-slate-200 bg-white p-6 shadow-sm">
        <div className="mb-5 flex items-baseline justify-between gap-3">
          <h2 className="text-base font-medium text-slate-800">基础问题</h2>
          <span className="shrink-0 text-xs tabular-nums text-slate-400">
            必答 {answeredRequired}/{requiredQuestions.length}
          </span>
        </div>
        <div className="flex flex-col gap-5">
          {questions.base_questions.map((question) => (
            <Field
              key={question.id}
              question={question}
              value={values[question.id] ?? ''}
              error={errors[question.id]}
              disabled={submitting}
              onChange={(value) => handleChange(question.id, value)}
            />
          ))}
        </div>
      </section>

      <section className="rounded-xl border border-slate-200 bg-white shadow-sm">
        <button
          type="button"
          onClick={() => setShowAdvanced((prev) => !prev)}
          aria-expanded={showAdvanced}
          className="flex w-full items-center gap-2 rounded-xl px-6 py-4 text-left transition hover:bg-slate-50 focus:outline-none focus-visible:ring-2 focus-visible:ring-primary-300"
        >
          {showAdvanced ? (
            <ChevronDown className="h-4 w-4 shrink-0 text-slate-400" aria-hidden />
          ) : (
            <ChevronRight className="h-4 w-4 shrink-0 text-slate-400" aria-hidden />
          )}
          <span className="text-base font-medium text-slate-800">高级问题</span>
          <span className="text-xs text-slate-400">
            {questions.advanced_questions.length} 题，全部选填
          </span>
          <span className="ml-auto text-xs text-primary-700">
            {showAdvanced ? '收起' : '展开'}
          </span>
        </button>

        {showAdvanced && (
          <div className="flex flex-col gap-5 border-t border-slate-100 px-6 py-5">
            {questions.advanced_questions.map((question) => (
              <Field
                key={question.id}
                question={question}
                value={values[question.id] ?? ''}
                error={errors[question.id]}
                disabled={submitting}
                onChange={(value) => handleChange(question.id, value)}
              />
            ))}
          </div>
        )}
      </section>

      <div className="flex items-center gap-4">
        <button
          type="submit"
          disabled={submitting}
          className="inline-flex items-center gap-2 rounded-lg bg-primary-600 px-5 py-2.5 text-sm font-medium text-white transition hover:bg-primary-700 focus:outline-none focus-visible:ring-2 focus-visible:ring-primary-400 disabled:cursor-not-allowed disabled:opacity-60"
        >
          {submitting ? (
            <span className="h-4 w-4 animate-spin rounded-full border-2 border-white/40 border-t-white" />
          ) : (
            <Send className="h-4 w-4" aria-hidden />
          )}
          提交
        </button>

        {errorCount > 0 && (
          <span className="flex items-center gap-1 text-sm text-rose-600" role="alert">
            <AlertCircle className="h-4 w-4 shrink-0" aria-hidden />
            还有 {errorCount} 项没填好
          </span>
        )}
      </div>
    </form>
  )
}
