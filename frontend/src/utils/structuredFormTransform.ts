/**
 * 结构化表单的**文本工具 + 老数据迁移**（05 篇）。
 *
 * ## 这里放什么
 *
 * 1. `parseList` / `splitFields`：多行文本的两个基础操作。它们原先定义在
 *    `StructuredForm.tsx` 里，改行编辑器之后**迁移逻辑也要用**（把老的管道符字符串
 *    切回行数组），所以搬到 util 层由组件反向 import —— 方向对（组件 → utils），
 *    而且只有一份实现（两份 `splitFields` 迟早对"空列算不算存在"给出不同答案）。
 * 2. `legacyParseMvpFeatures` / `legacyParseUiPages`：老格式（管道符）→ 行数组。
 * 3. `normalizeRowFields`：**任意**形状的 `mvpFeatures` / `pages` → 行数组。
 *    表单挂载、示例填充、草稿恢复三处都走它，所以"老 Session 打开就能用"这件事
 *    只有一条代码路径。
 *
 * ## 迁移之后不再向用户暴露管道符格式
 *
 * 老 Session 里存的仍是 `"功能名｜描述｜价值"` 这种字符串（也可能是一份更老的草稿）。
 * `normalizeRowFields` 把它们切开成行；写回时写的已经是行数组，所以**下一次保存就完成迁移**，
 * 用户不需要做任何事，也不需要知道曾经有过那个格式。
 */

import {
  emptyMvpFeatureRow,
  emptyUiPageRow,
  type MvpFeatureRow,
  type UiPageRow,
} from '../types/structuredForm'

/**
 * 按行拆，**丢掉空行**。
 *
 * 行号按"去掉空行之后"的序号算 —— 与用户看到的"第 N 条"一致
 * （直接用原始行号会因为空行而错位）。
 */
export function parseList(text: string): string[] {
  return (text ?? '')
    .split('\n')
    .map((line) => line.trim())
    .filter(Boolean)
}

/**
 * 按竖线拆列。
 *
 * 中文竖线 `｜` 与半角竖线 `|` 都认 —— 中文输入法下打出的是前者，从别处粘贴过来的
 * 往往是后者，只认一种会让人对着"格式没错但报格式错"发呆。
 * 拆完**保留空列不清除**：`功能名｜` 的第二列要能被判成"存在但为空"。
 */
export function splitFields(line: string): string[] {
  return line.split(/[｜|]/).map((cell) => cell.trim())
}

/**
 * 老格式的 MVP 功能 → 行数组：每行 `功能名｜描述｜价值｜优先级`（后两列可省略）。
 *
 * ⚠️ **第 4 列（`P0`/`P1`/`P2`）在这里被丢掉**：新行结构没有这一列，见
 * `types/structuredForm.ts` 里 `MvpFeatureRow` 的说明。旧实现还会顺手识别
 * "只写三列、第三列写优先级"的写法（那会被当成"价值：P0"）—— 新结构下第三列就是
 * 价值，所以那种老写法里的 `P0` 会作为**价值**留下（不再被搬到优先级上）。
 */
export function legacyParseMvpFeatures(text: string): MvpFeatureRow[] {
  return parseList(text).map((line) => {
    const cells = splitFields(line)
    return {
      name: cells[0] ?? '',
      description: cells[1] ?? '',
      user_value: cells[2] ?? '',
    }
  })
}

/**
 * 老格式的页面结构 → 行数组：每行 `页面名｜模块1,模块2｜备注`（后两列可省略）。
 *
 * 第 2 列的模块**保持原样**（逗号分隔的字符串），交给
 * `buildRequirementsSummary()` 去拆 —— 那边本来就要认中英文逗号与顿号。
 */
export function legacyParseUiPages(text: string): UiPageRow[] {
  return parseList(text).map((line) => {
    const [name = '', modules = '', notes = ''] = splitFields(line)
    return { name, modules, notes }
  })
}

/** 把任意值收敛成行数组：数组照收（逐行补缺键），别的（含老字符串）走迁移。 */
function toMvpRows(value: unknown): MvpFeatureRow[] {
  if (typeof value === 'string') return legacyParseMvpFeatures(value)
  if (!Array.isArray(value)) return []
  return value.map((item) => {
    const row = (item ?? {}) as Partial<MvpFeatureRow>
    return {
      name: typeof row.name === 'string' ? row.name : '',
      description: typeof row.description === 'string' ? row.description : '',
      user_value: typeof row.user_value === 'string' ? row.user_value : '',
    }
  })
}

function toUiPageRows(value: unknown): UiPageRow[] {
  if (typeof value === 'string') return legacyParseUiPages(value)
  if (!Array.isArray(value)) return []
  return value.map((item) => {
    const row = (item ?? {}) as Partial<UiPageRow>
    return {
      name: typeof row.name === 'string' ? row.name : '',
      // 老草稿里 `modules` 可能是**数组**（更早的一版按数组存过），这里一并收掉
      modules: Array.isArray(row.modules)
        ? (row.modules as unknown[]).filter((m): m is string => typeof m === 'string').join('，')
        : typeof row.modules === 'string'
          ? row.modules
          : '',
      notes: typeof row.notes === 'string' ? row.notes : '',
    }
  })
}

/**
 * 把任意形状的 `mvpFeatures` / `pages` 归一化成行数组（**唯一的迁移入口**）。
 *
 * 三种输入都认：
 *
 * | 输入 | 来源 |
 * | --- | --- |
 * | `"功能名｜描述"` 字符串 | 老 Session 的快照 / 老本机草稿 |
 * | 行数组 | 05 之后写的快照与草稿 |
 * | 别的（`undefined` / 数字 / 坏数据） | 一律给**一行空行**，让用户能直接开始输入 |
 *
 * ⚠️ 空数组也补一行空行：`[]` 渲染出来是一个没有任何输入框的空块，用户会以为坏了
 * （需求 §六 的"仅剩一行时删除 = 清空该行，不删到 0 行"就是同一个理由）。
 */
export function normalizeRowFields(raw: unknown): {
  mvpFeatures: MvpFeatureRow[]
  pages: UiPageRow[]
} {
  const source = (raw ?? {}) as { mvpFeatures?: unknown; pages?: unknown }
  const mvpFeatures = toMvpRows(source.mvpFeatures)
  const pages = toUiPageRows(source.pages)
  return {
    mvpFeatures: mvpFeatures.length > 0 ? mvpFeatures : [emptyMvpFeatureRow()],
    pages: pages.length > 0 ? pages : [emptyUiPageRow()],
  }
}
