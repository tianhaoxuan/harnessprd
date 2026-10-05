/**
 * 顶栏「产出物」进度条的**纯推导**。
 *
 * ## 为什么从 `StepProgress` 换成这一套
 *
 * `StepProgress` 画的是「流程进度」—— 一格 = 一个 `ViewState` 的投影，所以它只能回答
 * 「我走到第几步」。那条进度有几个说不过去的地方：
 *
 * 1. **把"已有正文"和"走到哪"混成一件事**：从导入入口进来的 PRD 是用户贴的、
 *    一个字都没生成过，流程条照样写着"PRD 已完成"；反过来，用户跳到接口文档那屏看
 *    一眼，PRD 就会退成"未完成"。
 * 2. **点得动的格子没意义**：`StepProgress` 的每一格都能点（`onSelect` 一律给），
 *    点一份**还没生成的**接口文档会落到一张空审阅页上。
 * 3. 格子数按入口裁剪这件事本身是对的，要保留。
 *
 * 这一套改成：**格子 = 产出物**，`done` 由"这份产物有没有正文"推导，
 * 只有 `done` 的格子可点（点它去对应的 `review-*`）。
 *
 * ## 这个文件**刻意是纯函数**
 *
 * 不 import 任何 hook / 组件，连回调都是入参带进来的（`onNavigate`）：
 * 于是它可以离线渲染、可以单测、状态全由内容推导而**不另存一份** ——
 * 刷新后不会出现"进度条说做完了、正文却是空的"这种两份真相打架的情况。
 *
 * 与 `types/index.ts` 的 `stepsForMode` / `stepIndexOf` 的分工：那两个函数与
 * `StepProgress.tsx` **留着不动**（本次改动可一键回退），只是不再有人调用。
 */

import type { EntryMode, ViewState } from '../types'

// ---------------------------------------------------------------- 类型

/** 一份产出物。**不是** `DocKind` —— 前两格（需求、澄清）不是文档产物。 */
export type ArtifactId = 'requirements' | 'clarification' | 'prd' | 'api-docs' | 'prompts'

/**
 * 一格的状态。**三者互斥**（`active` 压过 `done`）。
 *
 * | 值 | 含义 | 可点 |
 * | --- | --- | --- |
 * | `pending` | 还没有正文 | 否（**必须给 `title` 说明原因**，否则用户点不动又不知道为什么） |
 * | `active` | 当前就在这一屏 | 否（点了也是原地不动） |
 * | `done` | 已有正文 | 是（跳到对应的 `review-*`） |
 */
export type ArtifactStatus = 'pending' | 'active' | 'done'

export interface ArtifactNode {
  id: ArtifactId
  label: string
  status: ArtifactStatus
  clickable: boolean
  onClick?: () => void
  title?: string
}

/** 判断「这一份产出物有没有正文」所需的全部输入（其余状态一概不进这个函数）。 */
export interface ArtifactContents {
  /** 表单里有没有有效的产品名（`structured` 的 `requirements` 判据之一）。 */
  hasProductName: boolean
  /** 澄清对话里有内容（`messages` 非空）。 */
  hasClarification: boolean
  /**
   * 已经发生的对话轮次（`roundIndex`）。
   *
   * ⚠️ 它严格来说不是"正文"，进这个函数是**产品口径拍板的结果**：
   * `requirements` 格要表达"需求已经录进来了"，而"进过澄清页"这件事没有单独的状态
   * —— `viewState` 只记"现在在哪一屏"，用户聊完走到 `review-prd` 之后，
   * "他确实聊过"就查不到了。所以用 `roundIndex > 1`（至少聊过一轮）兜住
   * "聊过但还没产出 PRD"这一段。
   */
  roundIndex: number
  prdContent: string
  apiDocsContent: string
  promptsContent: string
}

/** 每份产出物的格子文案。 */
const ARTIFACT_LABEL: Record<ArtifactId, string> = {
  requirements: '需求',
  clarification: '澄清',
  prd: 'PRD',
  'api-docs': '接口文档',
  prompts: '提示词',
}

/**
 * 产出物的**规范顺序**。
 *
 * 用途只有一个：`viewState` 指向的那一格在当前入口里**不存在**时（例：
 * `prompts-debug` 没插"接口文档"那一格，却落到了 `review-api-docs`），
 * 往前找最近的格子当"当前"。不是为了排序渲染 —— 渲染顺序由 `artifactIdsFor` 给。
 */
const ARTIFACT_ORDER: ArtifactId[] = [
  'requirements',
  'clarification',
  'prd',
  'api-docs',
  'prompts',
]

/**
 * `ViewState` → 当前那一格。
 *
 * ⚠️ 写成**全量 Record**而不是 `switch`：`ViewState` 加一屏时这里会**编译期**报缺键，
 * 而 `switch` 只会静默走到 `default`（表现为"新那一屏没有当前格"，看不出来是漏了）。
 */
const ACTIVE_BY_VIEW: Record<ViewState, ArtifactId | null> = {
  // `form` 那一屏是「该入口的第一份产出物」：structured 是需求，两个导入入口是 PRD。
  // 所以它不在这里给死值，由 `resolveActive` 按裁剪后的格子表现取第一格。
  form: null,
  chatting: 'clarification',
  'generating-prd': 'prd',
  'review-prd': 'prd',
  'generating-api-docs': 'api-docs',
  'review-api-docs': 'api-docs',
  'generating-prompts': 'prompts',
  'review-prompts': 'prompts',
  // 完成页：没有"当前这一份"，全都按内容点亮
  done: null,
}

