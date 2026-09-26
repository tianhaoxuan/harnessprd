# harnessprd frontend

需求 → PRD → 接口文档 → 提示词套件 的前端。

接手开发请先读仓库根目录的 [`../HANDOFF.md`](../HANDOFF.md)。选型与实测版本见
[`../docs/技术栈.md`](../docs/技术栈.md) §2.3。

## 当前状态

表单、对话、三份产物的生成与审核修订，整条链路都已经接进 `App.tsx`。

| 文件 | 内容 |
| --- | --- |
| `src/types/index.ts` | 类型镜像（`QuestionConfig` / `QuestionsConfig`）、视图状态（`ViewState` / `STEPS` / `VIEW_TO_STEP`）、对话消息（`ChatMessage`，含 `content` / `display` 拆分）、产物状态（`DocStatus`，镜像 `backend/services/state.py`） |
| `src/services/api.ts` | axios 实例、`getQuestions()`、`describeApiError()`、`readStream` 与 6 个流式函数（2 个对话 + 4 个产物）、`extractStreamingMessage()` |
| `src/services/storage.ts` | localStorage 封装（按 `会话持久化方案` §4.1、§4.2、§8.2 的 key 与信封约定）与会话键 |
| `src/services/download.ts` | `downloadFile()`、`safeFileName()`，Blob 下载与文件名清洗 |
| `scripts/check-plain-text.mjs` | 离线守卫：拦住"在 JSX 文本里写 `**加粗**`" |
| `src/components/FormStep.tsx` | 20 题表单：按 `type` 渲染四种控件、必答红星号、高级题折叠、前端校验（`validateForm` 导出给 App 复用） |
| `src/components/StepProgress.tsx` | 顶部步骤条，5 格，由 `ViewState` 投影 |
| `src/components/MessageList.tsx` | 消息列表：用户靠右、AI 靠左，AI 正文渲染 Markdown（含 GFM 表格），贴底才自动滚 |
| `src/components/ChatInput.tsx` | 输入框：Enter 发送、Shift+Enter 换行、输入法组合态不误发、高度自适应 |
| `src/components/Markdown.tsx` | 对话与文档共用的 Markdown 渲染，样式映射只有这一份 |
| `src/components/DocumentReview.tsx` | 产物审核面板：生成中只读预览、生成后可编辑、AI 优化（F8.6）、一键复制、底部自定义操作按钮 |
| `src/App.tsx` | 视图切换、取题目、表单双向绑定、草稿、对话编排、产物编排（3 个生成 + 优化 + 通过 / 重新生成、落本地、刷新恢复） |

### 产物编排

`App.tsx` 里的状态是 `prdContent`、`apiDocsContent`、`promptsContent`（三份正文）、
`isGenerating` 与 `generatingKind`（在飞的是哪一份）、`approvedDocs`、`docFailure`、`streamingDoc`。
`DocStatus` 是派生的，不另存一份：依次看"在不在生成、有没有失败、通没通过、有没有内容"，
各存一份必然漂移。

流程是：

```
对话聊完（stage_status=done） → 生成 PRD → 通过 → 生成接口文档 → 通过
                            → 生成提示词套件 → 通过 → done
```

有四个刻意做的决定。

1. 「生成 PRD」的显隐条件用模型自报的 `stage_status`（`readDialogueStageStatus()`）。
   这违反了「阶段由服务端推进、不让模型自判」（`对话阶段设计` §8 第 5 项），
   但没有 `SessionStore`、服务端不下发任何东西，这是唯一可用的信号。
   会话层落地后应改成读 `snapshot.step` 与 `snapshot.actions`。

2. 失败不覆盖上一版。生成失败时不把半截内容写进产物，否则一次失败的重生成会把上一版好文档顶掉。
   界面显示上一版加失败原因。

3. `known_info` 只取用户原话。结构化合并属于 `apply_event`（未实现），所以先用用户亲口说过的内容顶一下，
   明确排除 AI 的 `suggested_answer`：那是它的建议、不是用户的确认，当事实喂进去就是
   `gen_common.md` 点名禁止的"拿邻近内容顶替"，而错位的内容看起来像真的（坑 #3）。
   代价是缺结构化 `known_info` 会让生成质量下降（坑 #4），这是数据流缺口，提示词救不了。

4. 分片生成：计划由服务端给，前端只负责循环与拼接。整份接口文档（实测 17257 字符）与
   提示词套件（实测 18354 字符）必然撞上单次输出上限被截断，所以 `handleGenerateDocument`
   先 `getDocumentPlan(kind)` 拿计划，再逐片调用、逐片累加，最后 `stitchParts()` 拼接。
   前端不自己编切分规则，因为章节与文件清单只有一份真相（内嵌在 `gen_*.md` 里，坑 #10），
   由 `services/document_plan.py` 解析后经 `GET /conversation/document-plan` 下发。

   PRD 的计划是一片（整份实测装得下，当前技能包结构 6 章、2967 字符，不截断），
   走同一套循环，没有特例分支。计划里的标签由服务端按**真实结构**给出（技能包 → 「整份（6 章）」），
   所以界面上看到的章数不会与产物不符。
   实测效果：接口文档 3 片拼成 19763 字符，1 到 9 章与附录 A/B 齐全；套件 3 片拼成 28331 字符，
   12 个文件块；两者都不再截断（`HANDOFF.md` §4 坑 #20）。
   某一片失败时整份都不写入，前几片成功也不行，因为缺章的"完整感"比明说失败更危险；
   失败文案会点明断在第几片。生成中显示「正在生成第 2/3 片：…」，否则用户面对的是一个
   长时间不动的生成中。

另外，「通过」是本地推进，不是真实的状态变更。真实实现要发 `approve` 事件
（`POST /sessions/{id}/events`，仍是 501）。理由与表单提交那处相同，会话层就绪后整体删掉换发事件。

#### 状态控制与流程守卫

