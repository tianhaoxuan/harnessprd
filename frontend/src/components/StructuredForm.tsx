/**
 * 结构化录入表单 —— 流程的第一步（原来独立成一个 AppV2 界面，现在并进 V1 的向导）。
 *
 * 与 `FormStep`（按 20 题逐题渲染）的区别：这里按 `field-schema.json` 的字段组织成 4 个折叠区块，
 * 支持多行格式化输入（每行 `功能名｜描述｜价值｜优先级`）、一键填示例、以及 JSON 预览。
 *
 * ## 它只负责"填 + 转"，不负责生成
 *
 * 提交时把表单转成两样东西交给上层（`App.tsx`）：
 * 1. `form`：**20 题形状**的作答 —— 对话澄清与生成都吃这个形状，所以字段必须映射成题目 id；
 * 2. `extras`：20 题里没有对应题目的那部分（页面结构、关键交互、范围、LLM 说明、可用性），
 *    由上层并进 `known_info` 一起发给生成接口。
 *
 * 生成、审核、分片、完成页都在上层（V1 的向导）里 —— 这个组件**刻意不碰任何接口**，
 * 这样"提示词怎么组装、产物怎么审核"只有一套实现。
 */

import { useCallback, useEffect, useMemo, useState } from 'react'
import { Check, ChevronDown, ChevronRight, Code, Copy, FileText, Loader2 } from 'lucide-react'

import { clearLocal, readLocal, writeLocal } from '../services/storage'
import type { FormValues } from './FormStep'

/**
 * 本组件自己的草稿键。
 *
 * ⚠️ 与 `FormStep` 用的 `draft` **分开**：那个键存的是"映射后的 20 题作答"（供会话恢复），
 * 这里存的是"用户手填的结构化内容"。共用一个键的话，两套值会互相覆盖。
 */
const DRAFT_KIND = 'structured-form-draft'

/** 草稿写回的防抖：localStorage 是同步 API，不该逐键写（同 `FormStep` 的理由）。 */
const DRAFT_DEBOUNCE_MS = 400

// ---------------------------------------------------------------- 选项

/**
 * 下拉框取值。
 *
 * `label` = 界面显示的文字，`value` = **发给后端的原话**（取自 `questions_config.json`）。
 * 两者分开是有意的：界面可以给人看的话，发给后端的必须是提示词里做字面比较的那几句。
 * 目前两者相同，保留这层结构是为了以后改文案时不会顺手改掉协议值。
 */
interface Option {
  label: string
  value: string
}

const PLATFORMS: Option[] = [
  { label: '网页应用', value: 'Web 网站' },
  { label: '微信小程序', value: '微信小程序' },
  { label: 'App', value: '移动 App' },
  { label: 'CLI', value: 'CLI 命令行工具' },
]

const AUTH_OPTIONS: Option[] = [
  { label: '账号密码注册登录', value: '需要：账号密码注册登录' },
  { label: '仅第三方登录', value: '需要：仅第三方登录（微信 / GitHub 等）' },
  { label: '无认证', value: '不需要：打开即用' },
  { label: '待定', value: '还不确定' },
]

const STORAGE_OPTIONS: Option[] = [
  { label: '关系型数据库', value: '需要：要长期保存用户数据' },
  { label: '仅少量配置或缓存', value: '需要：只存少量配置或缓存' },
  { label: '无持久化', value: '不需要：纯前端运行即可' },
  { label: '待定', value: '还不确定' },
]

/** 页面数量题的取值（`page_count` 是下拉题，只能填它自己的选项）。 */
function pageCountValue(lines: number): string {
  if (lines <= 0) return ''
  if (lines === 1) return '1 个单页'
  if (lines <= 3) return '2–3 个'
  if (lines <= 8) return '4–8 个'
  return '8 个以上'
}

// ---------------------------------------------------------------- 表单状态

interface Values {
  // 产品与用户（必填）
  productName: string
  platform: string
  productGoal: string
  targetUsers: string
  // 功能与页面
  mvpFeatures: string
  futureFeatures: string
  pages: string
  interactions: string
  // 技术约束
  auth: string
  storage: string
  frontendStack: string
  backendStack: string
  deployment: string
  llmUsage: string
  // 非功能与范围
  security: string
  performance: string
  availability: string
  inScope: string
  outOfScope: string
}

const EMPTY: Values = {
  productName: '',
  platform: PLATFORMS[0]?.value ?? '',
  productGoal: '',
  targetUsers: '',
  mvpFeatures: '',
  futureFeatures: '',
  pages: '',
  interactions: '',
  auth: AUTH_OPTIONS[0]?.value ?? '',
  storage: STORAGE_OPTIONS[0]?.value ?? '',
  frontendStack: '',
  backendStack: '',
  deployment: '',
  llmUsage: '',
  security: '',
  performance: '',
  availability: '',
  inScope: '',
  outOfScope: '',
}

// ---------------------------------------------------------------- 示例预设

/** 一键填充用的示例。`values` 是**部分字段**，缺的那些用 `EMPTY` 补齐。 */
interface Preset {
  id: string
  label: string
  /** 鼠标悬停时显示的说明：这个示例是拿来测什么的 */
  hint: string
  values: Partial<Values>
}

/**
 * 三个示例，各自**测不同的分支**，不是三份随机内容：
 *
 * | 示例 | 用来测什么 |
 * | --- | --- |
 * | 完整（群聊周报助手） | 正常路径：四个区块全填、四列 MVP（含价值与优先级）、页面带模块、范围两张表 |
 * | 极简（无认证 · 无存储 · 无页面） | 缺省路径：条件章省略、可选字段留空、平台选 CLI（非 Web） |
 * | 故意有错 | 校验路径：必填缺失、每行缺列（含"列在但为空"）、范围重复，一次看全三类红字 |
 *
 * 多行内容用**数组 + join**，不写成带真实换行的模板字符串：那样稍微一缩进就会在
 * 每行前面带上空格（虽然解析时会 trim 掉，但预览里看着脏）。
 */
