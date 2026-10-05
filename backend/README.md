# harnessprd backend

面向产品经理的文档生成工具后端（FastAPI + LangChain）。

> **接手开发请先读仓库根目录的 [`../HANDOFF.md`](../HANDOFF.md)。**
> 那份文档说明设计现状、哪些结论已验证过、哪些还需要拍板。
> 本 README 只讲后端骨架怎么跑、坑在哪。

## 当前状态

设计阶段已完成（见 [`../docs/`](../docs/)），**业务代码部分实现**。

- **已实现**：对话侧 3 条接口（题目下发 + 首轮/接续流式对话）、澄清阶段编排、
  三份产物的生成与修订（`services/document_service.py`，`astream` 流式）、
  会话快照存储（`/api/session/*`，SQLite）、
  **生成任务（Generation Job，`/api/jobs/*`）—— 生成不再绑在 SSE 连接上**
- **未实现**：会话状态机（`SessionStore` / `apply_event`）、
  M8 产物审核与质检、数据层接入 —— 所以 `/sessions/*` 那 8 条仍是 501
- `main.py` / `api/` 的分层外壳是**设计之前就有的骨架**，但已按新设计改造过
  （旧接口面按 HANDOFF §7 删除），现在可以当作实现基础 —— 见 HANDOFF.md §0
- 旧的「一句话需求 → 固定八章 PRD」已连同提示词一并删除：`system_prd.md`、`clarify.md`、
  `services/prd_service.py`、`api/prd.py`、`scripts/preview.py` —— 见 HANDOFF.md §7

## 目录结构

```
backend/
├── main.py                 应用入口（create_app 工厂 + uvicorn 启动）
├── requirements.txt        运行依赖
├── .env.example            环境变量模板（复制为 .env）
├── api/                    路由层：只做参数解析与响应组装，**不写业务逻辑**
│   ├── router.py           路由聚合（挂到 /api/v1）
│   ├── schemas.py          请求/响应模型（对外契约；枚举复用 services.state）
│   ├── health.py           GET  /health、/api/v1/health
│   ├── sessions.py         /sessions：创建/列表/快照/表单草稿/事件/文档/对话历史（占位，501）
│   ├── conversation.py     /conversation：题目下发 + 首轮/接续对话 + 产物生成/修订（SSE；**generate-* 已弃用**）
│   ├── jobs.py             /api/jobs：创建任务 + 查快照 + 订阅进度（SSE，**已实现**）
│   ├── config.py           运行配置接口（占位，501）
│   └── deps.py             依赖注入（Settings / Conversation / Document / Session / Job）
├── core/                   配置与静态资源
│   ├── config.py           环境变量唯一入口（pydantic-settings）
│   ├── logging.py          日志配置
│   ├── questions.py        表单题目配置的加载与形状（/conversation/questions 的数据源）
│   ├── questions_config.json        20 题表单定义（唯一来源）
│   ├── form_submission.schema.json  由脚本生成，**勿手改**
│   └── prompts/            11 份可用提示词 + load_prompt/available_prompts
├── services/               业务逻辑：不感知 HTTP、不 import `api.*`
│   ├── llm.py              LangChain ChatModel 工厂（**唯一实现**）
│   ├── llm_factory.py      `get_llm()` 命名门面（转调 llm.py，不复制实现）
│   ├── prompts.py          提示词**命名入口**（只有名字，正文在 core/prompts/*.md）
│   ├── state.py            会话状态的**内部**契约（枚举）
│   ├── conversation_service.py  澄清阶段对话编排（`astream` 流式；**缺口见模块 docstring**）
│   ├── document_service.py      三份产物的**生成 / 优化**（已实现）+ 审核/质检（占位）
│   ├── document_plan.py         **分片计划**：从提示词文件解析章节/文件清单并分批（不调模型）
│   ├── stitch.py                分片拼接（任务与 split_check 共用一份实现）
│   ├── plan_models.py / plan_repository.py / plan_service.py   方案表（plans）与快照存储
│   ├── session_models.py / session_service.py                  会话层：降级规则 + 任务同步
│   ├── job_models.py            Generation Job 的枚举 / 映射 / payload 白名单
│   ├── job_repository.py        generation_jobs 表的 SQL
│   ├── job_service.py           任务 CRUD + 同产物唯一 / payload 过滤 / 草稿节流
│   ├── job_bus.py               同进程订阅广播（**退订不影响任务**）
│   └── job_runner.py            后台 runner：复用生成器，写库 + 发事件 + 同步会话
├── scripts/
│   ├── smoke_check.py      离线自检：配置 / 提示词 / 模型构造 / 路由 / 分片计划
│   ├── http_check.py       对着运行中的服务打真实 HTTP
│   ├── gen_form_schema.py  从 questions_config.json 生成 form_submission.schema.json
│   ├── validate_prompts.py 真实 LLM 验证提示词（会花钱；`--recheck` 免费）
│   ├── e2e_flow.py         全流程端到端验收（真实浏览器 + 真实模型；`all` 一次跑完）
│   ├── split_check.py      分片生成验收（真实模型 6 次调用；证明不再被截断）
│   └── job_check.py        Generation Job 验收（默认离线假模型；`--live` 真模型三种产物）
├── validation_out/         验证留档（17 个文件：11 份分片留档 + 3 份端到端 + 2 份分片结果 + README）
└── validation_out_deepseek-flash/  换模型实验证据（7 份，见 HANDOFF §4 坑 #14）
```