流程控制集中在两处，不在渲染里写 if 链：`STAGE_ACTIONS`（模块级表）管每个审核阶段的
通过按钮文案、通过之后去哪个阶段、必需哪份上游；`docActionsFor(kind)` 按那张表生成按钮，
决定每个按钮何时出现、何时可点。

`STAGE_ACTIONS` 长这样，`next: null` 是"这一步之后没有下一步"的唯一声明处：

```ts
prd:     { approveLabel: '通过，去生成接口文档',   next: 'api',     upstream: null }
api:     { approveLabel: '通过，去生成提示词套件', next: 'prompts', upstream: 'prd' }
prompts: { approveLabel: '通过并完成任务',        next: null,      upstream: 'prd' }
```

五道守卫，都有断言盯着：

- 空产物不能通过：提示「还没有内容，不能通过」
- 缺已通过的上游不能通过：提示并把用户带到那份上游，不让人干站着
- 缺上游不能生成下游：「生成」按钮直接 `disabled`，`title` 里写明缺哪一份
- `handleEndTask` 要求三份全通过：提示还剩几份，并跳到第一个没过的那一步
- 改过或重新生成之后撤销已通过：内容变了，旧的通过结论不再成立

有序性上有一处关键：**「下一步：X」只在已通过时出现**，不是有内容就出现。
否则用户能在没通过 PRD 的情况下跳到接口文档页，而那条链条约束在服务端会返回 422。
想看后面几步请用调试跳转行，那是显式的逃生口，不混进正常按钮里。

`handleEndTask` 还有两个必须记住的坑，都实测踩到过：

1. 不能读刚 `setState` 过的 state。`handleApproveDocument` 里 `setApprovedDocs(...)` 之后
   紧接着调 `handleEndTask()`，而 state 更新是异步的，后者闭包里还是旧的 `approvedDocs`，
   于是"通过最后一份"会被自己判成"它还没通过"，拒绝并把人弹回原页。
   修法是先把新的一份算出来，再作为参数传进去（`handleEndTask(approved?)`）。

2. 不能写 `onClick: handleEndTask`，必须写 `() => handleEndTask()`。前者会把 `MouseEvent`
   当成那份 map 传进去，"缺哪几份"算出来全是缺，永远进不去完成页。
   而 `DocumentAction.onClick` 的类型是 `() => void`，多一个可选参数的函数也满足它，
   类型检查不会报。

#### 「生成 PRD」按钮的两个条件

显示与可用是刻意分开的。显示的条件是"已有会话且至少收到一条 AI 回复"，
因为开场那一轮还没回来时显示一个灰按钮毫无意义；可用的条件是"澄清结束（`stage_status=done`）
且没有流在跑"，没聊完就生成等于拿半份输入去写 PRD。

这里刻意选择"显示而禁用"而不是"条件不满足就藏起来"：用户需要知道这里有个按钮、但还差点什么，
藏起来只会让人以为流程断了。禁用时旁边有一句说明，提到 `stage_status`。
已有 PRD 时文案变成「重新生成 PRD」并提示会覆盖。

#### 完成页与下载

`done` 不是死胡同。页面有三张产物卡片，每张显示标题、已通过、字数、即将下载的文件名，
可以单独下载为 `.md`，也能回到对应审核页。另外从任何审核页（三份都通过时）都能进完成页，
早先只在提示词套件阶段给这个按钮，结果是"从完成页点进接口文档看一眼就回不去了"。

`downloadFile(filename, content)` 走 Blob 加临时 `<a download>`，三个地方必须写对：

- 临时 `<a>` 要挂进文档再 `click()`，游离节点在 Firefox 里不触发下载
- `URL.revokeObjectURL` 要延后（这里 1 秒），紧接 `click()` 就释放会取消下载，而不释放则 Blob 泄漏
- 不加 UTF-8 BOM。加了文件开头会多一个 `\ufeff`，Markdown 第一个标题会被解析器带上它

BOM 那条是明确的取舍，不是遗漏。现代编辑器（VS Code、Win10 1903 之后的记事本）都能认无 BOM 的
UTF-8，而 BOM 在 git diff 里是噪声。若以后有用户反馈"老记事本打开是乱码"，改法是在 Blob 前
加一行 `'\ufeff'`。

`safeFileName()` 清洗的是用户填的产品名，它可能含 `/ \ : * ? " < > |`，这些在 Windows 上非法，
在某些浏览器里还可能被当成路径分隔符。例如 `群聊/周报:助手*` 会变成 `群聊-周报-助手`。
规则是：非法与控制字符换成 `-`，连续空白与连字符折叠，去掉首尾的点和连字符
（Windows 会悄悄吃掉结尾的点），截断到 60 字符，清空后回落 `fallback`。
最终文件名是 `{清洗后的产品名}-{主干}.md`，主干来自 `DOC_META[kind].fileStem`
（`PRD`、`接口文档`、`提示词套件`）；刻意不用 `title`，那个带全角括号，拼出来啰嗦。

卡片上显示的文件名与实际下载的文件名是同一个函数（`downloadNameFor`）算的。
两处各拼一次必然漂移，而"显示的名字和存下来的名字不一样"是很难被发现的 bug。

提示词套件是多文件产物，下载下来是一个带 `=== FILE: ===` 分隔行的 `.md`，按文件拆开还没做（见待办）。
页面上有明确说明，免得用户以为漏了文件。

下载功能实测过（Chromium 加 CDP，36/36）：把 Chrome 的下载行为改成落到磁盘再核对真实文件，
覆盖 `safeFileName` 的 9 个边界（路径分隔符、控制字符、全空白、超长、首尾点与连字符）、
完成页三张卡与文件名显示，以及用含 `/ : *` 的产品名走完整流程后落盘的三个文件确实被清洗成
`群聊-周报-助手-{PRD,接口文档,提示词套件}.md`，内容 UTF-8 中文完好、无 BOM、三份互不覆盖、
无 `.crdownload` 残留、重新开始后清空。这里的关键是如果只断言"调了 `createObjectURL`"，
上面任何一个文件名或编码问题都抓不到。