const PRESETS: Preset[] = [
  {
    id: 'full',
    label: '示例 1：完整（群聊周报助手）',
    hint: '四个区块全填，走正常路径：应生成 6 章 PRD，第 2 章带 FR/AC 编号',
    values: {
      productName: '群聊周报助手',
      platform: 'Web 网站',
      productGoal:
        '团队群聊里的进展、决策与阻塞散在聊天记录里，每周要人工翻两小时拼周报，还容易漏掉关键事项。目标是把整理耗时降到 10 分钟以内，并保证不漏项。',
      targetUsers: [
        '20 人以下创业团队的技术负责人，兼做项目管理',
        '团队成员（只读自己被汇总到的条目）',
      ].join('\n'),
      mvpFeatures: [
        '绑定群聊并汇总｜选定群聊与时间范围后触发汇总，返回按人分组的消息集合｜省掉每天翻记录｜P0',
        '生成周报草稿｜把汇总结果按人 / 项目组织成草稿，初始状态 draft｜不用从零开始写｜P0',
        '人工编辑草稿｜在草稿页修改措辞，保存后状态转 editing｜让措辞贴合团队习惯｜P1',
        '导出到飞书｜把草稿导出为飞书文档并回链，导出成功状态转 exported｜周报直接进群｜P0',
      ].join('\n'),
      futureFeatures: ['多群对比', '自动把周报发到群里', '按项目维度组织草稿'].join('\n'),
      pages: [
        '登录页｜表单,错误提示｜/login',
        '汇总进度页｜进度条,失败重试｜/aggregations/:id',
        '周报草稿页｜列表,编辑器,导出条｜/reports/:id',
        '导出历史页｜列表,外链｜/exports',
      ].join('\n'),
      interactions: [
        '点击「开始汇总」且时间范围合法 → 进入进度页并轮询任务状态',
        '在草稿页点保存且校验通过 → 写回草稿并提示已保存',
        '导出时飞书限流（每分钟 5 次）→ 退避重试；重试用尽 → 状态回落到 editing',
      ].join('\n'),
      auth: '需要：账号密码注册登录',
      storage: '需要：要长期保存用户数据',
      frontendStack: 'React 19 + TypeScript + Tailwind',
      backendStack: 'Python 3.12 + FastAPI',
      deployment: '内网服务器，容器化（Docker Compose），不上公有云',
      llmUsage:
        '用 DeepSeek 生成周报草稿；上下文只放选定时间范围内的消息；单次输出上限 8192 token',
      security: '手机号必须加密存储；需要操作审计日志；数据不能出境',
      performance: '汇总 500 条消息 30 秒内返回；单团队 20 人以内，消息量每天数百条',
      availability:
        '工作时段可用性 99%；导出失败要能重试；支持 Chrome / Edge 最近两个大版本，屏幕宽度 1280 起',
      inScope: ['账号注册登录', '绑定群聊并汇总', '生成周报草稿', '人工编辑草稿', '导出到飞书'].join(
        '\n',
      ),
      outOfScope: ['自动把周报发到群里', '多人协同编辑同一份草稿', '除飞书外的其它导出目标'].join(
        '\n',
      ),
    },
  },
  {
    id: 'minimal',
    label: '示例 2：极简（无认证 · 无存储 · 无页面）',
    hint: '测缺省分支：平台选 CLI、无认证、无持久化、页面结构留空、LLM 说明留空',
    values: {
      productName: 'JSON 格式化小工具',
      platform: 'CLI 命令行工具',
      productGoal:
        '排查线上问题时要反复把压缩的 JSON 展开看，网页工具要么联网、要么不敢粘敏感数据。目标是一个纯本地、一条命令就能格式化并校验的工具。',
      targetUsers: '后端工程师（日常排查日志与接口响应）',
      mvpFeatures: [
        '格式化 JSON｜读文件或标准输入，缩进后输出｜不用再打开网页工具',
        '校验并定位错误｜非法 JSON 时给出行号与列号｜省掉肉眼找括号',
        '写回文件｜格式化结果覆盖原文件或写到新文件｜批量处理更省事',
      ].join('\n'),
      futureFeatures: ['支持 JSONL', '语法高亮'].join('\n'),
      // 页面结构**故意留空**：CLI 没有页面，顺便测"可选字段缺失"这条路
      interactions: [
        '输入非法 JSON → 在 stderr 输出错误行号与列号，并以非零退出码结束',
        '结果写入 stdout → 可直接管道给下一个命令',
      ].join('\n'),
      auth: '不需要：打开即用',
      storage: '不需要：纯前端运行即可',
      frontendStack: '',
      backendStack: 'Python 3.12（标准库 json + argparse）',
      deployment: '单文件脚本，随仓库分发，不需要服务器',
      llmUsage: '',
      security: '所有处理必须在本机完成，输入不得上传到任何外部服务',
      performance: '10 MB 以内的 JSON 在 2 秒内完成格式化',
      availability: '不依赖网络；支持 Python 3.10 及以上',
      inScope: ['格式化 JSON', '校验并定位错误', '写回文件'].join('\n'),
      outOfScope: ['JSONL 支持', '语法高亮', '图形界面'].join('\n'),
    },
  },
  {
    id: 'invalid',
    label: '示例 3：故意有错（测校验）',
    hint: '必修：2 项必填留空 + MVP 两行缺列 + 页面一行缺列 + 范围重复，一次看全三类红字',
    values: {
      productName: '校验演示',
      productGoal: '', // 必填留空
      targetUsers: '', // 必填留空
      mvpFeatures: [
        '只有功能名的一行', // 缺第二列
        '功能 B｜｜价值 B', // 第二列在但为空
        '功能 C｜描述 C', // 合法，作为对照
      ].join('\n'),
      pages: [
        '只有页面名', // 缺第二列
        '页面 B｜模块 1,模块 2', // 合法，作为对照
      ].join('\n'),
      inScope: ['导出到飞书', '账号注册登录'].join('\n'),
      outOfScope: ['导出到飞书'].join('\n'), // 与"在范围内"重复
    },
  },
]

// ---------------------------------------------------------------- 解析

/**
 * 多行文本 → 字符串数组。
 *
 * 规则：按 `\n` 分割，逐行去首尾空白，**丢掉空行**（用户在段落之间空一行是排版习惯，
 * 不是"一条空条目"）。CRLF 已被 `\r` 的 trim 顺手处理掉。
 */
function parseList(text: string): string[] {
  return text
    .split('\n')
    .map((line) => line.trim())
    .filter(Boolean)
}

/**
 * 按分隔符拆字段。
 *
 * 中文竖线 `｜` 与半角竖线 `|` 都认 —— 中文输入法下打出的是前者，从别处粘贴过来的
 * 往往是后者，只认一种会让人对着"格式没错但报格式错"发呆。
 * 拆完**保留空列不清除**：`功能名｜` 的第二列要能被判成"存在但为空"，
 * 顺手 filter 掉的话，这种写法会被当成只有一列，报错信息就指不准。
 */
function splitFields(line: string): string[] {
  return line.split(/[｜|]/).map((cell) => cell.trim())
}

/** MVP 功能的一行。`priority` 是第 4 列（可选，见 `parseMvpFeatures` 的说明）。 */
interface MvpFeature {
  name: string
  description: string
  user_value: string
  priority: string
}

/** 页面结构的一行。`modules` 已按逗号拆成数组。 */
interface PageItem {
  name: string
  modules: string[]
  notes: string
}