分层规则：`api → services → core`，单向依赖。`services` 不 import `fastapi`，也**不 import `api.*`**
（枚举定义在 `services/state.py`，由 `api/schemas.py` 反向复用 —— 这是允许的方向，避免两边各写一份）。

## 提示词：怎么组装、怎么渲染

组装与渲染都在 `core/prompts/__init__.py`，**运行时与验证脚本共用同一份实现**
（脚本里不再自带 `render()`）：

| 函数 | 作用 |
| --- | --- |
| `build_system_prompt("clarify_s2", values)` | 按 `{族}_common + {名}` 组装并渲染，顺序固定不可颠倒 |
| `render_prompt(name, values)` | 单文件渲染（`str.replace`） |
| `declared_placeholders(name)` | 从提示词的「占位符清单」表格解析出声明的占位符名 |
| `load_prompt` / `available_prompts` | 读取与枚举 |

两条硬规则（HANDOFF.md §4 坑 #1、#11）：

1. **渲染必须用 `str.replace`，不能用 `str.format`。** 提示词里既有运行时占位符
   （`{form_summary}`），也有给人看的格式记号（`{id}`、`{模块缩写}{3位序号}`）；
   `str.format` 会把后者也当占位符，实测在 `gen_api.md` 上抛 `KeyError`。
2. **`str.replace` 对没传进来的键是静默跳过、不报错。** 所以每次验证都会打印
   「未注入已声明的占位符」。**这个缺口已经在生成侧补上了**：
   `document_service` 把 `{doc_outline}`、`{generate_scope}`、`{scope_spec}`
   （`gen_common.md` 标为「全部」适用）连同其余声明项一次性全部注入，并用
   `missing_placeholders()` 兜底（漏了会打 warning）。
   ⚠️ `validate_prompts.py` **仍然**不注入这三个 —— 那 7 份留档是在"占位符以字面量
   留在 prompt 里"的条件下产出的，所以留档**不等于**对 `document_service` 的验证。
   要重跑真实验证时，应改走服务层（见 `document_service` 模块 docstring）。

> 已删除的死代码：`validate_prompts.py` 私有的 `render()`（与 core 重复，正是漏注入
> 能长期潜伏的原因）与 `build_outline()`（为注入 `{doc_outline}` 而写，但从未被调用）。

## 安装与启动

```powershell
cd backend
uv venv .venv
uv pip install --python .venv\Scripts\python.exe -r requirements.txt

Copy-Item .env.example .env      # 然后填入 DEEPSEEK_API_KEY
.venv\Scripts\python -m uvicorn main:app --reload
```

**⚠️ 所有业务接口都在 `/api/v1/` 下** —— 这是刻意保留的版本段，手输 URL 时最容易漏：

| 用途 | 地址 |
| --- | --- |
| 接口文档（OpenAPI） | http://127.0.0.1:8000/docs |
| 健康检查（探针用，**不带版本段**） | http://127.0.0.1:8000/health |
| 表单题目定义（**已实现**） | http://127.0.0.1:8000/api/v1/conversation/questions |

