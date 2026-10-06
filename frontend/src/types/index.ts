/**
 * 后端接口的前端类型镜像。
 *
 * ⚠️ 这些类型是**手写镜像**，不是自动生成的 —— 后端改字段它们不会自动跟着变。
 * 字段以实际响应为准，权威来源：
 * - `backend/core/questions.py`（题目配置的形状）
 * - `backend/api/schemas.py`（会话 / 事件等契约）
 *
 * 只声明**前端用得到的字段**：接口返回的额外字段（如题目的 `covers_dimensions`）
 * 不在这里列，运行时依然存在，只是前端不依赖它。
 */

/** 题目类型。与 `backend/core/questions.py` 的 `QuestionType` 保持一致。 */
export type QuestionType = 'text' | 'textarea' | 'select' | 'radio'

/**
 * 一道表单题。
 *
 * ⚠️ 主键是 **`id`** 而不是 `name`。
 * 后端 `core/questions_config.json` 里就叫 `id`，接口原样返回；
 * 写成 `name` 会拿到 `undefined`，且 **TypeScript 不会报错**
 * （编译期类型不校验运行时数据），表单会静默渲染不出来。
 * 提交时 `form` 的键用的也是这个 `id`。
 */
export interface QuestionConfig {
  /** 题目 id，如 `product_name` */
  id: string
  /** 短标签，如「产品名称」 */
  label: string
  /** 题目正文，如「你的产品叫什么名字？」 */
  question: string
  /** 帮助文案 */
  description: string
  type: QuestionType
  /** 选项。`text` / `textarea` 固定为空数组；`select` / `radio` 必有值 */
  options: string[]
  /** 是否必答。后端保证这些字段一定存在，所以这里是必填而非可选 */
  required: boolean
  /** 输入长度上限。仅 `text` / `textarea` 有意义，选择类为 `null` */
  maxLength: number | null
}

/** `GET /api/v1/conversation/questions` 的响应。 */
export interface QuestionsConfig {
  /**
   * 表单版本（当前 `1.2`）。
   * 会话按**创建时**的版本解释作答，不按当前配置 —— 后端
   * `docs/状态数据设计.md` §6.2。所以这个字段要带到会话里，不能丢。
   */
  version: string
  title: string
  description: string
  /** 基础题（13 题，7 题必答） */
  base_questions: QuestionConfig[]
  /** 高级选填题（7 题），界面建议默认折叠 */
  advanced_questions: QuestionConfig[]
}

// ---------------------------------------------------------------- 视图状态

/**
 * 前端**视图**状态：决定当前渲染哪一屏。
 *
 * ⚠️ **它不是会话状态的权威来源。** 设计明确要求「服务端 `state` 是真相，前端不自己实现状态机」
 * （`docs/会话持久化方案.md` §7.3、`docs/状态数据设计.md` §4.3）——
 * 按钮显隐由服务端下发的 `actions` 决定，不由前端自行判断。
 *
 * 因此本类型的定位是：**服务端 `SessionPhase`（13 态）到"渲染哪一屏"的本地映射**。
 * 会话接口接上后，它应当由 `GET /api/v1/sessions/{id}` 的 `snapshot.state` 推导。
 *
 * ⚠️ 也正因为如此，**它绝不能存进 localStorage**：§7.3 明确警告过 ——
 * 本地记"我发起过生成"只会在服务端已经完成时让前端错误地显示"生成中"。
 *
 * ⚠️ 与设计对照，这里**少两个状态**（刻意先不做，等那一步再补）：
 *
 * | 缺的 | 为什么迟早要加 |
 * | --- | --- |
 * | `generation-failed` | `会话持久化方案` §7.4 要求显示「生成已中断」+「重试」按钮；没有它就没地方放这个界面。`HANDOFF.md` §4 坑 #6 把"孤儿生成态"列为切 Postgres 后的**头号 bug** |
 * | `abandoned` | 会话被回收 / 用户放弃 |
 */
export type ViewState =
  | 'form'
  | 'chatting'
  | 'generating-prd'
  | 'review-prd'
  | 'generating-api-docs'
  | 'review-api-docs'
  | 'generating-prompts'
  | 'review-prompts'
  | 'done'