/**
 * 解析 MVP 功能：每行 `功能名｜描述｜价值｜优先级`（第 3、4 列可省略）。
 *
 * 列的含义按使用方给的格式来：**第 3 列是「价值」不是「优先级」**。
 * 但下游（`FR-xx` 编号、接口文档的 P0 覆盖、套件的覆盖矩阵）都要优先级，
 * 所以再留一个**可选的第 4 列**：写了 `P0`/`P1`/`P2` 就带走，不写就留空
 * （schema 里 `priority` 本来就是可选的，规则是"不给按 P0 处理"）。
 *
 * 缺列时**不抛错**：空的列照样返回空串 —— 校验由 `validate()` 负责报错，
 * 解析器只管尽可能把用户写的东西还原出来（两者职责分开，报错信息才不会互相打架）。
 */
function parseMvpFeatures(text: string): MvpFeature[] {
  const isPriority = (value: string) => /^P[0-2]$/i.test(value)
  return parseList(text).map((line) => {
    const cells = splitFields(line)
    const name = cells[0] ?? ''
    const description = cells[1] ?? ''
    let userValue = cells[2] ?? ''
    let priority = cells[3] ?? ''
    // 兼容"只写三列、第三列写优先级"的写法（旧提示词就是这么教的）。
    // 不认这一条的话，它会被当成"价值：P0"这种没有意义的内容带进 PRD。
    if (!priority && isPriority(userValue)) {
      priority = userValue
      userValue = ''
    }
    return {
      name,
      description,
      user_value: userValue,
      priority: isPriority(priority) ? priority.toUpperCase() : '',
    }
  })
}

/**
 * 解析页面结构：每行 `页面名｜模块1,模块2｜备注`（第 2、3 列可省略）。
 *
 * 第 2 列按逗号拆成数组：中英文逗号与顿号都当分隔符（`列表,编辑器` / `列表，编辑器` /
 * `列表、编辑器` 是同一个意思）。备注是自由文本，原样保留。
 */
function parsePages(text: string): PageItem[] {
  return parseList(text).map((line) => {
    const [name = '', moduleCell = '', notes = ''] = splitFields(line)
    return {
      name,
      modules: parseList(moduleCell.replace(/[，、]/g, ',')).flatMap((cell) =>
        cell
          .split(',')
          .map((item) => item.trim())
          .filter(Boolean),
      ),
      notes,
    }
  })
}

/**
 * 技能包 `field-schema.json` 的 8 字段载荷（**唯一以 schema 为准的输出形状**）。
 *
 * 元素级的字段名必须与 schema 逐字一致，否则那份 JSON 就不合法：
 * `mvp_features` 的元素只有 `name` / `description` / `priority`；
 * `ui_pages` 的元素只有 `name` / `purpose` / `path`（都是 `additionalProperties: false`）。
 */
interface SkillPayload {
  product_name?: string
  product_goal?: string
  target_users?: string | string[]
  platform?: string
  mvp_features?: Array<{ name: string; description?: string; priority?: string }>
  v2_features?: string[]
  ui_pages?: Array<{ name: string; purpose?: string; path?: string }>
  technical_constraints?: Record<string, string>
}

/** `buildRequirementsSummary()` 的返回值：解析结果 + 与 schema 完全匹配的载荷。 */
interface RequirementsSummary {
  productName: string
  platform: string
  productGoal: string
  targetUsers: string[]
  mvpFeatures: MvpFeature[]
  futureFeatures: string[]
  pages: PageItem[]
  interactions: string[]
  auth: string
  storage: string
  frontendStack: string
  backendStack: string
  deployment: string
  llmUsage: string
  security: string
  performance: string
  availability: string
  inScope: string[]
  outOfScope: string[]
  /** 与后端 `field-schema.json` 完全匹配的那一份（空字段不输出 → 始终合法） */
  schemaPayload: SkillPayload
}

/**
 * 主解析函数：把整个表单一次性解析成结构化摘要，并顺手产出 schema 载荷。
 *
 * **只在这里解析一次**：`buildRequest()`（20 题映射）与 JSON 预览都吃它的结果。
 * 之前 `buildSkillPayload()` 与 `buildRequest()` 各自 `split('\n')` 一遍，
 * 同样的规则写两处，改一处必漏一处。
 *
 * 解析结果里保留表单原本的列名（`user_value` / `modules` / `notes`），
 * 而 `schemaPayload` 把它们**收口到 schema 的字段名**上：
 *
 * | 解析结果 | schema 载荷 | 为什么 |
 * | --- | --- | --- |
 * | `user_value` | 并入 `description`（`描述；价值：xxx`） | schema 里没有"价值"这个字段，而"不能丢信息"优先 |
 * | `modules` + `notes` | 并入 `ui_pages[].purpose` | 同上：元素只允许 `name` / `purpose` / `path` |
 * | `priority` | `priority`（原样） | schema 里本来就有 |
 */
function buildRequirementsSummary(values: Values): RequirementsSummary {
  const mvpFeatures = parseMvpFeatures(values.mvpFeatures)
  const pages = parsePages(values.pages)
  const targetUsers = parseList(values.targetUsers)

  const stack = [
    values.frontendStack.trim() && `前端：${values.frontendStack.trim()}`,
    values.backendStack.trim() && `后端：${values.backendStack.trim()}`,
  ]
    .filter(Boolean)
    .join('；')

  const technicalConstraints: Record<string, string> = {}
  const putConstraint = (key: string, value: string) => {
    if (value.trim()) technicalConstraints[key] = value.trim()
  }
  // `technical_constraints` 是 schema 里唯一的开放对象，这几个键正是"表单里有、
  // schema 顶层却没有"的字段的去处（auth / storage / llm / availability）
  putConstraint('stack', stack)
  putConstraint('auth', values.auth)
  putConstraint('storage', values.storage)
  putConstraint('deployment', values.deployment)
  putConstraint('llm', values.llmUsage)
  putConstraint('performance', values.performance)
  putConstraint('compliance', values.security)
  putConstraint('availability', values.availability)

  const schemaPayload: SkillPayload = {}
  // 空字段一律不输出：这样这份 JSON 始终满足 schema（连必填项的 minLength: 1 都不违反），
  // 可以整段复制去跑 JSON Schema 校验
  const put = <K extends keyof SkillPayload>(key: K, value: SkillPayload[K]) => {
    if (value === undefined) return
    if (typeof value === 'string' && !value.trim()) return
    if (Array.isArray(value) && value.length === 0) return
    schemaPayload[key] = value
  }

  put('product_name', values.productName.trim())
  put('product_goal', values.productGoal.trim())
  // schema：单个角色写字符串，多个角色写字符串数组
  put('target_users', targetUsers.length === 1 ? targetUsers[0] : targetUsers)
  put('platform', values.platform.trim())
  put(
    'mvp_features',
    mvpFeatures
      .filter((feature) => feature.name)
      .map((feature) => {
        const description = [feature.description, feature.user_value && `价值：${feature.user_value}`]
          .filter(Boolean)
          .join('；')
        return {
          name: feature.name,
          ...(description ? { description } : {}),
          ...(feature.priority ? { priority: feature.priority } : {}),
        }
      }),
  )
  put('v2_features', parseList(values.futureFeatures))
  put(
    'ui_pages',
    pages
      .filter((page) => page.name)
      .map((page) => {
        const purpose = [
          page.modules.length > 0 && `模块：${page.modules.join('、')}`,
          page.notes && `备注：${page.notes}`,
        ]
          .filter(Boolean)
          .join('；')
        return { name: page.name, ...(purpose ? { purpose } : {}) }
      }),
  )
  put(
    'technical_constraints',
    Object.keys(technicalConstraints).length > 0 ? technicalConstraints : undefined,
  )

  return {
    productName: values.productName.trim(),
    platform: values.platform.trim(),
    productGoal: values.productGoal.trim(),
    targetUsers,
    mvpFeatures,
    futureFeatures: parseList(values.futureFeatures),
    pages,
    interactions: parseList(values.interactions),
    auth: values.auth.trim(),
    storage: values.storage.trim(),
    frontendStack: values.frontendStack.trim(),
    backendStack: values.backendStack.trim(),
    deployment: values.deployment.trim(),
    llmUsage: values.llmUsage.trim(),
    security: values.security.trim(),
    performance: values.performance.trim(),
    availability: values.availability.trim(),
    inScope: parseList(values.inScope),
    outOfScope: parseList(values.outOfScope),
    schemaPayload,
  }
}