状态控制也实测过（38/38）：按钮的显示与禁用两态、已有 PRD 后文案变化、五道守卫、
三个阶段各自的按钮集合与文案、「下一步」只在通过后出现、通过最后一份直接进完成页且不误报、
完成页能回到任一审核页、`handleRestart` 清空并回表单页、重新生成撤销已通过、页面里没有字面量 `**`。

浏览器实测 47/47 覆盖：按钮的显隐条件（`stage_status=done` 前后）、生成中只读加光标、
三个生成请求的请求体（`form` 与 `known_info` 来自用户原话且不含 AI 建议、`prd_content` 的链条传递、
不给 PRD 传 `scope`）、AI 优化只改一节并正确拼回（含"模型只给子节正文"的兜底路径）、通过链推进、
localStorage 里三份正文与 `approved` 标记、刷新后恢复到正确的那一屏。
这一轮用的是罐装 SSE，传输层已在 api 层真实验证 36/36；这里验的是编排。

### 流式对话（SSE）

| 函数 | 作用 |
| --- | --- |
| `readStream(response, { onChunk, onDone })` | 通用 SSE 读取器：按帧解析、实时回调、最后返回全文 |
| `startConversationStream(request, options)` | 首轮对话（`POST .../start-stream`） |
| `continueConversationStream(request, options)` | 接续对话（`POST .../continue-stream`） |
| `generatePrdStream(request, options)` | 生成 PRD（`POST .../generate-prd-stream`） |
| `generateApiDocsStream(request, options)` | 从 PRD 生成接口文档（`POST .../generate-api-docs-stream`） |
| `generatePromptsStream(request, options)` | 从 PRD 生成提示词套件（`POST .../generate-prompts-stream`） |
| `optimizeDocumentStream(request, options)` | 按反馈修订某一节（`POST .../optimize-document-stream`，F8.6） |

后端那几个端点是 POST，所以不能用 `EventSource`（它只支持 GET），走的是 `fetch` 加手动读流。
随之也没有断线自动重连，不需要 `[DONE]` 哨兵。

两个地方必须写对，都已实现并有浏览器测试覆盖：

1. `TextDecoder` 要带 `{stream: true}`。一个汉字在 UTF-8 里占 3 字节，完全可能被切在两个网络分片之间；
   不带这个参数会解出 `�`，而且只在长文本里偶发，极难排查。
2. 帧会跨分片。一帧（`event: …\ndata: …\n\n`）可能分成几段到达，所以必须留残余缓冲，
   只派发完整的帧。

另外三个刻意的行为：没收到 `done` 就结束要抛错，不当成功返回，否则前端会把半截回复存成完整的一轮；
非 2xx（比如缺 Key 的 503）要抛带后端 `detail` 的错误，不悄悄变成空流；
`done.chars` 对不上时 `console.warn`，比较时按码点（`[...text].length`），用 `.length` 会把 emoji
算成 2，产生假警报。

`extractStreamingMessage()` 不是可选桥接，是必需的。原先这里写的是"临时方案、定下来后可能删掉"，
实测把这个结论推翻了：后端推的就是 `对话阶段设计` §6.2 的结构化 JSON，直接渲染出来是一坨 JSON 语法，
正文里的 `\n` 还是字面量而不是换行。它只负责显示，回传给后端的 `history` 必须用原始 JSON，
详见下面「对话编排」。

### 产物生成与修订 API（SSE）

四个函数与对话那两个共用同一条读取路径（`postStream` → `readStream`），所以上面那些必须写对的地方
（`TextDecoder` 带 `stream: true`、帧跨分片要留残余缓冲、没收到 `done` 就抛错、非 2xx 抛带 `detail`
的错误）对它们同样成立，不用各写一遍。

与对话接口唯一但关键的差别是：这四个流的正文就是产物本身。

| | 对话接口 | 产物接口 |
| --- | --- | --- |
| 流的正文 | 结构化 JSON（`{"message": …, "questions": […]}`） | 纯 Markdown，没有 JSON 外壳 |
| 增量怎么用 | 必须 `extractStreamingMessage()` 抠出 `message` 才能渲染 | 直接丢给 Markdown 渲染器 |

不要对产物接口调 `extractStreamingMessage()`。那里没有 `"message"` 键，它只会稳定返回 `null`，
然后你会写一段"为空就回落原文"的分支，而那段分支永远走不到，只会让后来者以为两个接口的输出形状一样。
实测 `gen_api` 的流重组出来就是 `## 第 6 章 接口清单\n\n### 接口清单\n\n| 方法 | …`。

请求体形状对应后端 `api/schemas.py` 的同名模型，由客户端携带上下文（`form`、`known_info`、
`prd_content` 等）。这些本该由服务端按 `session_id` 取，而会话层还没实现，所以现在由客户端带。
会话落地后这四个请求体会整体换成 `{session_id}`。

两个刻意的类型设计：`OptimizeDocumentRequest` 是判别联合，`kind` 决定要不要 `prd_content`；
写成"全是可选字段"的话，漏 `prd_content` 要等后端返 422 才知道，联合类型让它在编译期就报
（已实测：故意漏字段时 `tsc` 报 TS2345，而合法的 `kind: 'prd'` 不报）。
可选字段没传就不注入，`undefined` 的键会被 `JSON.stringify` 丢掉，于是后端自己的默认值生效，
「（尚无）」这个哨兵只在服务端定义一份。

这四个接口不是产物的权威写入路径。它们是前台流式，请求断了这一轮就没了
（`会话持久化方案` §7.1 要求生成是后台任务），所以流完之后必须把结果存起来，不能指望服务端已经存了。
另外 `generatePromptsStream` 的产出是多文件（用 `=== FILE: <相对路径> ===` 分隔），本层不解析它；
按分隔行切分是调用方的事，目前还没有对应的切分函数，接提示词套件页时要么加一个，要么在页面里就地切。

