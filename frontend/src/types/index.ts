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