```powershell
# 起来之后的三条冒烟
curl.exe http://127.0.0.1:8000/health
curl.exe http://127.0.0.1:8000/api/v1/health
curl.exe http://127.0.0.1:8000/api/v1/conversation/questions
```

> 完整清单见下面「接口」表，或直接看 `/docs`。**只有 `/health` 不带版本段** ——
> 它给负载均衡 / 探针用，路径越稳定越好。

> **改了 `.env` 必须重启服务。** 配置在进程启动时读取并被缓存，
> 且 `--reload` 只监听 `.py` 文件的变化——只改 `.env` 不会触发热重载。
> 判断是否生效看启动日志里的 `key_configured=True/False`。

在仓库根目录下启动（无需 cd）：

```powershell
backend\.venv\Scripts\python -m uvicorn main:app --app-dir backend --reload
```

> 沙箱/受限环境下 uv 默认把缓存写到 `%LOCALAPPDATA%\uv\cache`，可能因无权限而失败（`os error 5`）。
> 把缓存挪进项目即可，不影响功能：
>
> ```powershell
> $env:UV_CACHE_DIR = "$PWD\.uv-cache"   # 该目录已在 .gitignore 中
> uv venv backend\.venv
> ```
>
> 同样地，**受限环境里 `--reload` 可能起不来**：Windows 上 reloader 靠 `multiprocessing`
> 建命名管道与 worker 通信，禁命名管道的沙箱会报 `PermissionError: [WinError 5]`，
> 表现为服务端刷一屏 traceback、客户端拿到 `HTTP 000`。
> **去掉 `--reload` 即可**（功能不受影响，只是没有热重载；改 `.env` 本来也需要重启）。

## 自检与验证

```powershell
cd backend
$env:PYTHONIOENCODING = 'utf-8'

# 离线：不联网、不需要 Key
.venv\Scripts\python scripts\smoke_check.py

# HTTP：另开一个终端起服务后再跑
.venv\Scripts\python -m uvicorn main:app --port 8000
.venv\Scripts\python scripts\http_check.py http://127.0.0.1:8000

# 提示词真实 LLM 验证（会花钱）
.venv\Scripts\python scripts\validate_prompts.py --recheck      # 免费，复查历史输出
.venv\Scripts\python scripts\validate_prompts.py                # 7 次真实调用

# 改了题目配置后：重新生成 schema
.venv\Scripts\python scripts\gen_form_schema.py

# 全流程端到端（真实浏览器 + 真实模型）：需要前端 Vite 也起着（默认 5173）
.venv\Scripts\python scripts\e2e_flow.py all
```

`http_check.py` 断言的是路由与状态码（含「旧形状路由已 404」、8 个占位接口返回 501、
入参校验返回 422），**不做 LLM 调用**；提示词层的真实 LLM 验证由 `validate_prompts.py` 承担；
**整条产品链路**（表单 → 对话 → 三份产物 → 通过 → 下载）由 `e2e_flow.py` 承担 ——
它驱动的是真实浏览器，所以能覆盖只有前端才会出错的地方（落盘、降级、下载、截断提示）。
⚠️ Chromium 在受限沙箱下会因命名管道被拒而起不来，见该脚本 docstring。