真后端加真模型下实测 36/36：四个函数的 URL、方法、请求头、请求体字段（含 `scope` 逐字转发、
未传的可选字段不注入）、`onChunk` 的 `piece` 与 `fullText` 两种参数、返回值等于最后一次 `fullText`、
422 错误带上字段名、网络失败可读；真实 SSE 下片段数递增、输出是 Markdown 而非 JSON 外壳、
FR 可追溯；真实修订只动一节且体现了反馈；`AbortSignal` 取消会抛错，而不是静默返回半截文本。

### 对话编排

```
填表提交 → handleStartConversation → POST /conversation/start-stream    → 流式渲染 → 落本地
发消息   → handleSendMessage      → POST /conversation/continue-stream → 流式渲染 → 落本地
```

状态分成三块，分法是刻意的：`messages` 只装完整轮次（`ChatMessage[]`）；
`streamingContent` 装本轮正在接收的原始增量（字符串）；`conversationId`、`formVersion`、
`roundIndex` 是会话元信息。

流式增量不能往 `messages` 里塞。一轮会推几百个 chunk，那样会连带触发落本地的 effect 和整列表重渲染；
分开之后流式期间 `messages` 不变，只有那一个字符串在动。列表里那条"正在输入"的气泡是渲染时
合成进去的（`renderedMessages`），所以 `messages` 永远是干净的。

#### 一条消息为什么存两份

`content` 是模型原始输出，也就是结构化 JSON 原文，用来作为 `history` 回传后端；
抠掉 `questions` 就等于让模型忘了自己上一轮问过什么。
`display` 是从 JSON 里抠出的 `message` 正文，渲染给用户看，纯派生，不落本地存储，
读回来按 `content` 重算。

派生逻辑在 `App.tsx` 的 `deriveDisplay()`，有两处实测踩出来的细节：

1. 流式最开头会闪 JSON 残片。模型刚吐 `{` 而 `"message"` 键还没出现，这时回落到原文就会闪一个 `{`
   （实测确实会闪）。所以"看起来是 JSON 但还没抠出 `message`"时，流式期间返回空串，
   气泡里只剩光标，正是想要的"正在思考"观感。
2. 收完了仍没有 `message`，说明后端违反了 §6.2 契约。这种情况把原文摆出来，宁可难看也要让人看见，
   不要静默变成空气泡。

#### 失败时的三种收尾

三种情况的处理刻意不同：

- 一个字都没收到（后端没起、503、断网）：撤掉刚乐观加上的用户消息，并抛出，让 `ChatInput`
  把原文还回输入框。不撤的话列表里有一条、输入框里又有一条，看着像发了两遍。
- 已收到半截（流中途收到 `error`）：用户消息和半截正文都留在界面上并标成失败，输入框不回填。
  半截已经在那儿了，把原文塞回输入框反而让人无从下手。
- 被自己掐掉（清空重来、重新开场、卸载）：直接收场，不弹错误、不写残缺消息、也不动 `sending`。
  那不是失败，是取消。这一条是潜在竞态的守卫，当前 UI 没有"停止"按钮所以够不到它，
  加了取消功能之后它才是必需的。收尾前要先判断 `abortRef.current === controller`，
  否则被顶掉的那一轮结束时会 `setSending(false)`，把正在跑的新一轮误标成已结束。

两种失败都不落本地，只存完整的一轮（user 加一条成功的 ai）。注意光按 `!error` 过滤不够：
失败那轮里用户消息身上没有 error 标记（错在 AI 那条上），于是会存下一条永远等不到回复的提问，
所以用户消息必须检查它后面那条回复。这条是浏览器实测抓出来的。

#### 轮次与阶段：一半传、一半不传

`continue-stream` 的 `round_index` 传，它就是用户第几次发言，前端确实知道；并在 `MAX_ROUNDS`
处封顶，不封顶会出现「第 7 / 4 轮」这种自相矛盾的提示词。

`stage` 不传，让后端默认值生效。阶段该由服务端状态机推进（`对话阶段设计` §8 第 5 项），
前端并不知道现在是 S1 还是 S3，硬编一个只会骗模型。

`MAX_ROUNDS = 4` 是后端 `DEFAULT_MAX_ROUNDS` 的镜像，属于权宜之计。服务端状态机接上后，
这个常量要连同 `roundIndex` 一起删掉。

### 本地持久化（`SessionData`、`saveSession`、`loadSession`）

| key | 内容 |
| --- | --- |
| `harnessprd:{env}:1:session` | 整份会话：`sessionId`、`formVersion`、`form`、`messages`、`roundIndex`、`documents`、`viewState` |
| `harnessprd:{env}:1:draft` | 表单草稿。它是会话之前的临时存储，还没提交表单时没有会话可言，所以单独一把 |

会话只有一把键（`SESSION_KEY`），由 `storage.ts` 的 `sessionKey()` 按上述约定算出来，
不是硬编码字面量，写死会让开发环境与线上共用一份数据。

早先的实现是两把键（`conversation-pointer` 加 `conversation:{id}`），已经合并：`sessionId` 就存在
记录里，少一次读，也少一类"指针与记录失步"的 bug。恢复路径没变，接口就绪后把 `loadSession()`
换成 `GET /sessions/{data.sessionId}`（`会话持久化方案` §7.3）。键约定、信封、环境隔离、
schema 版本失效、7 天过期一个都没丢，因为读写仍然走 `storage.ts` 的 `readLocal` 与 `writeLocal`。

`SessionData` 的形状定义在 `App.tsx` 而不是 `storage.ts`，因为那是产品决定（哪些字段值得留住），
不是存储约定。storage 层只提供 key 约定、信封与泛型读写，不需要随会话结构变化而改。

#### 为什么要存 viewState，以及必须配套的降级

