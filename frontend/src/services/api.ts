/**
 * 后端接口封装。
 *
 * **只写相对路径，绝不硬编码 `http://127.0.0.1:8000`**：
 * 开发期由 Vite 代理转发（见 `vite.config.ts`），生产由反向代理承担同一角色。
 * 硬编码的话部署时必然失效，还会引入跨域。
 */

import axios, { isAxiosError } from 'axios'

import type {
  ContinueStreamRequest,
  DocKind,
  DocumentPlan,
  GenerateApiDocsRequest,
  GeneratePrdRequest,
  GeneratePromptsRequest,
  OptimizeDocumentRequest,
  QuestionsConfig,
  StartStreamRequest,
} from '../types'

/**
 * API 前缀。
 *
 * ⚠️ **版本段 `/v1` 是刻意保留的，别去掉** —— 请求路径少了它会 404。
 * 决定与理由见 `HANDOFF.md` §11 第 8 条。
 */
export const API_BASE_URL = '/api/v1'

/** 共享的 axios 实例。超时给 15s：对话单轮上限也是 15s（F3.14），对齐。 */
export const http = axios.create({
  baseURL: API_BASE_URL,
  timeout: 15000,
  headers: { Accept: 'application/json' },
})

/**
 * 取表单题目定义（20 题 + `version`）。
 *
 * 题目**必须由服务端下发，前端不能自己存一份** —— `docs/对话阶段设计.md` §8 第 7 项。
 * 与提示词外置同理：两份定义必然漂移。
 */
export async function getQuestions(): Promise<QuestionsConfig> {
  const { data } = await http.get<QuestionsConfig>('/conversation/questions')
  return data
}

/**
 * 取某份产物的**分片计划**（不调模型，纯服务端解析 + 分批）。
 *
 * 为什么计划要从服务端拿：章节/文件清单只有一份真相（内嵌在提示词里），
 * 而整份接口文档与提示词套件**必然撞上输出上限被截断**，必须分片生成。
 * 前端**不自己编切分规则**，只照着返回的 `parts` 循环调用（`HANDOFF.md` §4 坑 #10）。
 *
 * ⚠️ 线上字段是 `multi_part`（snake_case），这里转成 `multiPart` —— 转换点只有这一处。
 */
export async function getDocumentPlan(kind: DocKind): Promise<DocumentPlan> {
  const { data } = await http.get<{
    kind: DocKind
    multi_part: boolean
    source: string
    parts: DocumentPlan['parts']
  }>('/conversation/document-plan', { params: { kind } })
  return { kind: data.kind, multiPart: data.multi_part, source: data.source, parts: data.parts }
}

/**
 * 把 axios 的错误转成能直接显示给用户的一句话。
 *
 * axios 的错误对象结构复杂（`error.message` 对 4xx/5xx 只会说 "Request failed with status code 404"，
 * 真正的原因在 `error.response.data.detail`），直接 `String(error)` 会丢掉后端给的信息。
 */
export function describeApiError(error: unknown): string {
  if (isAxiosError(error)) {
    if (error.code === 'ECONNABORTED') {
      return '请求超时：后端可能没启动，或响应太慢'
    }
    if (!error.response) {
      return '连不上后端：确认后端已在 127.0.0.1:8000 启动'
    }
    const { status, data } = error.response
    // FastAPI 的错误体是 { detail: ... }，字符串或数组（422 校验错误时是数组）
    const detail =
      typeof data === 'object' && data !== null && 'detail' in data
        ? (data as { detail: unknown }).detail
        : undefined
    const suffix =
      typeof detail === 'string'
        ? `：${detail}`
        : detail
          ? `：${JSON.stringify(detail)}`
          : ''
    return `后端返回 ${status}${suffix}`
  }
  return error instanceof Error ? error.message : String(error)
}

// ---------------------------------------------------------------- 流式对话（SSE）

/** 一帧的解析结果。`data` 已 JSON 解析；解析失败时是原始字符串。 */
interface ParsedFrame {
  event: string
  data: unknown
}

/**
 * 解析一帧 SSE 文本。
 *
 * 按 SSE 规范处理：`字段: 值`、`:` 开头是注释行、同一帧内多个 `data:` 用换行拼接。
 * 行尾的 `\r` 一并容忍（服务端用 CRLF 时）。
 */
