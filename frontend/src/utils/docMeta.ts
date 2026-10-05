/**
 * 三份产物的元信息表（**唯一来源**）。
 *
 * ## 为什么从 `App.tsx` 搬到这里
 *
 * 这张表原先定义在 `App.tsx` 里，只有它一个使用者。接入 Generation Job 之后
 * 多了一个使用者：`utils/jobViews.ts` 要把「Job 的 artifact」映射成
 * 「产物 kind / 生成中视图 / 审核视图」。而 `App.tsx → hook → jobViews`，
 * 让 `jobViews` 反过来 import `App.tsx` 就成环了。
 *
 * 搬出来而不是在 `jobViews` 里再写一份：**「哪种产物 ↔ 哪两个视图 ↔ 哪个标题」
 * 是同一件事**，两份表迟早出现"页面按 review-api-docs 走、任务按 review-prompts 走"
 * 这种对不上的状态，而且不会有任何报错。
 */

import type { DocKind, ViewState } from '../types'

/**
 * 三份产物的元信息。
 *
 * 把「哪种产物 ↔ 哪两个视图 ↔ 哪个标题」收在一张表里，是因为 App 里到处都要按 kind
 * 分支（生成、优化、状态推导、恢复）。散成七八处 `if (kind === 'prd')` 之后，
 * 加第四个产物就会漏掉其中一两处。
 */
export const DOC_META: Record<
  DocKind,
  {
    title: string
    /** 生成中的视图（进度条与"正在生成…"用） */
    generating: ViewState
    /** 产出后的审核视图 */
    review: ViewState
    /** 生成按钮文案 */
    generateLabel: string
    /** 它依赖哪些上游（用于按钮禁用提示与错误文案） */
    requiresPrd: boolean
    /**
     * 下载时的文件名主干（不含扩展名）。实际文件名是 `{产品名}-{主干}.md`。
     *
     * 刻意**不用 `title`**：那个带全角括号与"（产品需求文档）"这种说明，拼出来是
     * `群聊周报助手-PRD（产品需求文档）.md`，长且啰嗦。
     */
    fileStem: string
  }
> = {
  prd: {
    title: 'PRD（产品需求文档）',
    generating: 'generating-prd',
    review: 'review-prd',
    generateLabel: '生成 PRD',
    requiresPrd: false,
    fileStem: 'PRD',
  },
  api: {
    title: '接口文档',
    generating: 'generating-api-docs',
    review: 'review-api-docs',
    generateLabel: '生成接口文档',
    requiresPrd: true,
    fileStem: '接口文档',
  },
  prompts: {
    title: '提示词套件',
    generating: 'generating-prompts',
    review: 'review-prompts',
    generateLabel: '生成提示词套件',
    requiresPrd: true,
    fileStem: '提示词套件',
  },
}

/** 链条顺序：PRD 是唯一源头，接口文档从 PRD 推导，套件消费前两者（`HANDOFF.md` §1）。 */
export const DOC_ORDER: DocKind[] = ['prd', 'api', 'prompts']

/**
 * `run_summary.run_type` → 它属于哪一份产物（03）。
 *
 * ⚠️ **必须有这张表。** `run_summary` 是**一份共享 state**，而澄清流与三份产物都会往里写；
 * 不按 `run_type` 过滤的话，聊完天切到 PRD 页就会看到"澄清那一轮"的统计挂在 PRD 正文下面
 * —— 数字是真的、但说的是别的事。
 *
 * 取值来自后端 `api/conversation.py` 里那 8 处 `run_type=`（PRD 有两个入口：
 * 带双智能体审核的 `generate_prd_from_summary` 与表单路径的 `generate_prd`）。
 * ⚠️ 任务化之后 PRD 只走 `generate_prd_from_summary`（后端 `job_models.ARTIFACT_RUN_TYPE`），
 * 两个值都留着是因为**旧的前台接口还在**（标了 deprecated 但没删）。
 */
export const DOC_RUN_TYPES: Record<DocKind, readonly string[]> = {
  prd: ['generate_prd', 'generate_prd_from_summary'],
  api: ['generate_api_docs'],
  prompts: ['generate_prompts'],
}