§7.3 警告过"本地记我发起过生成，刷新后会错误地显示生成中"。对策不是不存，而是 `loadSession()`
把 `generating-*` 一律降级成对应的 `review-*`（§7.4 的孤儿生成态），并通过返回值里的 `interrupted`
把这个事实报给调用方，页面顶部给一条「上次生成…被刷新打断了，当前显示的是上一次的版本」的提示。

这两件事必须成对存在：只存不降级，刷新后就显示一个根本没在跑的生成中；只降级不提示，
用户会把上一版内容当成刚刚生成出来的。实测覆盖：改盘上的 `viewState` 为 `generating-prd` 再刷新，
断言落在审核页、没有生成光标、有打断提示。

存视图状态还有个好处：`done`、`chatting`、具体哪一个审核页都能精确还原，不用按内容猜。
老记录没有 `viewState` 时才回落到"第一份未通过的产物的审核页"。

有一个反复出现的坑要记住：纯文本里不能写 Markdown 记号。`**加粗**` 在 JSX 文本节点里不是 Markdown，
会连星号一起显示给用户。实测在 6 处踩到，其中一处从旧代码继承，最后一次是刚写完这条说明之后又踩的。
所以规则记在文档里没用，现在有 `pnpm check:text` 兜底：去掉注释后源码里不该再出现 `**`，
否则 `pnpm build` 直接失败。要加粗就写 `<strong className="font-medium">`，
纯字符串里（三元表达式的文案）直接去掉强调。这和"把 AI 回复的原始 JSON 直接渲染"（坑 #15）
是同一类错误：面向人的文本带着记号原样出现。

另外 `streaming`、`error`、`display` 都不落盘：前者会让刷新后的消息永远显示"正在输入"，
中间那个的正文可能是残缺的，最后一个是纯派生。三个都有断言盯着。产物侧同理，只存正文加 `approved`，
不存 `generating` 与 `failed`。

会话落盘是防抖加最长等待，分别是 800ms 和 2000ms。要防抖是因为产物正文是逐键写进 `prdContent` 的
（`DocumentReview` 的 textarea 每敲一个字就 `onContentChange`），不防抖就是每个按键一次
localStorage 同步写，而文档 §2 明确警告它是同步 API、会阻塞主线程。要上限是因为光有防抖会被饿死：
每处 state 变化都会清掉上一个定时器、重排一次，所以只要变化间隔一直小于 800ms，就一次都不会落盘。
实测踩到过：探针以 250ms 一步连点走完整条链条，盘上始终只有草稿、没有会话；真实场景里
"连续打字 30 秒然后关标签页"是同一回事。加了 `SESSION_MAX_WAIT_MS` 之后，从第一次待写算起最多
推迟 2 秒就强制落一次，最坏情况只丢 2 秒输入，而不会一直没存。

旧的 `conversation-pointer` 与 `conversation:{id}` 两把键已不再被读取。项目尚未发布，
所以不做数据迁移，旧键 7 天后按 §8.2 自然过期。本地还留着旧记录的话，刷新后会看到空会话，
重填一次即可。

还有一个隐蔽的坑：生成成功后必须把 `viewState` 切回 `review-*`。漏掉的后果很难看出来，
因为产物状态是派生的（`statusFor` 只看内容、失败、通过），界面上照样显示"待审核"；
而 `VIEW_TO_STEP` 又把 `generating-*` 与 `review-*` 映到同一格，步骤条也看不出差别。
但盘上留下的是 `generating-*`，下次刷新会被降级并误报"上次生成被打断"。
把它抓出来的是"落盘的 `viewState` 是什么"这条断言，只断言派生状态的测试抓不到。

持久化实测 45/45：`loadSession` 的三种降级（`generating-prd/api-docs/prompts` 映射到对应审核页
并报出被打断的那一份）、六种非生成中状态原样保留、非法或缺失 `viewState` 的兼容回落、
字段清洗（非法消息丢弃、`roundIndex` 回落、`approved` 只认 `true`、空或全空白产物丢弃）、
缺 `sessionId` 或空 `form` 返回 `null`、会话只占一把键且仍套信封、`clearSession` 清干净。
真实 App 侧覆盖：未开始会话时不落盘，状态变化后落盘（含当时的 `viewState`），生成完成后盘上是
审核页而不是生成中，刷新回到审核页且内容恢复并带提示，孤儿生成态降级，清空重来两把键都没了。
这一轮产物端点用的是罐装 SSE（传输层已单独真实验证）。另外 Chrome 的 profile 目录跨轮持久化
localStorage，探针开头必须清，否则上一轮留下的键会污染"只占一把键"这类断言（实测踩到）。

端到端实测（真后端加真模型 `deepseek-chat` 加 Chromium）46/46 断言全过：表单渲染、必答未填被拦下、
填 7 题提交、切对话页且步骤条走到第 2 格、流式光标出现且内容逐段增长（不是一次性吐出）、
界面不出现原始 JSON 也不出现字面量 `\n`、会话写入且形状正确（`form_version`、表单快照、
无 UI 字段、无派生字段）、真实键盘发消息、乐观渲染用户消息、第二轮流式、共 3 条且角色为
`ai→user→ai`、显示「第 2/4 轮」、刷新后回到离开的那一屏并恢复内容、两条失败分支、
失败轮次不污染本地历史、清空重来清空本地并回表单页，全程无 JS 报错。
其中失败分支是用注入的 fetch 触发的（一个直接 reject，一个流到一半发 `error` 帧），
走的仍是真实的 `readStream` 解析路径，但不是真的掐掉后端进程。

另一轮聚焦实测 24/24：真后端加真模型跑通"首轮 → 接续 → 共 3 条"，再用一个只发一帧就永不关闭的
假流模拟"流还在推"，此时点清空重来触发 abort，断言回到表单页、不弹错误提示（被掐掉的不算失败）、
本地无残留、表单没被卡住、无未捕获报错；随后重开一轮，断言只有 1 条消息且显示的是自己的正文，
也就是被打断那轮的半截没有泄漏进新会话。