## 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/` | 服务自述（名称 / 版本 / 文档入口） |
| GET | `/health` | 健康检查，探针用的稳定路径（不带版本号） |
| GET | `/api/v1/health` | 同上内容，版本化路径 |
| GET | `/api/v1/conversation/questions` | **已实现** — 表单题目定义（20 题 + `version`，前端渲染用） |
| GET | `/api/v1/conversation/document-plan` | **已实现** — **分片计划**（`?kind=prd\|api\|prompts`）。**不调模型**，解析提示词文件得出；前端据此逐片生成再拼接。整份接口文档必被输出上限截断，所以这份计划是产物完整性的前提（HANDOFF §4 坑 #20） |
| POST | `/api/v1/conversation/start-stream` | **已实现** — 首轮流式对话（SSE） |
| POST | `/api/v1/conversation/continue-stream` | **已实现** — 接续流式对话（SSE） |
| POST | `/api/v1/conversation/generate-prd-stream` | **已弃用** — 生成 PRD（前台流式）。正式入口改走 `/api/jobs`，见下方「Generation Job」 |
| POST | `/api/v1/conversation/generate-prd-from-summary-stream` | **已弃用** — 从摘要生成 PRD（同上；回归脚本仍在用它的裸文本契约） |
| POST | `/api/v1/conversation/generate-api-docs-stream` | **已弃用** — 从 PRD 生成接口文档（同上） |
| POST | `/api/v1/conversation/generate-prompts-stream` | **已弃用** — 从 PRD 生成提示词套件（同上） |
| POST | `/api/v1/conversation/optimize-document-stream` | **已实现** — 按反馈修订文档某一节（SSE，F8.6）；**仍是前台流式**，优化任务见后续篇 |
| POST | `/api/jobs` | **已实现** — 创建生成任务（**立刻返回 `job_id`**，不等生成） |
| GET | `/api/jobs/{job_id}` | **已实现** — 任务快照（刷新时先调它，页面不空白） |
| GET | `/api/jobs/{job_id}/stream` | **已实现** — 订阅任务进度（SSE，GET 无 body；断开不影响任务） |
| POST | `/api/v1/sessions` | **占位** — 创建会话 |
| GET | `/api/v1/sessions` | **占位** — 会话列表（`states` / `limit` / `cursor`） |
| GET | `/api/v1/sessions/{id}` | **占位** — 会话快照（含 `actions`） |
| PUT | `/api/v1/sessions/{id}/form` | **占位** — 表单草稿（前端防抖 800ms 自动保存） |
| POST | `/api/v1/sessions/{id}/events` | **占位** — **所有状态变更的唯一入口** |
| GET | `/api/v1/sessions/{id}/documents/{kind}` | **占位** — 文档正文（`kind` ∈ `prd` / `api` / `prompts`） |
| GET | `/api/v1/sessions/{id}/messages` | **占位** — 对话历史（单独分页取，不进快照） |
| GET | `/api/v1/config` | **占位** — 运行配置 |

命名空间分工（路径形状取自 `docs/会话持久化方案.md`）：

- `/sessions/*` —— 会话生命周期与状态推进，含**唯一变更入口** `events`
- `/conversation/*` —— 对话阶段自身：题目下发、流式回复，以及**产物的流式生成 / 修订**

**刻意没有"发一条消息"的写接口**：一轮对话就是一个 `reply` 事件，走 `/sessions/{id}/events`。
设计对状态变更只留一条入口，多开一个写接口就多一条绕过转换表与 guard 的路径。
契约细节见 `api/schemas.py`（对外）与 `docs/状态数据设计.md` §4.3（出处）。

> ⚠️ **`/conversation/*` 的 7 个 POST 都是「无状态补全」，不是状态变更**：它们不写会话、
> 不改 `state`、不落产物，所以现在不违规。代价是**客户端要自己带状态**
> （`form` / `known_info` / `prd_content`……），而设计要求「服务端 `state` 是真相」。
> 会话层落地后这些都要改形，见 `api/conversation.py` 的模块 docstring。
>
> ✅ **四个 `generate-*` 已被 `/api/jobs` 取代**（标了 `deprecated=True`）：它们是前台流式，
> 请求断了这一轮就没了。**生成任务才是产物的权威写入路径**，见下一节。

占位接口一律返回 **501 Not Implemented**，不返回 200 假数据——假数据会被误当成"已实现"。
`/health` 与 `/api/v1/health` 复用同一个 handler，不是两份实现。

### Generation Job：生成不再绑在 SSE 连接上

**为什么有这层**：`docs/会话持久化方案.md` §7.1 要求「生成不绑在 HTTP 请求上」。
前台流式那套的后果很具体：刷新页面 = 丢掉这一轮；关掉标签页 = 模型白烧；
`/conversation/*` 的 4 个生成接口因此**只能当预览通道**，产物落库没有归宿。

**现在**：`POST /api/jobs` 只登记任务并**立刻返回 `job_id`**，执行在一个后台 `asyncio.Task`
里跑（`services/job_runner.py`），状态与草稿落 `generation_jobs` 表，
进度通过进程内广播（`services/job_bus.py`）推给**当前订阅中**的客户端。