// ---------------------------------------------------------------- 裁剪

/**
 * 这个入口显示哪几格。
 *
 * | 入口 | 格子 |
 * | --- | --- |
 * | `structured` | 需求 → 澄清 → PRD → 接口文档 → 提示词（5） |
 * | `prd-shortcut` | PRD → 接口文档 → 提示词（3） |
 * | `prompts-debug` | PRD → 提示词（2）；**已导入接口文档时**在中间插一格接口文档 |
 *
 * ⚠️ `prompts-debug` 那一格是**按内容**插的，不是按入口插的：这条入口本来就不生成
 * 接口文档，但用户可能从"导入 PRD+接口文档"里真的贴了一份进去 —— 那时它确实是一份
 * 存在的产出物，漏掉它会让顶栏少显示一份已经有的东西。
 */
function artifactIdsFor(entryMode: EntryMode, contents: ArtifactContents): ArtifactId[] {
  switch (entryMode) {
    case 'prd-shortcut':
      return ['prd', 'api-docs', 'prompts']
    case 'prompts-debug':
      return contents.apiDocsContent.trim()
        ? ['prd', 'api-docs', 'prompts']
        : ['prd', 'prompts']
    default:
      return ['requirements', 'clarification', 'prd', 'api-docs', 'prompts']
  }
}

// ---------------------------------------------------------------- 判据

/** 这一份产出物有没有正文。 */
function isArtifactDone(id: ArtifactId, contents: ArtifactContents): boolean {
  switch (id) {
    case 'requirements':
      // ⚠️ 「填了产品名」还不够：还得**往下走过**（聊过 / 有 PRD / 至少聊过一轮），
      // 否则用户刚在产品名那一栏敲一个字，需求格就打勾了。
      //
      // 判据里没有"曾经进入过澄清页"这个更准的说法，因为它**没有现成状态**：
      // `viewState` 只记"现在在哪一屏"，用户从 chatting 走到 review-prd 之后就查不到了。
      // 三个可选项里挑的是"最省事、且不会出现'进过澄清页却一个字没说、格子却打勾'"的那个。
      return (
        contents.hasProductName &&
        (contents.hasClarification ||
          contents.prdContent.trim().length > 0 ||
          contents.roundIndex > 1)
      )
    case 'clarification':
      return contents.hasClarification
    case 'prd':
      return contents.prdContent.trim().length > 0
    case 'api-docs':
      return contents.apiDocsContent.trim().length > 0
    case 'prompts':
      return contents.promptsContent.trim().length > 0
  }
}

/**
 * 当前那一格在**这个入口裁剪后的**格子表里的落点。
 *
 * 正常情况就是 `ACTIVE_BY_VIEW[viewState]`；只有三种例外：
 * - `form` → 第一格（见 `ACTIVE_BY_VIEW` 的说明）；
 * - `done` / 该值本来就是 `null` → 没有当前格；
 * - 指向的那一格**不在**这张表里（`prompts-debug` 没插接口文档、却落到了
 *   `review-api-docs`）→ 往前取最近的格子。**不返回 `null`** ——
 *   整条进度条一格都不亮，用户会以为进度条坏了。
 */
function resolveActive(viewState: ViewState, ids: ArtifactId[]): ArtifactId | null {
  if (viewState === 'form') return ids[0] ?? null

  const target = ACTIVE_BY_VIEW[viewState]
  if (target === null) return null
  if (ids.includes(target)) return target

  const order = ARTIFACT_ORDER.indexOf(target)
  for (let index = ids.length - 1; index >= 0; index -= 1) {
    if (ARTIFACT_ORDER.indexOf(ids[index]) <= order) return ids[index]
  }
  return ids[0] ?? null
}

// ---------------------------------------------------------------- 组装

/**
 * 组装顶栏要渲染的那一排格子。
 *
 * @param onNavigate 每个格子该跳去哪；返回 `undefined` = 这一屏没有对应去处。
 *                   **只对 `done` 的格子调用** —— 调用方因此不必自己判断状态。
 */
export function buildArtifactNodes(args: {
  entryMode: EntryMode
  viewState: ViewState
  contents: ArtifactContents
  onNavigate: (id: ArtifactId) => (() => void) | undefined
}): ArtifactNode[] {
  const { entryMode, viewState, contents, onNavigate } = args
  const ids = artifactIdsFor(entryMode, contents)
  const active = resolveActive(viewState, ids)

  return ids.map((id) => {
    // `active` **压过** `done`：同一格既"有正文"又"就是当前这一屏"时，
    // 该告诉用户的是"你在这儿"（否则当前屏就没有任何标记了）。
    const status: ArtifactStatus =
      id === active ? 'active' : isArtifactDone(id, contents) ? 'done' : 'pending'

    const onClick = status === 'done' ? onNavigate(id) : undefined
    const clickable = status === 'done' && typeof onClick === 'function'

    return {
      id,
      label: ARTIFACT_LABEL[id],
      status,
      clickable,
      onClick,
      title:
        status === 'pending'
          ? '尚未生成'
          : status === 'done' && !clickable
            ? '这一屏没有可以打开的位置'
            : undefined,
    }
  })
}