/**
 * 把一个多行文本块渲染成带标题的段落，供 `known_info` 用。
 *
 * 没有内容时返回空串（不输出空标题）—— 空标题会让模型以为"这一项确认过、就是没内容"，
 * 而实际是用户根本没填。
 */
function block(title: string, text: string): string {
  const body = text.trim()
  return body ? `${title}：\n${body}` : ''
}

// ---------------------------------------------------------------- 映射

interface BuiltRequest {
  form: Record<string, string>
  known_info: string
}

/**
 * 把**解析后的摘要**转成后端当前要的形状（20 题作答 + `known_info`）。**映射规则只在这里**。
 *
 * 输入是 `buildRequirementsSummary()` 的结果，不再自己解析一遍 —— 解析规则只有一处。
 *
 * 三处刻意的取舍：
 * 1. `known_info` 装"20 题里没有对应题目"的信息（未来规划、页面结构明细、关键交互、LLM 说明、
 *    可用性、在/不在范围内）。它是自由文本且**优先级高于表单**，正好承接这些内容。
 * 2. 页面数量是按解析出的页面数**算出来**的（`page_count` 是下拉题，不能塞自由文本）。
 * 3. `core_features` 用解析后的字段**重新排版**（`- 功能名：描述（价值：…）`），
 *    而不是把原始文本块原样塞进去 —— 这样"每行两列"的约定在提示词里是显式的，
 *    即使有人写得歪歪扭扭，模型看到的也是规整的一行。
 */
function buildRequest(summary: RequirementsSummary): BuiltRequest {
  const stack = [
    summary.frontendStack && `前端：${summary.frontendStack}`,
    summary.backendStack && `后端：${summary.backendStack}`,
  ]
    .filter(Boolean)
    .join('；')

  const coreFeatures = summary.mvpFeatures
    .filter((feature) => feature.name)
    .map((feature) => {
      const extras = [
        feature.user_value && `价值：${feature.user_value}`,
        feature.priority && `优先级：${feature.priority}`,
      ].filter(Boolean)
      const head = feature.description ? `${feature.name}：${feature.description}` : feature.name
      return `- ${extras.length > 0 ? `${head}（${extras.join('；')}）` : head}`
    })
    .join('\n')

  const form: Record<string, string> = {
    product_name: summary.productName,
    platform: summary.platform,
    problem: summary.productGoal,
    target_users: summary.targetUsers.join('、'),
    core_features: coreFeatures,
    need_auth: summary.auth,
    need_database: summary.storage,
    tech_stack: stack,
    hard_constraints: summary.deployment,
    compliance: summary.security,
    scale_expectation: summary.performance,
  }
  const pageCount = pageCountValue(summary.pages.length)
  if (pageCount) form.page_count = pageCount

  // 空值不放进 form：留空与"填了但内容为空"在后端是两种含义（后者会让该题不显示「未填写」）
  for (const key of Object.keys(form)) {
    if (!form[key]) delete form[key]
  }

  // 页面结构用解析后的形状重新排版：模块是数组，逐页列清楚，比原始文本好读
  const pagesText = summary.pages
    .filter((page) => page.name)
    .map((page) => {
      const parts = [page.name]
      if (page.modules.length > 0) parts.push(`模块：${page.modules.join('、')}`)
      if (page.notes) parts.push(`备注：${page.notes}`)
      return `- ${parts.join('｜')}`
    })
    .join('\n')

  const knownInfo = [
    block('本期在范围内（结构化录入）', summary.inScope.map((item) => `- ${item}`).join('\n')),
    block('本期明确不做（结构化录入）', summary.outOfScope.map((item) => `- ${item}`).join('\n')),
    block(
      '未来规划（不在本期范围，结构化录入）',
      summary.futureFeatures.map((item) => `- ${item}`).join('\n'),
    ),
    block('页面结构与模块（结构化录入）', pagesText),
    block('关键交互（结构化录入）', summary.interactions.map((item) => `- ${item}`).join('\n')),
    block('LLM 使用说明（结构化录入）', summary.llmUsage),
    block('可用性要求（结构化录入）', summary.availability),
  ]
    .filter(Boolean)
    .join('\n\n')

  return { form, known_info: knownInfo }
}

/**
 * 表单校验。三类规则，结果按字段分组。
 *
 * | 类别 | 规则 |
 * | --- | --- |
 * | 必填 | 产品名称 / 核心目标 / 目标用户 / 平台 / MVP 功能 不能为空 |
 * | 格式 | MVP 功能每行必须有两列（功能名｜描述）；页面结构每行必须有两列（页面名｜模块列表） |
 * | 范围 | 「在范围内」与「不在范围内」不能出现相同条目 |
 *
 * 返回 `byField`（键 = `Values` 的字段名，值 = 该字段的人话错误）与 `summary`（拍平后给总提示框）。
 * **按字段分组**而不是一个平铺数组，是为了能同时做两件事：字段下方标红 + 顶部汇总。
 *
 * 行号按**去掉空行之后**的序号算，与用户看到的"第 N 条"一致
 * （`lines()` 会丢空行，直接用原始行号会错位）。
 */