function parseFrame(frame: string): ParsedFrame | null {
  let event = 'message' // SSE 的默认事件名
  const dataLines: string[] = []

  for (const rawLine of frame.split('\n')) {
    const line = rawLine.endsWith('\r') ? rawLine.slice(0, -1) : rawLine
    if (!line || line.startsWith(':')) continue
    const colon = line.indexOf(':')
    const field = colon === -1 ? line : line.slice(0, colon)
    // 规范允许冒号后跟一个空格，要去掉
    const value = colon === -1 ? '' : line.slice(colon + 1).replace(/^ /, '')
    if (field === 'event') event = value
    else if (field === 'data') dataLines.push(value)
  }

  if (dataLines.length === 0) return null
  const raw = dataLines.join('\n')
  try {
    return { event, data: JSON.parse(raw) }
  } catch {
    // 不是 JSON 就把原文交出去 —— 通用函数不该假设负载一定是 JSON
    return { event, data: raw }
  }
}

export interface StreamChunkMeta {
  chunks: number
  chars: number
  /**
   * 输出是否**被模型单次输出上限截断**。
   *
   * 只有**文档类**接口会带这个键（对话接口不带）。判据来自厂商的 `finish_reason`，
   * 因为截断在正文里**看不出来** —— 末尾就是半行表格 / 半句话，与正常写完无法区分。
   * 实测整份生成接口文档必然被截断（`HANDOFF.md` §4 坑 #18）。
   *
   * `undefined` 一律当"没截断"：**不猜**（凭"末尾看起来不完整"去猜会误报，
   * 而误报会让用户不再相信这个提示）。
   */
  truncated?: boolean
  /** 厂商原话（`stop` / `length` / `max_tokens` …）。排查用，界面不直接展示。 */
  finish_reason?: string | null
}

export interface StreamHandlers {
  /** 每收到一段增量就调一次。`fullText` 是到目前为此累积的全文，省得调用方自己拼。 */
  onChunk?: (text: string, fullText: string) => void
  /** 正常结束时调一次，带服务端给的统计。 */
  onDone?: (meta: StreamChunkMeta, fullText: string) => void
}

export interface StreamOptions extends StreamHandlers {
  /** 取消用。**同一个 signal 要传给 `fetch`** —— 中断后 `reader.read()` 会抛 `AbortError`。 */
  signal?: AbortSignal
}

/**
 * 通用 SSE 流读取器：吃一个 `Response`，按帧解析、实时回调，最后返回全文。
 *
 * 与后端 `api/conversation.py` 的协议对应（三个命名事件）：`chunk` / `done` / `error`。
 *
 * 两处最容易写错、这里专门处理的地方：
 *
 * 1. **`TextDecoder` 必须带 `{stream: true}`**。一个汉字在 UTF-8 里占 3 字节，
 *    完全可能被切在两个网络分片之间；不带这个参数会解出 `�`（替换字符），
 *    而且只在长文本里偶发，极难排查。
 * 2. **帧会跨分片**。一帧（`event: …\ndata: …\n\n`）可能分成几段到达，
 *    所以必须留残余缓冲，**只派发完整的帧**。
 */
