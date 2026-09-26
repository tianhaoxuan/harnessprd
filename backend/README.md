# harnessprd backend

面向产品经理的文档生成工具后端（FastAPI + LangChain）。

> **接手开发请先读仓库根目录的 [`../HANDOFF.md`](../HANDOFF.md)。**
> 那份文档说明设计现状、哪些结论已验证过、哪些还需要拍板。
> 本 README 只讲后端骨架怎么跑、坑在哪。

## 当前状态

设计阶段已完成（见 [`../docs/`](../docs/)），**业务代码部分实现**。

- **已实现**：对话侧 3 条接口（题目下发 + 首轮/接续流式对话）、澄清阶段编排、
  三份产物的生成与修订（`services/document_service.py`，`astream` 流式）
- **未实现**：会话状态机（`SessionStore` / `apply_event`）、后台生成任务、
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
│   ├── conversation.py     /conversation：题目下发 + 首轮/接续对话 + 产物生成/修订（**7 条均已实现**，SSE）
│   ├── config.py           运行配置接口（占位，501）
│   └── deps.py             依赖注入（Settings / ConversationService / DocumentService）
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
│   └── document_plan.py         **分片计划**：从提示词文件解析章节/文件清单并分批（不调模型）
├── scripts/
│   ├── smoke_check.py      离线自检：配置 / 提示词 / 模型构造 / 路由 / 分片计划
│   ├── http_check.py       对着运行中的服务打真实 HTTP
│   ├── gen_form_schema.py  从 questions_config.json 生成 form_submission.schema.json
│   ├── validate_prompts.py 真实 LLM 验证提示词（会花钱；`--recheck` 免费）
│   ├── e2e_flow.py         全流程端到端验收（真实浏览器 + 真实模型；`all` 一次跑完）
│   └── split_check.py      分片生成验收（真实模型 6 次调用；证明不再被截断）
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
| POST | `/api/v1/conversation/generate-prd-stream` | **已实现** — 生成 PRD（SSE，`scope` 可选；PRD 整份装得下） |
| POST | `/api/v1/conversation/generate-api-docs-stream` | **已实现** — 从 PRD 生成接口文档（SSE，**应传 `scope` 按计划分片**） |
| POST | `/api/v1/conversation/generate-prompts-stream` | **已实现** — 从 PRD 生成提示词套件（SSE，**应传 `scope` 按计划分片**） |
| POST | `/api/v1/conversation/optimize-document-stream` | **已实现** — 按反馈修订文档某一节（SSE，F8.6） |
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
> ⚠️ 其中 4 个生成接口**没有满足「生成必须是后台任务」**（`会话持久化方案` §7.1）：
> 它们是前台流式，请求断了这一轮就没了。**只能当进度/预览通道，不能当产物的权威写入路径。**

占位接口一律返回 **501 Not Implemented**，不返回 200 假数据——假数据会被误当成"已实现"。
`/health` 与 `/api/v1/health` 复用同一个 handler，不是两份实现。

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
- 表单（前端）、对话（S0–S5 流式）、三份产物的**生成与修订**都已有 HTTP 入口
  （`/conversation/*` 的 4 条 SSE）；
  **会话状态机、后台生成任务、产物审核/质检（M8）仍均未实现** ——
  所以 `/sessions/*` 那 8 条仍是 501。
  ⚠️ 但这 4 条入口**不是**产物的权威写入路径：它们是前台流式，请求断了这一轮就没了，
  而设计要求生成是后台任务（`会话持久化方案` §7.1）。产物落库要先有 `SessionStore`。
  见 HANDOFF.md §5 的建议动手顺序
- `scripts/validate_prompts.py` 验证的是**提示词设计假设**，不是代码正确性；
  且它只跑 S0/S1/S2/S4 四个澄清阶段，**S3、S5 从未验证过**，脚本里也没有对应分支
