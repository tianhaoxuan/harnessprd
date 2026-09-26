// ⚠️ 这个文件原来导出的是 `FormStep`（把 20 题**逐题**渲染成表单的组件）。
// 流程的第一步已经换成 `StructuredForm`（结构化录入），**那个组件已删除** ——
// 它当时占了这个文件 400 行里的 345 行，而全项目只剩注释里提过它一次。
//
// 留在这里的两个符号仍然活着，所以文件保留：
// - `FormValues`：提交契约，`App.tsx` 与 `StructuredForm` 都在用；
// - `validateForm`：服务端 guard 的前端镜像，`App.tsx` 的 `handleStartConversation` 在用。
//
// 要恢复到"20 题逐题渲染"，得按 git 历史把组件重建（校验规则还在，重建的是纯 UI）。
import type { QuestionConfig } from '../types'

/** 表单值：键 = 题目 **id**，值 = 作答。与后端 `form: dict[str, str]` 同形（扁平，不嵌套）。 */
export type FormValues = Record<string, string>

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