/**
 * 顶部步骤条。
 *
 * ⚠️ 这 5 步是设计里**九步主流程**（`SessionSnapshot.step`，1–9）的**聚合**，
 * 且它的文案本应由服务端的 `step_label` 提供（`状态数据设计` §4.3）。
 * 接上会话接口时要决定用哪一套 —— 若用服务端的 `step`，这里就退化成纯展示。
 */
export const STEPS = [
  { id: 'form', label: '描述产品' },
  { id: 'chatting', label: 'AI 对话' },
  { id: 'review-prd', label: 'PRD' },
  { id: 'review-api-docs', label: '接口文档' },
  { id: 'review-prompts', label: '提示词' },
] as const

/**
 * `ViewState` → 步骤序号（越界表示全部走完）。
 *
 * 生成环节与它对应的"待审核"**归到同一格**：从用户视角看，"PRD 这一步"
 * 既包含生成中也包含审核，进度条不该因为它们分成两格。
 */
export const VIEW_TO_STEP: Record<ViewState, number> = {
  form: 0,
  chatting: 1,
  'generating-prd': 2,
  'review-prd': 2,
  'generating-api-docs': 3,
  'review-api-docs': 3,
  'generating-prompts': 4,
  'review-prompts': 4,
  done: STEPS.length,
}

// ---------------------------------------------------------------- 文档产物

/**
 * 单份产物的状态。**与后端 `backend/services/state.py` 的 `DocStatus` 保持一致。**
 *
 * ⚠️ 和本文件其他类型一样是**手写镜像**：后端改了这里不会自动跟着变。
 * 后端那边是 `Literal`，改动要同步改这里（对照 `backend/api/schemas.py` 的导出）。
 *
 * | 值 | 含义 |
 * | --- | --- |
 * | `not_started` | 还没生成 |
 * | `generating` | 生成中（前台流式期间） |
 * | `pending_review` | 已产出，等审核 |
 * | `approved` | 已通过审核 |
 * | `stale` | 上游产物变了 / 被打回，需要重生成 |
 * | `failed` | 生成失败（含孤儿生成态降级，见 `HANDOFF.md` §4 坑 #6） |
 */
export type DocStatus =
  | 'not_started'
  | 'generating'
  | 'pending_review'
  | 'approved'
  | 'stale'
  | 'failed'

// ---------------------------------------------------------------- 澄清状态（03 篇）

/**
 * 澄清对话的收口状态。**纯启发式算出来的**（见 `utils/clarificationState.ts`），
 * 不是服务端状态机推进的结果 —— 会话层落地后应当改读 `snapshot.step`。
 *
 * | 值 | 含义 |
 * | --- | --- |
 * | `ready` | 末条 AI 明确收口，或用户刚回复且没有待确认项 |
 * | `awaiting_user_reply` | 末条是 AI，且它提了待回复的问题 |
 * | `collecting` | 仍在收集信息（既没收口、也没解析出明确问题） |
 */
export type ClarificationStatus = 'ready' | 'collecting' | 'awaiting_user_reply'

/**
 * 澄清状态评估结果（顶栏 badge + 生成前的二次确认都用它）。
 *
 * ⚠️ **它不是"能不能生成"的判据**：`needsConfirmBeforeGenerate` 只决定
 * 「按钮要不要警示 + 点它要不要先问一句」，不决定 disabled。真正不能生成的只有一种情况
 * —— 没有结构化摘要（那由 `handleGeneratePrdWithSync` 兜底）。
 */
export interface ClarificationState {
  status: ClarificationStatus
  /** 从 AI 末条解析出的待确认问题（结构化优先，启发式兜底），最多 5 条 */
  openQuestions: string[]
  /** 给人看的提醒，如「尚未在对话中补充说明」；非空也会触发确认 */
  warnings: string[]
  /** 顶栏文案，如「澄清进行中 · 2 个待确认」 */
  statusLabel: string
  /** 点「生成 PRD」时是否需要先弹一次确认 */
  needsConfirmBeforeGenerate: boolean
}

// ---------------------------------------------------------------- 对话流（SSE）

/** 对话阶段。与后端 `backend/services/state.py` 的 `DialogueStage` 一致。 */
export type DialogueStage = 'S0' | 'S1' | 'S2' | 'S3' | 'S4' | 'S5'

/**
 * 请求侧的精简消息（只有 role + 正文）。
 *
 * 与完整的消息契约（含 `id` / `stage` / `kind` / `created_at`）不同 —— 那些由服务端补。
 * 两者不要混用。
 */