interface ValidationResult {
  byField: Partial<Record<keyof Values, string[]>>
  summary: string[]
  /** 出错的字段数，用来决定要不要展开全部面板 */
  fieldCount: number
}

const REQUIRED_FIELDS: Array<{ key: keyof Values; label: string }> = [
  { key: 'productName', label: '产品名称' },
  { key: 'productGoal', label: '核心目标' },
  { key: 'targetUsers', label: '目标用户' },
  { key: 'platform', label: '平台' },
  { key: 'mvpFeatures', label: 'MVP 功能' },
]

function validate(values: Values): ValidationResult {
  const byField: Partial<Record<keyof Values, string[]>> = {}
  const add = (key: keyof Values, message: string) => {
    const list = byField[key] ?? []
    // 去重：同一条问题可能被同一行触发两次（例如一行里第一、二列都为空），
    // 重复的文案在界面上是噪音，而且会让 React 的列表 key 撞车
    if (!list.includes(message)) list.push(message)
    byField[key] = list
  }

  // ---------- 1. 必填 ----------
  for (const { key, label } of REQUIRED_FIELDS) {
    if (!values[key].trim()) add(key, `${label}不能为空`)
  }

  // ---------- 2. 格式：每行必须有的列 ----------
  /**
   * 逐行检查"至少两列且都不为空"。
   *
   * `splitFields()` 对空列不做过滤，所以 `「绑定群聊｜」` 会得到 `['绑定群聊', '']` ——
   * 第二列存在但为空，必须单独判；只看列数会把这种写法放过去。
   */
  const checkColumns = (key: keyof Values, first: string, second: string) => {
    parseList(values[key]).forEach((line, index) => {
      const columns = splitFields(line)
      const position = `第 ${index + 1} 行`
      if (columns.length < 2) {
        add(key, `${position}：只写到「${columns[0] || '空'}」，需要「${first}｜${second}」两列`)
        return
      }
      if (!columns[0]) add(key, `${position}：${first}为空`)
      if (!columns[1]) add(key, `${position}：${second}为空`)
    })
  }
  checkColumns('mvpFeatures', '功能名', '描述')
  checkColumns('pages', '页面名', '模块列表')

  // ---------- 3. 范围：两张表不能有同一条 ----------
  // 归一化只去首尾空白与结尾标点，不做模糊匹配 —— 中文条目上，模糊匹配的误报
  // 比漏报更烦人（"导出到飞书"与"导出到飞书（v2）"是两条不同的东西）。
  const normalize = (text: string) => text.trim().replace(/[。；;，,]+$/, '')
  const outOfScope = new Map<string, string>()
  for (const item of parseList(values.outOfScope)) {
    outOfScope.set(normalize(item), item)
  }
  const duplicated: string[] = []
  for (const item of parseList(values.inScope)) {
    const hit = outOfScope.get(normalize(item))
    if (hit) duplicated.push(hit)
  }
  for (const item of duplicated) {
    // 两边都标红：错在哪一边是用户的判断，界面不该替他选
    add('inScope', `「${item}」同时出现在「不在范围内」`)
    add('outOfScope', `「${item}」同时出现在「在范围内」`)
  }

  const summary: string[] = []
  const labelOf = (key: keyof Values) =>
    REQUIRED_FIELDS.find((field) => field.key === key)?.label ?? FIELD_LABELS[key]
  for (const key of Object.keys(byField) as Array<keyof Values>) {
    for (const message of byField[key] ?? []) {
      summary.push(`${labelOf(key)}：${message}`)
    }
  }

  return { byField, summary, fieldCount: Object.keys(byField).length }
}

/** 字段名 → 中文标签（错误汇总用；`REQUIRED_FIELDS` 里已有一部分）。 */
const FIELD_LABELS: Record<keyof Values, string> = {
  productName: '产品名称',
  platform: '平台',
  productGoal: '核心目标',
  targetUsers: '目标用户',
  mvpFeatures: 'MVP 功能',
  futureFeatures: '未来规划',
  pages: '页面结构',
  interactions: '关键交互',
  auth: '用户认证',
  storage: '数据存储',
  frontendStack: '前端技术',
  backendStack: '后端技术',
  deployment: '部署方式',
  llmUsage: 'LLM 使用说明',
  security: '安全要求',
  performance: '性能要求',
  availability: '可用性要求',
  inScope: '在范围内',
  outOfScope: '不在范围内',
}

// ---------------------------------------------------------------- 小组件

const inputClass =
  'w-full rounded-lg border border-slate-300 px-3 py-2 text-sm text-slate-800 placeholder:text-slate-400 focus:border-primary-400 focus:outline-none focus:ring-2 focus:ring-primary-100'

/** 出错时的边框：红框 + 红色聚焦环，与下面的红色文字一起构成"这个字段有问题"的信号。 */
const inputErrorClass =
  'w-full rounded-lg border border-rose-400 bg-rose-50/40 px-3 py-2 text-sm text-slate-800 placeholder:text-slate-400 focus:border-rose-500 focus:outline-none focus:ring-2 focus:ring-rose-100'

/** 红色错误文案列表。字段下方的就地提示用它，`role="alert"` 让读屏软件也能听到。 */
function FieldErrors({ errors }: { errors?: string[] }) {
  if (!errors || errors.length === 0) return null
  return (
    <ul className="mt-1 space-y-0.5" role="alert">
      {errors.map((message) => (
        <li key={message} className="text-xs text-rose-600">
          {message}
        </li>
      ))}
    </ul>
  )
}

interface FieldProps {
  label: string
  hint?: string
  required?: boolean
  errors?: string[]
  children: React.ReactNode
}

function Field({ label, hint, required, errors, children }: FieldProps) {
  const hasError = Boolean(errors && errors.length > 0)
  return (
    <label className="block">
      <span className="mb-1 flex items-baseline gap-1.5 text-sm font-medium text-slate-700">
        {label}
        {required && <span className={hasError ? 'text-rose-600' : 'text-rose-500'}>必填</span>}
        {hint && <span className="text-xs font-normal text-slate-400">{hint}</span>}
      </span>
      {children}
      <FieldErrors errors={errors} />
    </label>
  )
}

interface TextFieldProps {
  label: string
  value: string
  onChange: (value: string) => void
  placeholder?: string
  hint?: string
  required?: boolean
  errors?: string[]
}

function TextField({
  label,
  value,
  onChange,
  placeholder,
  hint,
  required,
  errors,
}: TextFieldProps) {
  const hasError = Boolean(errors && errors.length > 0)
  return (
    <Field label={label} hint={hint} required={required} errors={errors}>
      <input
        type="text"
        value={value}
        placeholder={placeholder}
        onChange={(event) => onChange(event.target.value)}
        aria-invalid={hasError}
        className={hasError ? inputErrorClass : inputClass}
      />
    </Field>
  )
}

