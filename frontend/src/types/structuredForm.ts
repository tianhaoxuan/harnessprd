/**
 * 结构化表单的**行类型**（05 篇）。
 *
 * ## 为什么只有两个类型
 *
 * 表单自己的字段集合是 `StructuredForm.tsx` 里的 `Values`（驼峰命名：`mvpFeatures` /
 * `pages` / `productName`…）。需求给的那份 `StructuredFormData`（蛇形命名：
 * `mvp_features` / `ui_pages` / `product_name`）与它**是同一批字段的两个名字** ——
 * 而字段名是 `Values` 的内部实现，改名要动 19 个字段 + `EMPTY` + `FIELD_LABELS` +
 * 全部 JSX，纯 churn 且没有任何用户可见收益。
 *
 * 所以这里只抽出**真正新增**的东西：两个行的类型。`buildRequirementsSummary()`
 * 负责把它们映射成 `RequirementsSummary` / `skillPayload`（形状以
 * `skills/prd-generator/references/field-schema.json` 为准），那一层不受命名影响。
 */

/**
 * MVP 功能的一行。
 *
 * ⚠️ 与改造前那行管道符相比**少了一列**：旧的 `功能名｜描述｜价值｜优先级` 第 4 列
 * `P0/P1/P2` 会进 `skillPayload.mvp_features[].priority`（下游的 FR 编号、接口文档
 * 的 P0 覆盖、套件的覆盖矩阵都吃它）。新行结构没有优先级这一列，所以
 * **老数据迁移过来时那一列会被丢掉**，此后所有 MVP 功能的 `priority` 都是空。
 * 这不会报错（schema 里 `priority` 可选，规则是"不给按 P0 处理"），但 P0/P1 的区分
 * 从此只能靠模型判断 —— 要恢复这一列入参，得先确认 UI 上放哪。
 */
export interface MvpFeatureRow {
  name: string
  description: string
  user_value: string
}

/**
 * 页面结构的一行。
 *
 * `modules` 刻意是**一个字符串**（逗号分隔）而不是数组：界面上就是一个单行输入框，
 * 让它在受控组件里跟数组来回转换，只会让"打了一个逗号光标就跳"这类问题变多。
 * 拆成数组发生在 `buildRequirementsSummary()` 里，那边本来就要处理中英文逗号与顿号。
 */
export interface UiPageRow {
  name: string
  modules: string
  notes: string
}

/** 一个空的 MVP 行（新增行 / 初始行都用它，避免到处写三遍空串）。 */
export function emptyMvpFeatureRow(): MvpFeatureRow {
  return { name: '', description: '', user_value: '' }
}

/** 一个空的页面行。 */
export function emptyUiPageRow(): UiPageRow {
  return { name: '', modules: '', notes: '' }
}

/** 这一行**整个是空的**吗（三列都只有空白）。用于校验前过滤、以及删除行的判断。 */
export function isBlankMvpRow(row: MvpFeatureRow): boolean {
  return !row.name.trim() && !row.description.trim() && !row.user_value.trim()
}

/** 同上，页面行。 */
export function isBlankUiPageRow(row: UiPageRow): boolean {
  return !row.name.trim() && !row.modules.trim() && !row.notes.trim()
}