export interface ConversationTurn {
  role: 'user' | 'ai'
  content: string
}

/** `POST /api/v1/conversation/start-stream` 的入参。 */
export interface StartStreamRequest {
  /** 表单作答。键 = 题目 **id**，值 = 作答 */
  form: Record<string, string>
  stage?: DialogueStage
  round_index?: number
  max_rounds?: number
}

/** `POST /api/v1/conversation/continue-stream` 的入参。 */
export interface ContinueStreamRequest {
  form: Record<string, string>
  /** 按时间正序的历史消息 */
  history?: ConversationTurn[]
  /** 用户本轮输入（必填，后端要求 minLength=1） */
  user_input: string
  stage?: DialogueStage
  round_index?: number
  max_rounds?: number
  known_info?: string
  open_questions?: string
  conflicts?: string
}

// ---------------------------------------------------------------- 产物生成 / 修订（SSE）

/**
 * 分片范围，对应 `{doc_outline}` / `{generate_scope}` / `{scope_spec}`。
 *
 * ⚠️ **三个要么都给、要么整个 `scope` 都不给。** 只给 `scope` 不给 `spec`，
 * 提示词里"严格按字数上限控制篇幅"那条就失去约束，而模型仍以为自己知道范围 ——
 * 比不传更糟。都不给时后端按"单次生成整份"处理 —— 实测 PRD 整份装得下，
 * 而**接口文档与提示词套件整份必然被输出上限截断**（`HANDOFF.md` §4 坑 #18），
 * 所以那两份必须分片。
 */
export interface DocumentScopeRequest {
  /** 完整结构清单（章节标题 + 风格 + 输入来源） */
  outline: string
  /** 本次要生成的部分（章节名或文件名列表） */
  scope: string
  /** 本次范围的详细规格：内容要点、风格、字数上限 */
  spec: string
}

/**
 * 三份产物的标识。**与后端 `backend/services/state.py` 的 `DocKind` 保持一致。**
 *
 * 定义在这里（而不是像早先那样在 `App.tsx` 里就地写一个字面量联合）：
 * 分片计划的类型也要用它，两处各写一份必然漂移 —— 与 `DocStatus` 同理。
 */
export type DocKind = 'prd' | 'api' | 'prompts'

/**
 * 分片计划里的一"片"，来自 `GET /conversation/document-plan`。
 *
 * ⚠️ **计划必须由服务端给，前端不许自己编一套切分规则**：
 * 章节/文件清单只有一份真相（内嵌在 `gen_*.md` 提示词里，`HANDOFF.md` §4 坑 #10），
 * 前端再抄一份必然漂移。所以前端只做两件事：照着 `parts` 循环调用、把结果拼起来。
 *
 * 字段与 `DocumentScopeRequest` 一一对应 —— 直接把这一片塞进生成请求的 `scope` 即可。
 */
export interface DocumentPlanPart {
  /** 从 1 起，用于显示进度 */
  index: number
  /** 给人看的一句话，如「第 7 章（1 章）」 */
  label: string
  scope: string
  spec: string
  outline: string
}

export interface DocumentPlan {
  kind: DocKind
  /** `false` = 整份一次生成（只有 PRD 是这样，实测它装得下） */
  multiPart: boolean
  /** 清单解析自哪个提示词文件（留痕） */
  source: string
  parts: DocumentPlanPart[]
}

/**
 * 四个产物接口共用的**对话侧输入**。
 *
 * ⚠️ 这些本该由服务端按 `session_id` 取（`会话持久化方案` §7.3「服务端 `state` 是真相」），
 * 但会话层还没实现，所以现在由**客户端携带**。后端 `api/schemas.py` 的
 * `ConversationContext` 是同一形状 —— 会话层落地后这四个请求体会整体换成 `{session_id}`。
 */
export interface DocumentContext {
  /** 表单作答。键 = 题目 **id**。留空表示没有表单输入 */
  form?: Record<string, string>
  /** 对话确认的结构化信息。**不传**由后端填「（尚无）」，不要自己填空串 */
  known_info?: string
  open_questions?: string
  conflicts?: string
}

/** `POST /api/v1/conversation/generate-prd-stream` 的入参。 */
export interface GeneratePrdRequest extends DocumentContext {
  scope?: DocumentScopeRequest
}