interface TextAreaProps extends TextFieldProps {
  rows?: number
}

function TextArea({
  label,
  value,
  onChange,
  placeholder,
  hint,
  required,
  errors,
  rows = 4,
}: TextAreaProps) {
  const hasError = Boolean(errors && errors.length > 0)
  return (
    <Field label={label} hint={hint} required={required} errors={errors}>
      <textarea
        value={value}
        rows={rows}
        placeholder={placeholder}
        onChange={(event) => onChange(event.target.value)}
        aria-invalid={hasError}
        className={`${hasError ? inputErrorClass : inputClass} resize-y font-mono text-xs leading-relaxed`}
      />
    </Field>
  )
}

interface SelectFieldProps {
  label: string
  value: string
  options: Option[]
  onChange: (value: string) => void
  hint?: string
  errors?: string[]
}

function SelectField({ label, value, options, onChange, hint, errors }: SelectFieldProps) {
  const current = options.find((option) => option.value === value)
  const hasError = Boolean(errors && errors.length > 0)
  return (
    <Field label={label} hint={hint} errors={errors}>
      <select
        value={value}
        onChange={(event) => onChange(event.target.value)}
        aria-invalid={hasError}
        className={hasError ? inputErrorClass : inputClass}
      >
        {options.map((option) => (
          <option key={option.value} value={option.value}>
            {option.label}
          </option>
        ))}
      </select>
      {/* 显示"实际发出去的值"：下拉框显示的是人话，发给后端的是提示词里做字面比较的原话 */}
      {current && current.label !== current.value && (
        <span className="mt-1 block text-xs text-slate-400">发送值：{current.value}</span>
      )}
    </Field>
  )
}

interface SectionProps {
  index: number
  title: string
  summary: string
  open: boolean
  onToggle: () => void
  children: React.ReactNode
}

function Section({ index, title, summary, open, onToggle, children }: SectionProps) {
  return (
    <section className="overflow-hidden rounded-xl border border-slate-200 bg-white shadow-sm">
      <button
        type="button"
        onClick={onToggle}
        aria-expanded={open}
        className="flex w-full items-center gap-2 px-4 py-3 text-left transition hover:bg-slate-50"
      >
        {open ? (
          <ChevronDown className="h-4 w-4 shrink-0 text-slate-400" aria-hidden />
        ) : (
          <ChevronRight className="h-4 w-4 shrink-0 text-slate-400" aria-hidden />
        )}
        <span className="text-sm font-semibold text-slate-800">
          {index}. {title}
        </span>
        <span className="ml-auto text-xs text-slate-400">{summary}</span>
      </button>
      {open && <div className="space-y-4 border-t border-slate-100 px-4 py-4">{children}</div>}
    </section>
  )
}


// ---------------------------------------------------------------- 主组件

/** 提交给上层的东西。`extras` 由上层并进 `known_info`（它优先级高于表单作答）。 */
export interface StructuredSubmit {
  form: FormValues
  extras: string
}

interface StructuredFormProps {
  /** 校验通过后才会被调用。上层负责推进到对话步。 */
  onSubmit: (payload: StructuredSubmit) => void
  /** 上层正在处理（比如开场请求在飞）—— 期间禁用提交与示例按钮。 */
  submitting?: boolean
}