| 文件 | 职责 |
| --- | --- |
| `services/job_models.py` | 枚举（artifact / status / phase）、映射表、**冲突组**、payload 白名单、`JobRecord` |
| `services/job_repository.py` | `generation_jobs` 表的 SQL（与方案表同一个库文件，共用连接与 PRAGMA）+ **旧表迁移** |
| `services/job_service.py` | 任务 CRUD + 三条规则（冲突组只跑一个 / payload 过滤 / 草稿节流 500ms） |
| `services/job_bus.py` | 同进程订阅广播；**退订不碰任务** |
| `services/job_runner.py` | 复用 `document_service` 的生成器与优化器；写库、发事件、收尾同步会话 |
| `services/section_edit.py` | **按标题替换一节**（优化收尾把模型输出拼回整篇；TS 那份的镜像） |
| `api/jobs.py` | 3 条路由（创建 / 快照 / 订阅 SSE） |

**六种 artifact，两种动作**（`job_models.JOB_ARTIFACTS`）：

| 动作 | artifact | runner 调用 | 会话 `viewState` |
| --- | --- | --- | --- |
| 整篇生成 | `prd` / `api-docs` / `prompts` | `generate_prd_with_review_events` / `generate_api_docs_stream` / `generate_prompts_stream` | 跑时 `generating-*`，收尾 `review-*` |
| **按指令优化一节**（F8.6） | `optimize-prd` / `optimize-api-docs` / `optimize-prompts` | `optimize_document_stream` | **全程 `review-*`**（用户就停在审阅页） |

优化任务的 payload（**五项缺一不可**）：

```json
{ "doc_type": "prd", "section": "## 2. 功能需求",
  "current_content": "<该节的现有正文>", "document_content": "<优化前的整篇>",
  "instruction": "把功能需求写细一点", "context": { "prd_content": "…" } }
```

⚠️ 三处与生成任务不同、且都是踩过才知道的：

1. **`draft_content` 是"那一节"，不是整篇**。模型只回一节（`generate_scope` 被强制对齐到
   `section`），拼接在收尾做（`section_edit.replace_section` → `result_json.content` = 整篇）。
   所以 `GET /api/jobs/{id}` 与 SSE 首帧都带 `section` —— 客户端要靠它把这一节拼回整篇显示；
   少了它，界面上只有一节片段，用户会以为整篇被替换了（实测踩到）。
2. **失败时也不能把那一节直接写进产物的 `*Content`**：那是把整篇换成一段话（数据丢失）。
   能拼就拼回整篇（`_optimize_partial`），拼不了就一个字都不动。
3. **优化不写 `truncated`、不清 `prdReviewResult`**：前者说的是"整篇被截断过"，改一节不代表
   末尾补全了；后者是审查结论，没重新审稿就不能抹掉。

**冲突规则**（`job_models.ARTIFACT_CONFLICT_GROUP`）：**同一份文档**不能同时"整篇生成"与
"按指令优化"（两者写同一个 `documents.<kind>.content`，谁后写完谁赢）→ **409**；
**不同文档之间不冲突**（`prd` 在跑时接口文档照样能生成）。

**SSE 事件**（与 `/conversation/*` 那套**不是**同一套帧名，别混）：

```
event: snapshot     data: {status, phase, artifact, draft_content, review, section?, doc_type?}   ← 订阅建立时第一条
event: phase        data: {phase: writer_started|review_started|rewrite_started|done, round, issues}   ← 仅生成（PRD）
event: text_delta   data: {content}                                          ← 增量文字（优化时是"那一节"的增量）
event: review       data: {content: {passed, issues, review_model, review_skipped, round}}   ← 仅生成（PRD）
event: run_summary  data: {…耗时 / token / 调用次数…}                          ← 与 conversation 一致：在 done 之前
event: done         data: {artifact, content, final_prd?, review, revision_applied, truncated, finish_reason, section?}
event: error        data: {message, request_id}
data: [DONE]                                                                 ← 流结束
```

几条刻意的取舍：

- **断开订阅不 cancel 任务**。`unsubscribe` 只摘掉那一条队列，后台 `asyncio.Task` 与这条
  HTTP 请求没有任何关系。已实测：订阅到第 5 帧时主动断开，任务照样跑完（`job_check.py --live`）。