/**
 * `POST /api/v1/conversation/generate-api-docs-stream` 的入参。
 *
 * `prd_content` **必填**：链条约束要求每个接口都能回指 PRD 的 FR 编号，
 * 没有 PRD 可读，模型只能凭空造接口。
 */
export interface GenerateApiDocsRequest extends DocumentContext {
  prd_content: string
  scope?: DocumentScopeRequest
}

/** `POST /api/v1/conversation/generate-prompts-stream` 的入参。 */
export interface GeneratePromptsRequest extends DocumentContext {
  prd_content: string
  /** 接口文档全文。不传由后端填「（尚无）」 */
  api_content?: string
  scope?: DocumentScopeRequest
}

/** 优化请求里与 `kind` 无关的部分。 */
interface OptimizeDocumentBase extends DocumentContext {
  /** 要修订的章节 / 小节名，必须与文档里的标题**逐字一致**（含 `#` 前缀） */
  section: string
  /** **该节的现有正文**。必填：只给反馈不给原文，模型会从零重写，手改的内容全丢 */
  current_content: string
  feedback?: string
  doc_title?: string
  outline?: string
  scope_spec?: string
  api_content?: string
}

/**
 * `POST /api/v1/conversation/optimize-document-stream` 的入参。
 *
 * ⚠️ 写成**判别联合**而不是把所有字段都设成可选：`kind` 决定要不要 `prd_content`，
 * 后端有一条 `model_validator` 会对 `kind ∈ {api, prompts}` 缺 `prd_content` 返 422
 * （`backend/api/schemas.py`）。写成可选字段的话，这个错要等发请求才知道；
 * 联合类型让它在**编译期**就报出来。
 *
 * ⚠️ 运行时仍然以服务端为准 —— 类型只是镜像，拦不住绕过 TS 的调用方。
 */
export type OptimizeDocumentRequest = OptimizeDocumentBase &
  (
    | { kind: 'prd' }
    | { kind: 'api' | 'prompts'; prd_content: string }
  )

/**
 * 对话消息（UI 侧）。
 *
 * 字段对应后端 `api/schemas.py` 的 `Message`（`docs/状态数据设计.md` §2.2）。
 *
 * ⚠️ 设计里 `role` **只有 `user` / `ai`，没有 `system`** —— 系统提示词是给模型的指令、
 * 不是对话内容，后端 `_serialize_messages` 已经把它丢掉了。所以 `system` 不该流到这里；
 * `MessageList` 仍然会防御性地跳过无法识别的 role（包括 `system`），但那不是"支持 system 消息"。
 */export interface ChatMessage {
  id: string
  role: 'user' | 'ai'
  /**
   * **原始正文**。
   *
   * ⚠️ 对 AI 消息来说它是**结构化 JSON 原文**，不是给人看的散文 ——
   * 后端按 `docs/对话阶段设计.md` §6.2 返回
   * `{message, questions[], conflicts, stage_status, open_questions}`。
   * 它必须原样保留：这段文本会作为 `history` 回传给后端，抠掉 `questions`
   * 就等于让模型看不到自己上一轮问过什么。
   */
  content: string
  /**
   * **给人看的那一份**（仅 AI 消息有）。
   *
   * 由 `content` 派生（`api.ts` 的 `extractStreamingMessage` 从 JSON 里抠出 `message`），
   * 所以它**不落本地存储** —— 读回来时按 `content` 重新算即可。
   * 抠不出来时等于 `content`，不会出现空白气泡。
   */
  display?: string
  /** **仅 UI 用**：这条还在流式接收中（用来显示光标 / 骨架） */
  streaming?: boolean
  /** **仅 UI 用**：这一轮失败了，`content` 可能是残缺的 */
  error?: string
}

// ---------------------------------------------------------------- 入口模式

/** 步骤条里的一格（从 `STEPS` 推导，避免手抄一份 id 联合）。 */
export type StepId = (typeof STEPS)[number]['id']

/**
 * 三种入口。**入口只决定「这一步要不要走」，不决定「能不能生成」** ——
 * 生成能力始终由服务端与链条约束决定（提示词套件缺 `prd_content` 照样 422）。
 *
 * | 入口 | 走什么 | 跳过什么 |
 * | --- | --- | --- |
 * | `structured` | 表单 → 对话澄清 → PRD → 接口文档 → 提示词 | 不跳 |
 * | `prd-shortcut` | 已有 PRD →（先 RAG 检索规范）→ 接口文档 → 提示词 | 表单、对话、PRD 生成 |
 * | `prompts-debug` | 提示词套件（自带 PRD 文本） | 表单、对话、PRD 与接口文档生成 |
 */