export async function readStream(
  response: Response,
  handlers: StreamHandlers = {},
): Promise<string> {
  if (!response.ok) throw await toStreamError(response)
  if (!response.body) throw new Error('响应没有可读流 —— 后端返回的不是 SSE？')

  const reader = response.body.getReader()
  const decoder = new TextDecoder('utf-8') // 显式指定，不依赖运行时的默认编码
  let buffer = ''
  let fullText = ''
  let doneMeta: StreamChunkMeta | null = null

  try {
    for (;;) {
      const { value, done } = await reader.read()
      if (done) break

      buffer += decoder.decode(value, { stream: true })
      const frames = buffer.split('\n\n')
      buffer = frames.pop() ?? '' // 最后一段可能不完整，留到下一分片再拼

      for (const frame of frames) {
        const parsed = parseFrame(frame)
        if (!parsed) continue

        if (parsed.event === 'chunk') {
          const text = (parsed.data as { text?: string } | null)?.text ?? ''
          if (text) {
            fullText += text
            handlers.onChunk?.(text, fullText)
          }
        } else if (parsed.event === 'done') {
          doneMeta = parsed.data as StreamChunkMeta
        } else if (parsed.event === 'error') {
          const failure = parsed.data as { type?: string; message?: string } | null
          throw new Error(
            `模型调用失败（${failure?.type ?? 'Unknown'}）：${failure?.message ?? '无详情'}`,
          )
        }
      }
    }
  } catch (error) {
    // 出错或被取消时把底层流也关掉，别让连接挂着
    await reader.cancel().catch(() => undefined)
    throw error
  }

  // 没收到 done 就结束了 = 连接被中途掐断，内容不完整。
  // **不能当成功返回**，否则前端会把半截回复存下来当成完整的一轮。
  if (!doneMeta) {
    throw new Error('流没有正常结束（未收到 done 事件），内容可能不完整')
  }

  // 用 done 帧的 chars 校正。注意按**码点**计数而不是 `.length`：
  // 后者是 UTF-16 码元数，遇到 emoji 之类会比后端（Python `len()` 数码点）多，造成假警报。
  const codePoints = [...fullText].length
  if (codePoints !== doneMeta.chars) {
    console.warn(
      `[stream] 重组得到 ${codePoints} 个字符，done 帧声称 ${doneMeta.chars} —— 可能丢帧或解码有问题`,
    )
  }

  handlers.onDone?.(doneMeta, fullText)
  return fullText
}

/** 非 2xx 响应 → 带后端 `detail` 的 Error（FastAPI 的错误体是 `{detail: ...}`）。 */
async function toStreamError(response: Response): Promise<Error> {
  let detail = ''
  try {
    const body: unknown = await response.json()
    const raw =
      typeof body === 'object' && body !== null && 'detail' in body
        ? (body as { detail: unknown }).detail
        : body
    detail = typeof raw === 'string' ? raw : JSON.stringify(raw)
  } catch {
    detail = ''
  }
  return new Error(`流式请求失败（HTTP ${response.status}）${detail ? `：${detail}` : ''}`)
}