### 对话组件

`MessageList` 与 `ChatInput` 都是纯展示加回调：不认识接口、不持有会话状态（会话状态归服务端）。
`ChatInput` 的 `onSend` 返回 Promise 时，失败会把内容还回输入框，不让用户重打一遍。

四个刻意的取舍：

1. 自动滚动只在用户本来就贴底时才跟随（阈值 48px）。一轮流式输出会刷几百次，无条件跟随会把
   正在往上翻历史的用户反复拽回底部；不跟随时右下角给一个「最新」按钮。滚动用
   `scrollTop = scrollHeight` 瞬时赋值，不用 `scrollIntoView`，后者会反复重启平滑动画，画面一直在抖。
2. 输入法组合中的 Enter 不发送：判断 `e.nativeEvent.isComposing`，另加一道 `keyCode === 229` 兜底。
   不判断的话，用输入法打中文时每确认一个候选词就把半句话发出去。
3. 用户消息按纯文本渲染（`whitespace-pre-wrap`），不解析 Markdown。用户随手打的 `*` 和 `#`
   不该变成标题，只有 AI 那边走 Markdown。
4. `system` 是防御性过滤，不是功能。设计里 `role` 只有 `user` 与 `ai`，后端 `_serialize_messages`
   已经丢掉了 `SystemMessage`；`MessageList` 只是保证将来服务端多出任何未知角色都不会被当成正文渲染。

样式刻意不引 `@tailwindcss/typography`。一个 `prose` 类确实能省掉 `components` 映射，
但那是纯为样式而加的依赖，主题色还得再覆盖一遍；现在用 `components` 映射直接对齐本站的
`slate` 与 `primary` 调色板。

浏览器实测 51/51：GFM 表格、代码块、引用、链接渲染、`system` 被过滤、右侧对齐与 `pre-wrap`、
超一屏自动贴底、上翻后不被拽回且出现「最新」按钮、Enter 发送并清空、Shift+Enter 不发送、
组合态 Enter 不发送、高度自适应（含缩回与 200px 封顶）、`disabled` 全链路、`onSend` 抛错回填，
控制台无报错。另有 3 条真实键盘事件（`Input.dispatchKeyEvent`）：Enter 发送、
Shift+Enter 插入换行且不发送、用 `Input.imeSetComposition` 造出真实组合态后 Enter 不发送
（实测 `isComposing === true`）。

两处需要说明的：造组合态时实测 `keyCode` 是 13 而不是 229，所以 `keyCode === 229` 那道兜底
没有被这次验证触发（真正拦住的是 `isComposing`），它是照 Windows 输入法常见行为加的保险，
代价为零，但请知悉它未经实测覆盖。另外用 `compositionstart` DOM 事件造不出组合态，
Chrome 的输入法状态来自系统 IME，不看 DOM 事件（实测合成 `compositionstart` 后 `isComposing`
仍是 `false`，消息照发），要复现必须用 CDP 的 `Input.imeSetComposition`。

### 产物审核面板

`DocumentReview` 接收 `title`、`content`、`streamingContent`、`status`、回调
（`onContentChange`、`onOptimize`、`onCopy`）与底部 `actions`。它不调接口、不决定通过或打回、
不持久化，那些都是状态变更，只能走 `POST /sessions/{id}/events`，语义由调用方定，
组件只给按钮位置。

按 `status` 呈现：`not_started` 是只读空态「还没有生成这份文档」；`generating` 是只读 Markdown
预览加流式光标，优化面板隐藏、底部按钮全部禁用；`pending_review` 与 `stale` 是可编辑 textarea；
`approved` 只读并提示「已通过审核，如需修改请先打回」；`failed` 只读并显示 `failureReason`，
说明"下面是已收到的部分"。

四个刻意做的设计点：

1. 生成中就渲染 Markdown，不摆原始文本。产物是 Markdown，生成中把 `## 第 5 章` 这样的原始记号
   摆出来，与对话侧那个"直接渲染原始 JSON"的坑（`HANDOFF.md` §4 坑 #15）是同一类错误。
   代价是标题会逐字符跳动（`##` 加半个标题时已按标题排版），取舍是宁愿跳也要看得懂。
2. `approved` 不可编辑。产物已通过审核，就地改会让"已通过"与实际内容脱节；要改必须先打回，
   那是一次状态变更。
3. AI 优化会把选中的那一节的现有正文一起发过去（`currentContent`），所以它不会覆盖用户手改的内容，
   也因此这里不需要 F4.12 的"重生成前二次确认"。那个确认属于整份重新生成，由调用方通过
   `actions` 提供。
4. `edited`（「已手改」徽标）靠回显识别算出来，不是拿 `draft` 和 `content` 比。踩过的坑是：
   调用方按推荐做法把 `onContentChange` 的值写回 `content` 后，`draft === content` 恒成立，
   徽标永远不出现（实测抓到的）。所以组件记住自己最后报出去的值，下一次 `content` 等于它就判定为
   回显、不动基线。权威的 `edited_by_user` 在服务端（`状态数据设计` §2.3 的 `DocumentView`），
   这里只是 UI 提示。

`splitSections(markdown)` 把正文拆成"标题加正文"的各节，用来填优化的节选择器。
它必须跳过代码块，因为产物里大量出现 ```bash，而 bash 的注释就是 `#`：

```bash
# 安装依赖      ← 不跳过围栏的话，这一行会被当成一级标题，切出一个假小节
npm install
```

实测这类文档里 `#` 注释很常见，所以围栏配对是必需的，不是洁癖。字数统计按非空白字符算，
与 `validate_prompts.py` 的检查口径一致，两边口径不同的话，界面上显示"没超"而校验脚本说"超了"。