export type EntryMode = 'structured' | 'prd-shortcut' | 'prompts-debug'

export interface EntryModeMeta {
  id: EntryMode
  label: string
  /** 一句话说明「跳过什么」，直接显示给用户 */
  hint: string
  /** 这个入口显示的步骤（顺序即步骤条顺序） */
  steps: readonly StepId[]
}

export const ENTRY_MODES: readonly EntryModeMeta[] = [
  {
    id: 'structured',
    label: '从零创建',
    hint: '完整流程：表单 → AI 对话澄清 → PRD → 接口文档 → 提示词',
    steps: ['form', 'chatting', 'review-prd', 'review-api-docs', 'review-prompts'],
  },
  {
    id: 'prd-shortcut',
    label: '导入 PRD',
    hint: '已有 PRD：先 RAG 检索规范与历史示例，再生成接口文档和提示词套件',
    steps: ['review-prd', 'review-api-docs', 'review-prompts'],
  },
  {
    id: 'prompts-debug',
    label: '导入 PRD+接口文档',
    hint: '跳过前置流程，直接生成提示词套件（需要自带 PRD 文本）',
    steps: ['review-prompts'],
  },
]

/**
 * 「表单」这一屏内部的子视图（04 篇）。
 *
 * 改造前：三个入口是**一条全局的入口条**，钉在所有屏之上、随时可切，于是新人一进
 * `/v2` 就要在"从零创建 / 导入 PRD / 导入 PRD+接口文档"之间做一次没有上下文的判断。
 * 改造后：先在一个**路径选择页**里选一次，再落到专属界面；`viewState === 'form'`
 * 这一屏按 `formSubView` 分支渲染。
 *
 * ⚠️ 四个取值**都在 `viewState === 'form'` 之下**（不是四屏）：
 * `import-*` 是"表单这一步里的导入向导"，选完、贴完、点「作为基准并继续」之后
 * 由 `handleImportPrdAsBaseline` 把 `viewState` 抬到 `review-*`，那时子视图自然退场。
 */
export type FormSubView = 'chooser' | 'structured' | 'import-prd' | 'import-prompts'

/**
 * `entryMode` → 该落在哪个子视图（**老会话按它推导，跳过 chooser**）。
 *
 * 与 `stepsForMode` 放在一起：两者都是"入口 → 这一屏该长什么样"的映射，
 * 分两处写迟早漂移。
 */
export function formSubViewForEntryMode(mode: EntryMode): FormSubView {
  switch (mode) {
    case 'prd-shortcut':
      return 'import-prd'
    case 'prompts-debug':
      return 'import-prompts'
    default:
      return 'structured'
  }
}

/** 步骤 → 默认落在哪个视图（步骤条点击用）。 */
export const STEP_VIEW: Record<StepId, ViewState> = {
  form: 'form',
  chatting: 'chatting',
  'review-prd': 'review-prd',
  'review-api-docs': 'review-api-docs',
  'review-prompts': 'review-prompts',
}

/** 该入口要显示哪些步骤。 */
export function stepsForMode(mode: EntryMode): readonly (typeof STEPS)[number][] {
  const meta = ENTRY_MODES.find((item) => item.id === mode) ?? ENTRY_MODES[0]
  return STEPS.filter((step) => meta.steps.includes(step.id))
}

/**
 * `ViewState` → **该入口步骤条**里的序号。
 *
 * 先经全量 `VIEW_TO_STEP` 换成步骤 id，再在入口自己的步骤表里找位置。
 * 两种情况特殊：`done` 归到末尾；该入口没有这一步（例如快捷入口下落到 `chatting`）
 * 归到 **0**（进度条不许倒退），而不是「全部完成」 —— 后者会让进度看起来是满的。
 */
// ---------------------------------------------------------------- 观测（01/02 后端产出）

/**
 * 一次 LLM 调用的摘要（`run_summary.steps[]`）。
 *
 * ⚠️ 字段**全部 optional**：后端可能不发、也可能只发一部分。消费处一律按「可能没有」处理，
 * 后端没上线时页面不能崩。
 */
export interface RunStepSummary {
  step: string
  input_tokens?: number
  output_tokens?: number
  duration_ms?: number
}