export default function StructuredForm({ onSubmit, submitting = false }: StructuredFormProps) {
  const [values, setValues] = useState<Values>(EMPTY)
  const [restoredDraft, setRestoredDraft] = useState(false)
  const [openSections, setOpenSections] = useState<Record<string, boolean>>({
    basics: true,
    features: false,
    tech: false,
    quality: false,
  })
  /**
   * 是否已经点过一次提交。校验**一直**在算，但只在点过之后才显示 ——
   * 一进页面就满屏红字会把"还没开始填"误报成"填错了"；点过之后改为实时更新。
   */
  const [submitted, setSubmitted] = useState(false)
  /** JSON 预览面板是否展开。默认关着 —— 它是排查用的，不该挡在正常流程前面。 */
  const [showJson, setShowJson] = useState(false)
  const [copied, setCopied] = useState(false)

  // 挂载时读一次草稿。放在 effect 而不是 `useState` 初始化器里：
  // 初始化器在严格模式下会被调用两次，"读存储"这类副作用不该放在那儿。
  useEffect(() => {
    const saved = readLocal<Partial<Values>>(DRAFT_KIND)
    if (saved && Object.keys(saved).length > 0) {
      setValues({ ...EMPTY, ...saved })
      setRestoredDraft(true)
    }
  }, [])

  // 防抖写回草稿。整份都空时**清掉**这个键，而不是写一个空对象进去
  // （写空对象会让"上次填过"的痕迹永远留在本机）。
  useEffect(() => {
    const timer = window.setTimeout(() => {
      const filled = Object.values(values).some((value) => value.trim())
      if (filled) writeLocal(DRAFT_KIND, values)
      else clearLocal(DRAFT_KIND)
    }, DRAFT_DEBOUNCE_MS)
    return () => window.clearTimeout(timer)
  }, [values])

  /**
   * 表单解析结果。`useMemo` 挂在 `values` 上：表单每变一次重算一次，纯计算无副作用。
   * 校验、预览、提交三处都吃这一份 —— 解析规则只有 `buildRequirementsSummary()` 一处。
   */
  const summary = useMemo(() => buildRequirementsSummary(values), [values])

  /**
   * 实时预览用的三份 JSON。
   *
   * - `parsed`：解析结果（列出表单原本的列名：`user_value` / `modules` / `notes`）
   * - `skillPayload`：与 `field-schema.json` **完全匹配**的那一份（空字段不输出 → 始终合法）
   * - `requestBody`：真正作为 `form` 交出去的东西（20 题作答 + `known_info`）
   */
  const preview = useMemo(() => {
    const request = buildRequest(summary)
    const { schemaPayload, ...parsed } = summary
    return {
      parsed,
      skillPayload: schemaPayload,
      requestBody: {
        form: request.form,
        ...(request.known_info ? { known_info: request.known_info } : {}),
      },
    }
  }, [summary])

  /** 格式化后的整段 JSON：2 空格缩进，把三块并在一起，复制一次就够。 */
  const previewText = useMemo(() => JSON.stringify(preview, null, 2), [preview])

  const previewFields = useMemo(() => Object.keys(preview.skillPayload).length, [preview])

  const validation = useMemo(() => validate(values), [values])
  const showErrors = submitted && validation.fieldCount > 0

  /** 取某个字段的错误：没提交过就返回空，字段组件据此决定要不要标红。 */
  const errorsFor = useCallback(
    (key: keyof Values): string[] | undefined =>
      showErrors ? validation.byField[key] : undefined,
    [showErrors, validation],
  )

  const handleCopy = useCallback(async () => {
    try {
      await navigator.clipboard.writeText(previewText)
      setCopied(true)
      window.setTimeout(() => setCopied(false), 1500)
    } catch {
      // 剪贴板不可用（非 https、权限被拒）时不弹提示：面板里本来就能手动选中复制
      setCopied(false)
    }
  }, [previewText])

  const set = useCallback(
    <K extends keyof Values>(key: K) => (value: Values[K]) =>
      setValues((prev) => ({ ...prev, [key]: value })),
    [],
  )

  const toggle = useCallback((key: string) => {
    setOpenSections((prev) => ({ ...prev, [key]: !prev[key] }))
  }, [])

  /**
   * 一键填示例。
   *
   * 三个刻意的选择：
   * 1. **整体替换**（`{...EMPTY, ...preset.values}`）而不是合并：示例要能测"某字段留空"的分支，
   *    合并的话上一次填的内容会残留，缺省分支就永远测不到。
   * 2. **四个面板全展开**：填完立刻能看到填了什么，省一次点击。
   * 3. **不动草稿键**：随后的防抖 effect 会把新内容写回去，不需要在这里手动写。
   */
  const applyPreset = useCallback((preset: Preset) => {
    setValues({ ...EMPTY, ...preset.values })
    setOpenSections({ basics: true, features: true, tech: true, quality: true })
  }, [])

  const handleSubmitClick = useCallback(() => {
    setSubmitted(true)
    if (validation.fieldCount > 0) {
      // 校验不过时把四个区块全部展开：报错说"目标用户不能为空"，但那一块是收着的，
      // 用户会以为界面坏了
      setOpenSections({ basics: true, features: true, tech: true, quality: true })
      return
    }
    const request = buildRequest(summary)
    // 草稿**不清**：用户从对话步退回来时，填过的东西还在
    onSubmit({ form: request.form, extras: request.known_info })
  }, [validation, summary, onSubmit])

  /** 面板标题右侧的"已填几条"。名字叫 countLabel 而不是 summary —— 后者已被解析结果占用。 */
  const countLabel = (text: string, unit = '行') => {
    const count = parseList(text).length
    return count > 0 ? `${count} ${unit}` : '未填写'
  }

  return (
    <div className="flex flex-col gap-5">
      {restoredDraft && (
        <p className="rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-800">
          已恢复上次未提交的填写内容（存在本机浏览器里）。
        </p>
      )}

      {/* 测试用的示例。放表单上面：填一份长表单只为试一次生成太费事。
          三个示例分别测正常路径 / 缺省路径 / 校验路径，理由写在 `PRESETS` 的注释里。 */}
      <div className="flex flex-wrap items-center gap-2 rounded-xl border border-slate-200 bg-white px-4 py-3 shadow-sm">
        <span className="text-xs font-medium text-slate-500">快速填充示例</span>
        {PRESETS.map((preset) => (
          <button
            key={preset.id}
            type="button"
            data-testid={`preset-${preset.id}`}
            title={preset.hint}
            disabled={submitting}
            onClick={() => applyPreset(preset)}
            className="rounded-lg border border-slate-300 px-3 py-1.5 text-xs text-slate-600 transition hover:border-primary-300 hover:bg-primary-50 hover:text-primary-700 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {preset.label}
          </button>
        ))}
        <span className="text-xs text-slate-400">点一下即填满（会覆盖当前填写内容）</span>
      </div>

      <Section
        index={1}
        title="产品与用户"
        summary={countLabel(values.targetUsers, '个角色')}
        open={openSections.basics}
        onToggle={() => toggle('basics')}
      >
        <div className="grid gap-4 md:grid-cols-2">
          <TextField
            label="产品名称"
            required
            value={values.productName}
            onChange={set('productName')}
            placeholder="例如：群聊周报助手"
            errors={errorsFor('productName')}
          />
          <SelectField
            label="平台"
            value={values.platform}
            options={PLATFORMS}
            onChange={set('platform')}
            hint="决定非功能需求里的端与兼容性"
            errors={errorsFor('platform')}
          />
        </div>
        <TextArea
          label="核心目标"
          required
          rows={3}
          value={values.productGoal}
          onChange={set('productGoal')}
          placeholder="要解决的问题 + 期望达到的效果。例如：团队周报靠人工翻聊天记录拼，一次约两小时；目标是降到 10 分钟以内"
          errors={errorsFor('productGoal')}
        />
        <TextArea
          label="目标用户"
          required
          rows={3}
          value={values.targetUsers}
          onChange={set('targetUsers')}
          placeholder="每行一个角色。例如：&#10;20 人以下创业团队的技术负责人，兼做项目管理&#10;团队成员（只读自己的条目）"
          errors={errorsFor('targetUsers')}
        />
      </Section>

      <Section
        index={2}
        title="功能与页面"
        summary={countLabel(values.mvpFeatures, '条功能')}
        open={openSections.features}
        onToggle={() => toggle('features')}
      >
        <TextArea
          label="MVP 功能"
          required
          rows={6}
          value={values.mvpFeatures}
          onChange={set('mvpFeatures')}
          placeholder="每行一条，用竖线分隔：功能名｜描述｜价值｜优先级(P0/P1/P2)。例：&#10;绑定群聊并汇总｜选定群聊与时间范围后触发汇总，返回按人分组的草稿｜省掉每天翻记录｜P0&#10;导出到飞书｜把草稿导出为飞书文档并回链｜周报直接进群"
          hint="前两列必填：功能名｜描述；后两列可省略"
          errors={errorsFor('mvpFeatures')}
        />
        <TextArea
          label="未来规划"
          rows={3}
          value={values.futureFeatures}
          onChange={set('futureFeatures')}
          placeholder="每行一个，本期不做。例如：&#10;多群对比&#10;自动把周报发到群里"
          hint="会进 PRD 的「不在范围内」"
          errors={errorsFor('futureFeatures')}
        />
        <TextArea
          label="页面结构"
          rows={4}
          value={values.pages}
          onChange={set('pages')}
          placeholder="每行一个页面：页面名｜模块1,模块2｜备注。例如：&#10;周报草稿页｜列表,编辑器,导出条｜/reports/:id&#10;登录页｜表单,错误提示｜/login"
          hint="每行必须有两列：页面名｜模块列表"
          errors={errorsFor('pages')}
        />
        <TextArea
          label="关键交互"
          rows={4}
          value={values.interactions}
          onChange={set('interactions')}
          placeholder="每行一条：触发条件 → 结果。例如：&#10;在编辑页点保存且校验通过 → 写回草稿并提示已保存&#10;导出时飞书限流（每分钟 5 次）→ 退避重试"
        />
      </Section>

      <Section
        index={3}
        title="技术约束"
        summary={
          [values.frontendStack.trim() && '前端', values.backendStack.trim() && '后端']
            .filter(Boolean)
            .join('+') || '未填写'
        }
        open={openSections.tech}
        onToggle={() => toggle('tech')}
      >
        <div className="grid gap-4 md:grid-cols-2">
          <SelectField
            label="用户认证"
            value={values.auth}
            options={AUTH_OPTIONS}
            onChange={set('auth')}
            hint="决定要不要写鉴权章"
          />
          <SelectField
            label="数据存储"
            value={values.storage}
            options={STORAGE_OPTIONS}
            onChange={set('storage')}
            hint="决定要不要写数据模型章"
          />
          <TextField
            label="前端技术"
            value={values.frontendStack}
            onChange={set('frontendStack')}
            placeholder="例如：React 19 + TypeScript + Tailwind"
          />
          <TextField
            label="后端技术"
            value={values.backendStack}
            onChange={set('backendStack')}
            placeholder="例如：Python 3.12 + FastAPI"
          />
        </div>
        <TextField
          label="部署方式"
          value={values.deployment}
          onChange={set('deployment')}
          placeholder="例如：内网服务器，Docker Compose，不上公有云"
        />
        <TextArea
          label="LLM 使用说明"
          rows={3}
          value={values.llmUsage}
          onChange={set('llmUsage')}
          placeholder="例如：用 DeepSeek 生成周报草稿；上下文只放最近 7 天消息；输出限 8192 token"
        />
      </Section>

      <Section
        index={4}
        title="非功能与范围"
        summary={countLabel(values.inScope, '项在范围内')}
        open={openSections.quality}
        onToggle={() => toggle('quality')}
      >
        <TextArea
          label="安全要求"
          rows={3}
          value={values.security}
          onChange={set('security')}
          placeholder="例如：手机号加密存储；需要操作审计日志；数据不能出境（没要求就留空，PRD 会标待确认）"
        />
        <TextArea
          label="性能要求"
          rows={3}
          value={values.performance}
          onChange={set('performance')}
          placeholder="例如：汇总 500 条消息 30 秒内返回；单团队 20 人以内，消息量每天数百条"
        />
        <TextArea
          label="可用性要求"
          rows={3}
          value={values.availability}
          onChange={set('availability')}
          placeholder="例如：工作时段可用性 99%；导出失败要能重试；浏览器支持 Chrome / Edge 最近两个大版本"
        />
        <TextArea
          label="在范围内"
          rows={3}
          value={values.inScope}
          onChange={set('inScope')}
          placeholder="每行一项，本期要做的。例如：&#10;账号注册登录&#10;绑定群聊并汇总&#10;导出到飞书"
          hint="与「不在范围内」不能重复"
          errors={errorsFor('inScope')}
        />
        <TextArea
          label="不在范围内"
          rows={3}
          value={values.outOfScope}
          onChange={set('outOfScope')}
          placeholder="每行一项，本期明确不做。例如：&#10;自动发到群里&#10;多人协同编辑同一份草稿"
          hint="与「在范围内」不能重复"
          errors={errorsFor('outOfScope')}
        />
      </Section>

      {/*
        顶部汇总：字段下方的红字已经写了人话，但**面板可能是收着的** ——
        不汇总的话用户只知道"有错"，不知道错在哪一块。所以两处都要有。
      */}
      {showErrors && (
        <div
          data-testid="form-errors"
          role="alert"
          className="rounded-xl border border-rose-300 bg-rose-50 px-4 py-3 text-sm text-rose-700"
        >
          <p className="font-medium">
            校验没通过（{validation.fieldCount} 个字段、{validation.summary.length} 条问题）：
          </p>
          <ul className="mt-1 list-disc space-y-0.5 pl-5">
            {validation.summary.map((message) => (
              <li key={message}>{message}</li>
            ))}
          </ul>
        </div>
      )}

      <div className="flex flex-wrap items-center gap-3">
        <button
          type="button"
          data-testid="submit-form"
          onClick={handleSubmitClick}
          disabled={submitting}
          className="inline-flex items-center gap-2 rounded-lg bg-primary-600 px-4 py-2 text-sm font-medium text-white transition hover:bg-primary-700 disabled:cursor-not-allowed disabled:opacity-60"
        >
          {submitting ? (
            <Loader2 className="h-4 w-4 animate-spin" aria-hidden />
          ) : (
            <FileText className="h-4 w-4" aria-hidden />
          )}
          {submitting ? '正在开始澄清…' : '提交并开始澄清对话'}
        </button>
        <span className="text-xs text-slate-400">
          下一步：AI 就缺口追问几轮（澄清阶段），通过后才生成 PRD
        </span>
        <button
          type="button"
          onClick={() => setShowJson((open) => !open)}
          aria-expanded={showJson}
          data-testid="toggle-json"
          className="ml-auto inline-flex items-center gap-1.5 rounded-lg border border-slate-300 px-3 py-2 text-sm text-slate-600 transition hover:bg-slate-50"
        >
          <Code className="h-3.5 w-3.5" aria-hidden />
          {showJson ? '关闭 JSON 预览' : '查看 JSON 预览'}
        </button>
      </div>

      {showJson && (
        <section
          data-testid="json-preview"
          className="overflow-hidden rounded-xl border border-slate-200 bg-white shadow-sm"
        >
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1 border-b border-slate-100 px-4 py-3">
            <span className="text-sm font-semibold text-slate-800">JSON 预览</span>
            <span className="text-xs text-slate-400">
              随表单实时更新｜parsed = 解析结果，skillPayload 与 schema 对齐（{previewFields} 项），
              requestBody = 实际发出的请求
            </span>
            <button
              type="button"
              onClick={() => void handleCopy()}
              className="ml-auto inline-flex items-center gap-1.5 rounded-lg border border-slate-300 px-3 py-1.5 text-xs text-slate-600 transition hover:bg-slate-50"
            >
              {copied ? (
                <Check className="h-3.5 w-3.5" aria-hidden />
              ) : (
                <Copy className="h-3.5 w-3.5" aria-hidden />
              )}
              {copied ? '已复制' : '复制'}
            </button>
          </div>
          {/* 三块：parsed 是解析结果（保留表单的列名），skillPayload 是"与 schema 完全匹配"
              的那一份（可直接跑 schema 校验），requestBody 是实际发出去的东西。
              三者摆在一起，映射在哪一步"收口"一目了然。 */}
          <pre className="max-h-[60vh] overflow-auto bg-slate-50 px-4 py-3 font-mono text-xs leading-relaxed text-slate-700">
            {previewText}
          </pre>
        </section>
      )}
    </div>
  )
}