浏览器实测 58/58：六种状态各自的呈现、编辑回调与「已手改」徽标、字数联动、一键复制
（剪贴板内容与编辑器一致）、优化面板的节列表与忙碌态、底部按钮点击与禁用、
面板不被内层撑破也不与兄弟元素重叠，以及抽出共用组件后 MessageList 的 Markdown 回归。
两处实测记录下来的事实，别当成 bug 去修：复制出去的换行在 Windows 上会变成 CRLF
（写进去是 `\n`，`readText()` 回来是 `\r\n`，这是 Windows 剪贴板约定，这一层改不了，
校验时按行尾归一化再比）；`navigator.clipboard` 在 headless 里默认被拒，
必须用 CDP 的 `Browser.grantPermissions` 显式授权，否则测到的是退化路径而不是主路径。

### 视图状态

`ViewState` 决定渲染哪一屏：`form` 是表单，`chatting` 是对话页，其余是占位页（每页标了设计出处）。

它不是会话状态的权威来源，也绝不能进 localStorage。设计明确要求"服务端 `state` 是真相，
前端不自己实现状态机，按钮显隐由服务端下发的 `actions` 决定"（`会话持久化方案` §7.3、
`状态数据设计` §4.3），§7.3 还专门警告本地记"我发起过生成"只会在服务端已经完成时让前端错误地
显示生成中。所以刷新后的正确恢复路径是"读会话指针，再 `GET /sessions/{id}`，按 `snapshot.state`
渲染"，而不是读本地视图状态；会话接口就绪后 `viewState` 要改成由快照推导。

与设计有两处差额（`types/index.ts` 里有详细注释）：缺 `generation-failed`，
`会话持久化方案` §7.4 要求显示「生成已中断 + 重试」，现在没地方放；缺 `abandoned`，
表示会话被回收或用户放弃。另外 `STEPS` 的 5 步是设计里九步主流程（`SessionSnapshot.step`）的聚合，
接上服务端后要决定用哪一套。

### 表单草稿

设计上表单的主存储是服务端（前端防抖 800ms 写 `PUT /sessions/{id}/form`），localStorage 只是
同步失败时的降级兜底（`会话持久化方案` §5.1）。会话接口目前是 501，所以本地草稿暂时是唯一的
"不丢输入"手段；代码已按规范实现，等接口就绪后它自然降级为兜底，不用重写。

约定与实现：key 命名按 `harnessprd:{env}:{schema}:{kind}`，实际是
`harnessprd:development:1:draft`；值套统一信封 `{schema, env, saved_at, payload}`，
由 `storage.ts` 自动套；schema 或 env 不符就直接丢弃，读不到即当没有并顺手清 key；
`saved_at` 超过 7 天清除；不做逐键写入（同步 API 阻塞主线程），防抖 800ms 写一次。

提交后没有删草稿。设计 §5.4 要求"提交成功后立即删除"，但那指的是服务端提交成功；
现在没有服务端可提交，删了就等于丢用户输入。

空表单会清掉草稿，而不是写一个空对象。写空对象的话 key 会留下来，清空重来之后本地还挂着一条记录，
看起来像没清干净（虽然读回来确实当没有草稿），顺带也不会在首次加载时凭空造一个空草稿 key。
这条是实测抓出来的。

类型是手写镜像，字段名以实际接口为准。最典型的一处：题目的主键是 `id` 而不是 `name`，
后端 `questions_config.json` 就叫 `id`。写错不会报编译错误（TS 不校验运行时数据），
只会拿到 `undefined`、表单静默渲染不出来。改接口后请对照 `backend/core/questions.py` 与
`backend/api/schemas.py` 同步。

## 跑起来

前后端各开一个终端：

```powershell
# 后端（仓库根目录）
cd backend
.venv\Scripts\python -m uvicorn main:app --port 8000

# 前端
cd frontend
pnpm install
pnpm dev            # http://localhost:5173
```

`vite.config.ts` 里配了 `/api` 到 `http://127.0.0.1:8000` 的代理，所以页面里只写相对路径
（如 `axios.get('/api/v1/conversation/questions')`），不用硬编码后端地址，也不涉及跨域。

端口固定 5173（开了 `strictPort`）：5173 被占用时 dev server 直接失败，而不是静默改用 5174。
换了端口后端 CORS 白名单就对不上，而报错只会出现在浏览器控制台，容易查错方向。

代理链路已实测：`:5173/api/v1/conversation/questions` 返回 200（版本 1.2、20 题）；
`:5173/api/v1/nope` 返回 404 且是后端答的，证明真转发了；`:5173/其他路径` 返回 200，
是 Vite 自己的 history 兜底，未走代理。

| 命令 | 作用 |
| --- | --- |
| `pnpm dev` | 开发服务器（5173，带热更新） |
| `pnpm build` | `check:text` 加类型检查加生产构建 |
| `pnpm typecheck` | 只做类型检查 |
| `pnpm check:text` | 只跑"纯文本里没有 Markdown 记号"这道离线守卫 |
| `pnpm preview` | 预览构建产物 |

## 依赖与版本

| 包 | 版本 | 备注 |
| --- | --- | --- |
| react / react-dom | 19.3.0 | |
| typescript | 7.0.2 | |
| vite | 8.3.0 | 底层已是 rolldown |
| tailwindcss | 3.4.19 | 刻意固定 v3，见下 |
| postcss / autoprefixer | 8.5.28 / 10.6.1 | v3 工作流需要 |
| lucide-react | 1.47.0 | 图标 |
| react-markdown | 10.1.0 | 对话正文与文档预览 |
| remark-gfm | 4.0.1 | 必需，没有它 `\| 表格 \|` 这种语法根本不会被解析 |
| axios | 1.20.0 | |

### 为什么 Tailwind 固定在 v3