- **刷新后的正确顺序**：`GET /api/session/{id}` 拿 `activeJobId` → `GET /api/jobs/{id}` 填草稿
  → 订阅 `/stream`。所以快照接口必须在订阅**之前**就能给出全文草稿，页面才不会空白一下。
- **有任务真在跑时，`generating-*` 不再被降级**（`session_service.downgrade_session_data` 的
  `running_job_ids`）。降级的本意是"孤儿生成态"，而服务端有 `status=running` 的行时它是**活的** ——
  降级会让前端不去重连，用户把半截草稿当终稿。
- **同会话 + 同**一份文档**同时只允许一个 running 任务**（否则连点两次就是两个 runner 写同一份产物）。
  重复创建 → **409**，且 `pending` 也算"在跑"（创建与调度之间有个极短的窗口）。
  判重走的是**冲突组**而不是 artifact 字符串：`prd` 与 `optimize-prd` 写同一个字段，
  只比字符串会放它们一起进来（见上面的"冲突规则"）。
- **服务重启**：`asyncio.create_task` 活不过重启，所以启动时扫描 `pending`/`running` 一律标
  `failed` + `error="服务重启，任务中断"`（不自动续跑，草稿留在 `draft_content` 里给用户）。
- **表结构变更靠"检测 + 重建"**：SQLite 改不了 CHECK 约束，而 `CREATE TABLE IF NOT EXISTS`
  对已存在的表什么也不做。加了 optimize artifact 之后，旧库上插入优化任务会抛
  `CHECK constraint failed`（报错点离原因很远），所以 `job_repository.ensure_schema()` 会
  检测旧 CHECK → **复制数据**重建表（不 `DROP`，库里可能正躺着用户等了几分钟的任务）。
- **接口文档 / 提示词套件在任务里仍然分片**（`document_plan` + `services/stitch.py`）：
  实测整份必然被输出上限截断（HANDOFF §4 坑 #18/#20）。退回单次调用等于把已修好的 bug
  引回来，而这次是后台任务，用户拿到的是一份"看起来完整、其实缺了几章"的文档。
- **`done` 事件是 runner 组装的**，不是生成器给的：生成器正常返回 = 成功，
  抛异常 = 失败。生成器自己不产出"终稿"事件。

**验收**：

```powershell
cd backend
.venv\Scripts\python scripts\job_check.py             # 离线：66 项，假模型，不花钱
.venv\Scripts\python scripts\job_check.py --live      # 在线：3 种产物各一个真任务（会花钱）
.venv\Scripts\python scripts\job_check.py --live --only prompts   # 只重跑其中一种
```

离线段覆盖（除表结构 / 路由 / 广播外）：`section_edit.replace_section` 的五条边界
（同级替换、标题匹配不上原样返回、低一级标题补回原标题、代码围栏里的 `#` 不算标题、
其它章节不动）、冲突组（同文档互斥 + 跨文档放行）、优化 runner 的收尾（整篇拼接 /
`viewState` 留 `review-*` / 不动 `truncated` / 保留 `prdReviewResult` / snapshot 带 `section`）、
以及优化失败时"半成品那一节拼回整篇"。

在线段实测（deepseek-chat，本机）：PRD 1751 字符 / 审核通过；接口文档 3 片拼成
**27217 字符未截断**；提示词套件 3 片拼成 **19537 字符未截断**；三者都验到
"订阅断开后任务仍 completed"与"重连重放 `run_summary + done + [DONE]`"。
优化任务实测（同一台机器）：PRD 一节 2-4 秒、提示词一节 6 秒；刷新时后端日志能看到
`GET /api/session/{id}` → `GET /api/jobs/{id}` → `GET /api/jobs/{id}/stream` 的重连三连，
而任务在没有任何订阅者的情况下照常跑完。

### SSE 协议（两个 `*-stream` 端点）

产出**命名事件**，不是裸 `data: 文本`：

```
event: chunk
data: {"text": "我看了你的表单…"}

event: done
data: {"chunks": 792, "chars": 1426}

event: error
data: {"type": "TimeoutError", "message": "…"}
```