/** POST 一个 JSON body，读它的 SSE 流。 */
async function postStream(
  path: string,
  body: unknown,
  options: StreamOptions = {},
): Promise<string> {
  const response = await fetch(`${API_BASE_URL}${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
    body: JSON.stringify(body),
    ...(options.signal ? { signal: options.signal } : {}),
  })
  return readStream(response, options)
}

/**
 * 首轮流式对话：把表单作答发过去，实时拿回 AI 的开场复盘。
 *
 * ⚠️ 后端那两个端点是 **POST**，而 `EventSource` 只支持 GET —— 所以这里走
 * `fetch` + 手动读流，**不能用 `EventSource`**。也正因如此没有"断线自动重连"，
 * 流的结束由 `done` 事件明确给出。
 *
 * @returns 完整回复文本（同时通过 `onChunk` 实时回调）
 */
export async function startConversationStream(
  request: StartStreamRequest,
  options: StreamOptions = {},
): Promise<string> {
  return postStream('/conversation/start-stream', request, options)
}

/** 接续流式对话：带上历史与用户本轮输入。 */
export async function continueConversationStream(
  request: ContinueStreamRequest,
  options: StreamOptions = {},
): Promise<string> {
  return postStream('/conversation/continue-stream', request, options)
}

// ---------------------------------------------------------------- 产物生成 / 修订（SSE）

/**
 * 四个产物接口的**共同约定**，写在这里省得每条注释重复一遍。
 *
 * ## 与对话接口最关键的一条差别：流的正文就是产物本身
 *
 * 对话接口推的是**结构化 JSON**（`{"message": …, "questions": […]}`），所以那边必须用
 * `extractStreamingMessage()` 把 `message` 抠出来才能渲染（见该函数的说明）。
 *
 * **这四个接口推的是纯 Markdown 正文，没有 JSON 外壳。** 所以：
 *
 * - ✅ 直接把增量 `onChunk` 给 Markdown 渲染器
 * - ❌ **不要**对它们调 `extractStreamingMessage()` —— 那里没有 `"message"` 键，
 *   它只会稳定返回 `null`，然后你会被迫写一段"为空就回落原文"的分支，
 *   而那段分支永远走不到，只会让人以为这两个接口的输出形状一样
 *
 * 实测确认：`gen_api` 的流重组出来就是 `## 第 6 章 接口清单\n\n### 接口清单\n\n| 方法 | …`。
 *
 * ## 其余约定（与对话接口一致）
 *
 * - **POST + SSE**，所以不能用 `EventSource`；走 `fetch` + 手动读帧
 * - 没有"断线自动重连"，终止信号是 `done` 事件；没收到 `done` 就结束会**抛错**
 * - 非 2xx（缺 Key 的 **503**、入参不合法的 **422**）会**抛带后端 `detail` 的错误**，
 *   不会悄悄变成空流 —— 用 `describeApiError()` 转成可显示的文案
 * - ⚠️ **这几个接口不是产物的权威写入路径**：它们是前台流式，请求断了这一轮就没了。
 *   设计要求生成是后台任务（`会话持久化方案` §7.1），产物落库必须另走任务层。
 *   所以流完之后**必须**把结果交给调用方存起来，不能指望服务端已经存了。
 *
 * ## 分片
 *
 * **整份 PRD 能一次生成完**（技能包 6 章，实测未截断，见
 * `backend/validation_out/prd_skill.txt`）；**整份接口文档与提示词套件会撞上
 * 输出上限被截断**（实测 17257 / 18354 字符，`finish_reason=length`）。所以 `scope`
 * 现在还是可选参数，但第二、三份产物**必须**分片 —— 见 `HANDOFF.md` §4 坑 #18、§6 第 8 项。
 * 分片落地前，调用方必须把 `done` 帧的 `truncated` 告诉用户（`StreamChunkMeta.truncated`）。
 */

/**
 * 生成 PRD（默认走技能包 `skills/prd-generator/` 的 6 章结构：
 * 产品概述 / 功能需求 / 页面设计 / 技术规格 / 非功能需求 / 项目范围）。
 *
 * 整份 PRD 实测**能一次生成完**（未截断），所以不传 `scope` 是可用形态；
 * 但后端仍支持分片（章节级重生成 F8.6 会用到）。老结构（15 章 + 2 附录）只在
 * 服务端把 `PRD_USE_SKILL` 关掉时才走，前端不区分。
 *
 * @returns 完整 Markdown 正文（同时通过 `onChunk` 实时回调）
 */
export async function generatePrdStream(
  request: GeneratePrdRequest,
  options: StreamOptions = {},
): Promise<string> {
  return postStream('/conversation/generate-prd-stream', request, options)
}

/**
 * 从**已通过审核的 PRD** 推导接口文档。
 *
 * `prd_content` 必填且不能只有空白 —— 空/空白会被后端拦成 **422**
 * （`NonBlankStr`），`describeApiError()` 会带上后端的 `detail`。
 */
export async function generateApiDocsStream(
  request: GenerateApiDocsRequest,
  options: StreamOptions = {},
): Promise<string> {
  return postStream('/conversation/generate-api-docs-stream', request, options)
}

/**
 * 生成提示词套件（消费 PRD + 接口文档）。
 *
 * ⚠️ 产出是**多文件**，用 `=== FILE: <相对路径> ===` 分隔各文件
 * （`docs/提示词套件模板.md`）—— **调用方负责按这个分隔行切分**，
 * 本层与后端都不解析它。
 */
export async function generatePromptsStream(
  request: GeneratePromptsRequest,
  options: StreamOptions = {},
): Promise<string> {
  return postStream('/conversation/generate-prompts-stream', request, options)
}

/**
 * 按用户反馈**只修订一节**（F8.6「针对不合格项一键重生成对应章节」）。
 *
 * ⚠️ `current_content`（该节现有正文）必填：只给反馈不给原文，模型会从零重写，
 * **用户手改过的内容全丢**（`会话持久化方案` §7.4 要求重生成前对已手改内容二次确认）。
 *
 * ⚠️ 修订 `kind` 为 `api` / `prompts` 时还必须给 `prd_content` —— 类型上是判别联合，
 * 漏了编译期就报；运行时后端也会拦成 **422**。
 *
 * @returns 修订后的该节 Markdown（同时通过 `onChunk` 实时回调）
 */
export async function optimizeDocumentStream(
  request: OptimizeDocumentRequest,
  options: StreamOptions = {},
): Promise<string> {
  return postStream('/conversation/optimize-document-stream', request, options)
}

/**
 * 从 AI 那轮的**原始 JSON** 里读 `stage_status`（`"asking"` / `"done"`）。
 *
 * 用途：对话页的「生成 PRD」按钮需要一个"澄清是否已经聊完"的信号。
 *
 * ⚠️ **这是模型自报的状态，不是状态机推进的结果。** `docs/对话阶段设计.md` §8 第 5 项
 * 明确要求阶段由**服务端**推进、不让模型自判 —— 现在没有 `SessionStore`，服务端不下发
 * 任何东西，所以只能退而用模型自己报的 `stage_status` 当按钮的显隐条件。
 * 会话层落地后这里应当改成读 `snapshot.step` / `snapshot.actions`。
 *
 * 刻意**不用 `JSON.parse` 整段**：流式过程中拿到的可能是半截 JSON，抛异常还不如返回
 * `null` 让调用方按"还不知道"处理。只在能完整解析时才给结论。
 *
 * @returns `'asking'` / `'done'`；解析不出来（半截、被 ``` 包裹、字段缺失）返回 `null`
 */
export function readDialogueStageStatus(raw: string): 'asking' | 'done' | null {
  const text = raw.trim()
  if (!text.startsWith('{')) return null
  try {
    const parsed: unknown = JSON.parse(text)
    if (typeof parsed !== 'object' || parsed === null) return null
    const status = (parsed as { stage_status?: unknown }).stage_status
    return status === 'asking' || status === 'done' ? status : null
  } catch {
    return null
  }
}

/**
 * 从**尚未接收完整**的模型输出里，抠出 `message` 字段的当前值。
 *
 * **这不是可选的桥接，是必需的**（原来标着"临时方案、可能删掉"，实测后改了结论）。
 *
 * `docs/对话阶段设计.md` §6.2 规定输出是结构化 JSON，而 F3.14 要求流式，两者共存的方式是
 * 「逐字流式原始 JSON」—— 实测确认后端推的就是这个：
 *
 *     {"message": "我看了你的表单…", "questions": [{id, question, suggested_answer, reason}],
 *      "conflicts": [], "stage_status": "asking", "open_questions": []}
 *
 * 于是把增量直接丢给 UI，用户看到的是 `{"message": "我先把…` 加上正文里**字面量**的 `\n`
 * （JSON 转义，不是换行）。所以必须先把 `message` 抠出来才能渲染。
 *
 * ⚠️ **它只负责显示。** 回传给后端的 `history` 要用**原始 JSON**，
 * 抠掉 `questions` 就等于让模型看不到自己上一轮问过什么 —— 见 `App.tsx` 的
 * `content` / `display` 拆分。
 *
 * ⚠️ 还**没做完**的是「一键采纳建议」：`questions[].suggested_answer` 收得到但没用上，
 * 需要决定是等整个 JSON 收完再解析，还是边流边增量解析（`HANDOFF.md` §6 第 7 项）。
 *
 * @returns 已抠出的正文（可能是空串 —— 流式刚开始、`"message"` 那个键还没出现时就是）；
 *          **返回 `null` 表示压根没检测到 `message` 字段** —— 调用方应回退到显示原始文本。
 */
export function extractStreamingMessage(raw: string): string | null {
  const marker = '"message"'
  const at = raw.indexOf(marker)
  if (at === -1) return null

  const colon = raw.indexOf(':', at + marker.length)
  if (colon === -1) return ''
  const openQuote = raw.indexOf('"', colon + 1)
  if (openQuote === -1) return ''

  let out = ''
  for (let i = openQuote + 1; i < raw.length; i += 1) {
    const ch = raw[i]
    if (ch === '\\') {
      const next = raw[i + 1]
      if (next === undefined) break // 转义序列被切断，等下一片
      if (next === 'n') out += '\n'
      else if (next === 't') out += '\t'
      else if (next === 'r') out += '\r'
      else if (next === 'u') {
        const hex = raw.slice(i + 2, i + 6)
        if (hex.length < 4 || !/^[0-9a-fA-F]{4}$/.test(hex)) break // \uXXXX 被切断
        out += String.fromCharCode(Number.parseInt(hex, 16))
        i += 4
      } else out += next // \" \\ \/ 等
      i += 1
      continue
    }
    if (ch === '"') break // 字符串结束（完整 JSON 的情况）
    out += ch
  }
  return out
}