/** 一趟生成注入的一个技能包（`run_summary.injected_skills[]` 的元素）。 */
export interface InjectedSkill {
  /** skill id，与 `skills/{id}/` 目录名一致 */
  skill_id: string
  /** 语义化版本（暂时没有 pin 机制，先记下来便于事后定位） */
  version: string
  /** 注入的文件清单（相对技能目录的路径） */
  artifacts: string[]
}

/** 整趟流程的汇总（SSE `run_summary` 事件）。 */
export interface RunSummary {
  request_id: string
  run_type: string
  total_duration_ms: number
  total_input_tokens: number
  total_output_tokens: number
  llm_call_count: number
  /** PRD 是否自动改过一轮（决定第 3 步是 done 还是 pending） */
  revision_applied?: boolean
  steps?: RunStepSummary[]
  /** 预算检查结果（02）；前端只用 level 看告警，不展示百分比 */
  budget?: {
    estimated_input_tokens: number
    budget_tokens: number
    ratio: number
    level: string
  }
  /**
   * 这趟**注入进提示词**的技能包清单（RAG 下线那一篇加的）。
   *
   * 接口文档 / 提示词套件的团队规范与示例、PRD 的 6 章技能包都从这里看得出来 ——
   * 它回答的是"这份产物是按哪一版规范生成的"。
   *
   * ⚠️ **可选且可能是 `null`**：澄清聊天等纯对话 run 没有技能包；
   * 老后端（或后端回滚）不带这个键 → `undefined`。两种情况都按"没有"处理，不要报错。
   */
  injected_skills?: InjectedSkill[] | null

  // ---------- 一次产物生成 = 一个 run（04 观测修复）----------
  // 背景：接口文档 / 提示词套件是**分片生成**的（前端按 `getDocumentPlan()` 循环发 N 次
  // SSE 请求），而 `run_summary` 本来是**每请求一份** —— 所以界面上「本次生成」显示的
  // 只是**最后一片**的账（实测提示词套件最后一片只有 1 个文件：用户等了 2 分钟，面板写 12s）。
  // 后端现在按请求头 `X-Run-ID` 把同一趟的各片并起来，这里就是那份合计的形状。
  //
  // ⚠️ 全部 optional：老后端（或后端回滚）不带这些键，前端要按"没有"处理而不是崩。
  // ⚠️ `total_*` 的**语义随分片进度变化**：第 1 片的帧里它只算那一片，最后一片的帧里
  // 才是整份合计（后端边跑边累加）。前端不做相加 —— 耗时是墙钟，各片相加必然错。

  /** 本次生成的 run id（请求头 `X-Run-ID` 回显）。**拿它一条 grep 能查全整份** */
  run_id?: string
  /** 这一趟已经并进来的各片 `request_id`（顺序同发片顺序） */
  request_ids?: string[]
  /** 当前帧是第几片（1 基） */
  part_index?: number
  /** 这一趟一共几片（= 前端 `plan.parts.length`） */
  part_total?: number
  /** 后端**已经并进来**的片数。`< part_total` 就说明还有片没入账 */
  parts_seen?: number
  /** 整份是否已经并完（`false` = 这一帧只是阶段性合计，**不能当"整份完整"显示**） */
  complete?: boolean
  /** 当前这一片自己的墙钟耗时（`total_duration_ms` 是整份墙钟，两者不是一回事） */
  part_duration_ms?: number
}

/**
 * 上下文占用（澄清流 `done.context_usage`）。
 *
 * 前端**只按 `level` 决定是否显示 amber 提示**；不用百分比进度条，
 * `budget_source` / `hardware_cap` 这类调试字段一律不展示（会被当成 bug）。
 */
export interface ContextUsage {
  estimated_input_tokens: number
  budget_tokens: number
  ratio: number
  level: 'ok' | 'warn' | 'exceed'
  budget_source?: 'policy' | 'hardware'
  model?: string
  policy_cap?: number
  hardware_cap?: number
}

export function stepIndexOf(mode: EntryMode, view: ViewState): number {
  if (view === 'done') return stepsForMode(mode).length
  const stepId = STEPS[VIEW_TO_STEP[view]]?.id
  if (!stepId) return 0
  const index = stepsForMode(mode).findIndex((step) => step.id === stepId)
  return index === -1 ? 0 : index
}