- **多行文本会被 JSON 转义成 `\n`**，所以一帧永远只有一行 `data:`（符合 SSE 帧格式）
- ⚠️ **POST + SSE 不能用 `EventSource`**（它只支持 GET），前端要用 `fetch` + `ReadableStream`
  手动解析。好处是随之没有"断线自动重连"，所以**不需要 `[DONE]` 哨兵**，`done` 事件即终止信号
- ⚠️ **缺 Key 之类在开流前就返回 503**，不会变成"200 + 流里一个 error 事件"
- ⚠️ 这两个端点是**无状态补全**，不是状态变更。会话层落地后要改成
  `POST /sessions/{id}/events` 写 + 单独的流端点读（见 `api/conversation.py` 的模块 docstring）

**带请求体的接口会先做入参校验**（非法 `kind`、缺 `event` 字段、缺 `session_id` 都返回 422 而不是 501）——
这是刻意的：校验真的接上了，才说明契约生效而不只是个空壳。

状态码约定（生成阶段接入后会用到后两条）：

- `501` —— 占位接口尚未实现
- `422` —— 入参不合法
- `503` —— 服务端未就绪（如未配置 API Key），不是调用方的错
- `502` —— 上游 LLM 调用失败（网络 / 超时 / 鉴权被拒）

### 观测：一次产物生成 = 一个 run

**为什么有这层**：接口文档与提示词套件是**分片生成**的（各 3 片，`GET /api/v1/conversation/document-plan`），
前端按计划**逐片发请求** —— 一份产物 = 3 次 SSE 请求。而 `run_summary` 原本是"每请求一份"，
于是界面上显示的耗时 / token / 调用次数**只有最后一片**（提示词套件最后一片只有 1 个文件，
用户等了两分钟看到 `12s`），并且 `request_id` 只覆盖 1/3 —— 维护者按它检索日志，只能复盘三分之一。

**契约**（请求头都可选，只有产物生成需要带；澄清聊天不带）：

| 头 | 含义 |
| --- | --- |
| `X-Run-ID` | 一次产物生成 = 一个 run。同一次生成的**每一片都带同一个值**（前端 `newRunId()` 生成） |
| `X-Part-Index` / `X-Part-Total` | 1 基的分片序号与总片数（取自分片计划）。缺席按 `1/1` 单片处理 |
| 响应头 `X-Run-ID` | 原样回显 |

`run_summary`（仍在 `done` **之前**发；失败时 `error → run_summary`）在原有字段上增加：
`run_id`、`request_ids[]`、`part_index` / `part_total` / `parts_seen` / `complete`、
`part_duration_ms`（本片）、`total_duration_ms`（**整份墙钟**）、tokens 与 `llm_call_count`（整份求和）、
`steps[]`（各片按序拼接）。`done` 里的 `context_usage` 保持**本片**语义 —— 上下文占用本来就是
"这一片这次调用的输入大小"，合并它没有意义。

几条刻意的取舍：

- **合并按 `request_id` 幂等**：同一片重放（重试 / 断线重连）是**替换**不是累加 —— 累加会让 token 翻倍，
  那种数字比没有数字更糟（看着像"模型多花了钱"）。
- **墙钟取水位跨度**（见过的最早开始 → 最晚结束，只增不减），**不是各片相加**（相加会把片与片之间的
  等待重复计一遍），也不是"从现存各片现算"（重放会覆盖那片的原始区间，跨度会**缩短**）。
  计时用 `time.perf_counter()`，**不是 `time.monotonic()`** —— Windows 上后者是 `GetTickCount64`，
  粒度 15.6ms，几毫秒的请求起止会落在同一刻度上，窗口退化成 0（实测踩到）。
- **store 只存统计，不存正文**，进程内、惰性清理，默认 TTL 30 分钟 / 最多 256 个 run
  （`RUN_METRICS_TTL_SECONDS` / `RUN_METRICS_MAX_RUNS`）。**没有 `X-Run-ID` 时一个字节都不写**：
  澄清聊天每轮一个请求，塞进去只会把内存撑爆，而且永远等不到 `complete`。
  多实例部署时换掉 `RunMetricsStore` 一处即可（`services/llm_metrics.py`）。
- 缺 `X-Run-ID` 时 run id 退化成 `request_id`、按 `1/1 complete=true` 上报 —— 单片路径（PRD、聊天）
  的界面口径因此与多片一致。