需求指定的是 `tailwind.config.js` 加 `postcss` 加 `autoprefixer` 加 `@tailwind` 三条指令，
这套是 v3 工作流。Tailwind v4（最新 4.3.3）改成了 CSS 优先：主题配置从 `tailwind.config.js`
的 `theme.extend` 改成 `src/index.css` 里的 `@theme`；引入方式从
`@tailwind base/components/utilities` 改成 `@import "tailwindcss"`；postcss 与 autoprefixer
不再需要，v4 内置。要迁 v4 就是改这三处。

### 主色

`primary` 就是 Tailwind 内置的 sky 色系（50 到 950 全档），在 `tailwind.config.js` 里用
`colors.sky` 引用而不是抄十六进制，抄一份就多一处要同步的地方。用法如 `bg-primary-600`、
`text-primary-700`、`border-primary-200`。

必须写完整类名，不能拼 `bg-primary-${shade}`。Tailwind 是扫描源码里的完整类名生成 CSS 的，
拼接出来的它扫不到，样式会静默消失。`src/components/MessageList.tsx` 的 `MARKDOWN_COMPONENTS`
和 `src/components/StepProgress.tsx` 里的每一档都是字面量，就是为了这一点。

另一个相关陷阱是：构建成功不等于样式生效。没被任何模块 import 的文件根本不在 Vite 的模块图里，
`pnpm build` 通过证明不了它的类名被生成了（Tailwind 按 `content` 扫源码，与模块图无关）。
验证办法是直接查产物 CSS：`Select-String -Path dist/assets/*.css -Pattern 'min-h-9'`。

## 沙箱与受限环境

在禁命名管道的沙箱里会踩到几类问题，都不是代码问题：

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| `pnpm create vite` 报 `ERR_PNPM_CLI_DLX_CACHE`（`os error 5`） | dlx 缓存写在 `%LOCALAPPDATA%\pnpm-cache`，工作区外被拒 | 别用 `pnpm create`，直接 `pnpm add`（store 在工作区内，可用） |
| `pnpm build` 或 `pnpm dev` 报 `spawn EPERM` | Vite 在 Windows 上 `exec('net use')` 探测盘符映射 | 提权运行，或在正常终端里跑 |
| Chromium 报 `platform_channel.cc: Check failed: 拒绝访问`，或启动后立刻退出、只剩一行 crashpad `OpenProcess: 拒绝访问` | Chromium 的 Mojo IPC 在 Windows 上走命名管道 | 同上 |
| 起了 Chromium 之后 `pnpm dev` 自己崩了，报 `EBUSY ... watch '…\Default\Network\Cookies'` | Vite 的文件监听器会 watch `frontend/` 下的每个文件，而 Chromium 把 profile 目录建在了里面、`Cookies` 被独占占用 | profile 与 `--user-data-dir` 一律放到被监听目录之外，比如仓库根的临时目录 |
| Chromium 建的 profile 目录删不掉，报 `Access is denied`，连 `Get-Acl` 都读不了 | Chromium 给 user-data-dir 加了受限 DACL，沙箱的受限令牌不在授权列表里 | 用更宽的执行权限删。另外 `TerminateProcess` 不带走 `crashpad_handler` 子进程，要先清掉它（`taskkill /T` 或 `Stop-Process`），否则它占着目录 |

## 还没做

这一节按 `docs/技术栈.md` §2.2 的硬性要求列。

已经完成的：20 题表单动态渲染（`src/components/FormStep.tsx`，题目由服务端下发）；
把 `MessageList` 与 `ChatInput` 挂进 `chatting` 视图（`App.tsx` 的 `handleStartConversation`
与 `handleSendMessage`，46/46 端到端断言）。

还没做的：

- 表单提交接服务端。现在只标记"已提交"并留本地草稿，应改为发 `POST /api/v1/sessions/{id}/events`
  的 `submit_form` 事件，成功后再删本地草稿。
- 对话界面每问旁边要有「采纳建议」按钮（`suggested_answer`，设计里的一等公民）。
  数据已经收得到了（后端返回里就有 `questions[].suggested_answer`），缺的是决定"等整个 JSON
  收完再解析"还是"边流边增量解析"（`HANDOFF.md` §6 第 7 项）。
- 本地会话换服务端会话：`loadSession()` 换成 `GET /sessions/{data.sessionId}`，`viewState`
  改由 `snapshot.state` 推导（现在按本地存的 `viewState` 恢复，并降级掉 `generating-*`，
  那一步仍是本地近似）。
- 服务端状态机推进 `stage` 与轮次。现在是前端自己数轮次并在 `MAX_ROUNDS` 封顶，属于权宜之计
  （`对话阶段设计` §8 第 5 项）。
- 产物页改成会话形状：「通过」现在是本地推进，真实实现要发 `approve` 事件
  （`POST /sessions/{id}/events`，仍是 501）；`viewState` 也要改由 `snapshot.state` 推导。
- 提示词套件的 `=== FILE: ===` 切分。产出是多文件的，但本层不解析，目前也没有切分工具。
- 生成该是后台任务。现在四个接口都是前台流式，请求断了这一轮就没了（`会话持久化方案` §7.1），
  产物落库要先有任务层。
- `known_info` 的结构化合并（`apply_event`）。现在只能用用户原话顶替，生成质量因此受限（坑 #4）。
- 三份产物各自的独立预览页。Markdown 预览本身已支持（`Markdown.tsx` 加 `remark-gfm`），
  但 `DocumentReview` 是可编辑的审核面板，不是只读预览页。
- 轮询：生成中 2 秒一次、对话等待 1 秒一次，用 `document.visibilityState` 在后台暂停。
- 多标签页：比对 `revision`，写操作带 `expected_revision`，409 时提示并让人手动刷新，不做自动合并。
- ESLint 与 Prettier 配置（全局装了，项目内还没配）；Vitest 加 Playwright。
  括号里的 `splitSections`、`countContentChars`、`validateForm` 这些纯函数很适合先补 Vitest 单测，
  现在只能靠浏览器探针验，成本高得多。