**日志**：`event=llm_step` 每行带 `run_id` 与 `part`（形如 `"2/3"`），`event=run_summary` 带整份字段，
所以**一条 grep 就能查全整份生成**：

```bash
grep '"run_id":"live_run_e2e_0001"' app.log   # 该 run 的全部 llm_step + 每片的 run_summary
```

**验证**：`python scripts/verify_run_agg.py`（在 `backend/` 下跑）—— 用假模型驱动**真实 SSE 生成器**，
断言第二片是整份合计、重放幂等、CORS 预检放行、日志贯通；不调真实模型、不花钱。

## 配置

全部环境变量见 `.env.example`。几个容易踩的点：

| 变量 | 说明 |
| --- | --- |
| `DEFAULT_LLM_PROVIDER` | `anthropic` / `openai` / `deepseek`。**必须与有 Key 的厂商一致**，否则生成接口一律 503 |
| `DEFAULT_LLM_MODEL` | 留空则按 provider 回退：anthropic→`claude-sonnet-4-20250514`、openai→`gpt-4o-mini`、deepseek→`deepseek-chat` |
| `CORS_ORIGINS` | 三种写法都支持：`http://a`、`http://a,http://b`、`["http://a","http://b"]` |
| `HOST` | `0.0.0.0` 监听所有网卡（局域网/容器可达，**服务本身无鉴权**）；仅本机自用建议 `127.0.0.1` |

`ANTHROPIC_API_KEY=` / `OPENAI_API_KEY=` 这类**留空即视为未配置**（不会变成空字符串密钥再去报鉴权错误），
`DEFAULT_LLM_MODEL=` 留空则走上面的回退表。

字段名与环境变量名不必相同：`llm_provider` ↔ `DEFAULT_LLM_PROVIDER` 由 `validation_alias` 绑定。

### 切换模型

只改 `.env`，不动代码：

```ini
DEFAULT_LLM_PROVIDER=deepseek
DEFAULT_LLM_MODEL=deepseek-chat
```

DeepSeek 走 OpenAI 兼容协议（`langchain-openai` + `base_url`）。

> ⚠️ **换模型不是改一行配置那么简单。** 实测把 `deepseek-chat` 换成 API 正式清单里的
> `deepseek-flash`：`GET /v1/models` 只返回 `deepseek-flash`、`deepseek-v4-pro`，
> 而 `deepseek-chat` 是未公开的旧名（官方定价页脚注：旧名仍被接受、请求由当前模型服务
> **并按该模型计费**，因此实际模型与单价都不可知）。
> 但实测切换后 **「一个 FR 不同时出现在两张表里」这条设计假设失效**，且 flash 默认开启
> 思考模式，输出 token 膨胀 3-4 倍、单次耗时 21–59 秒。**已回退**。
> 留档在 `validation_out_deepseek-flash/`，结论见 HANDOFF.md §4 坑 #14。
> 真要做这个切换，请作为独立决策 + 重跑 `validate_prompts.py`，不要顺手改。

## 当前边界

- 数据层（Postgres 16 / Redis 7）已由根目录 `docker-compose.yml` 提供，但**代码尚未接入**；
  `/health` 刻意不探测它们
- 表单（前端）、对话（S0–S5 流式）、三份产物的**生成与修订**都已有 HTTP 入口；
  ✅ **生成已经是后台任务**（`/api/jobs/*` + `services/job_runner.py`）：
  任务状态落库、断开 SSE 不 cancel、刷新可恢复、收尾同步会话。
  ⚠️ 旧的 `/conversation/generate-*-stream` 是前台流式，**已标 deprecated**，不要再用
- **仍未实现**：会话状态机 `SessionStore` / `apply_event`（`/api/v1/sessions/*` 那 8 条仍是 501）、
  产物审核/质检（M8）、文档**优化**的后台任务（本次只做了三种产物的生成任务）。
  见 HANDOFF.md §5 的建议动手顺序
- `scripts/validate_prompts.py` 验证的是**提示词设计假设**，不是代码正确性；
  且它只跑 S0/S1/S2/S4 四个澄清阶段，**S3、S5 从未验证过**，脚本里也没有对应分支
