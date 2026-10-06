# HANDOFF —— 交接说明

> 写给接手开发的**人 / AI**。读完这一份再读别的。
> 最后更新：**Generation Job（生成任务化）落地之后** —— `/api/jobs/*`：任务状态落库、
> 后台协程执行、断开 SSE 不 cancel、收尾同步会话；四个 `generate-*-stream` 标记 deprecated。
> 再往前一次是「LLM 观测 / token 预算 / 一次产物生成 = 一个 run」与前端生成观测 UI。
> （维护范围见 §11 第 9 条：**只维护 V2**。）

---

## 0. 一句话现状

**设计阶段完成，没有任何业务代码。**

`backend/` 里现存的 `main.py` / `api/` / `core/` / `services/` 是**设计之前就有的骨架**，
与新设计不匹配（没有表单、没有多轮对话、没有状态机）。
可以当作分层风格的参考，**不要当作实现基础**。

> **更新（旧骨架清理已完成）**：原骨架里的「一句话需求 → 固定八章 PRD」已按 §7 删除。
> 现在保留的是：分层外壳、配置层、LLM 工厂、**提示词组装/渲染层**、自检脚本；
> 接口面已按 `docs/会话持久化方案.md` 改成**会话中心**的形状 ——
> 健康检查 3 条 + **对话侧 3 条**（题目下发 + 首轮/接续流式对话）真实可用，
> 另有 **8 个 501 占位**（`/sessions/*`、`/config`）。
> 其中 `core/config.py`、`services/llm.py`、`core/prompts/` 是 `docs/功能清单.md`
> 明确认可并依赖的部分（F6.1 / F4.7 / F3.14 / F6.3），**不是**待删对象。
>
> **服务层的生成侧已实现**：`services/document_service.py` 有模板加载/组装/渲染、
> 三个流式生成方法（PRD / 接口文档 / 提示词套件）、流式修订（F8.6），全部走 `astream`。
> 用真模型实测跑通了「接口文档生成 + 单节修订」。
>
> **HTTP 入口也有了**：`/conversation/` 下 4 条 SSE（`generate-prd-stream` /
> `generate-api-docs-stream` / `generate-prompts-stream` / `optimize-document-stream`），
> 已实测真实 SSE、以及缺 Key 时 6 条流接口全部 503。
>
> ⚠️ 但这 4 条**不是**产物的权威写入路径 —— 它们是**前台流式**，请求断了这一轮就没了，
> 而 `会话持久化方案` §7.1 要求生成是**后台任务**。**只能当进度/预览通道**，
> 产物落库必须另走任务层。这也正是 `/sessions/{id}/documents/{kind}` 仍是 501 的原因。
>
> **前端**：`frontend/` 已建好脚手架（Vite 8 + React 19 + TS 7 + Tailwind v3.4 +
> lucide-react + react-markdown + **remark-gfm** + axios），**能构建、能在真实浏览器渲染、
> 自定义主色已验证生效**。业务页面目前有**两块**：
>
> - **20 题表单页**（动态渲染 + 校验 + 草稿）
> - **对话页** —— `MessageList` / `ChatInput` 两个组件，**并且已经接进 `App.tsx`**：
>   提交表单起首轮对话、发消息接续、流式实时渲染、本地持久化与刷新恢复都已打通。
> - **产物审核面板** `DocumentReview`（生成中只读预览 / 生成后可编辑 / AI 优化 / 一键复制 /
>   底部自定义按钮）+ 共用的 `Markdown.tsx`。组件已浏览器实测（`58/58`）。
>   ✅ 六屏产物页（`generating-*` / `review-*`）**已经接进 `App.tsx`**：产物状态、
>   3 个生成方法、优化方法、通过链推进、落本地与刷新恢复，整条链路实测 `47/47`。
>   ⚠️ 两处过渡做法：**「通过」是本地推进**（`approve` 事件要走仍为 501 的
>   `/sessions/{id}/events`）；**「生成 PRD」的显隐条件用的是模型自报的 `stage_status`**
>   （违反「阶段由服务端推进」，但没有 `SessionStore` 时它是唯一信号）。
>
> 两块都用 Chromium + CDP 做过端到端验证（对话侧 **46/46**、产物编排 **47/47**），
> 详见 `frontend/README.md`。
>
> ⚠️ **不要用"看起来对不对"验收 AI 回复。** 后端推的是结构化 JSON，直接渲染出来就是
> 一坨 JSON 语法加字面量 `\n` —— 这个坑实际踩过一次，见 §4 坑 #15。
>
> ⚠️ **前端沙箱坑**：Chromium 的 profile 目录**不能放在 `frontend/` 里** ——
> Vite 的文件监听器会去 watch `Default/Network/Cookies` 并因 `EBUSY` 直接崩掉 dev server
> （排查时很像"代码坏了"）。详见 `frontend/README.md` 的沙箱表。

---

## 1. 这个工具要做什么

面向产品经理的文档生成工具：

```
填 20 题表单 → AI 多轮追问（S0–S5） → 生成 PRD → 审核 → 生成接口文档 → 审核
            → 生成提示词套件 → 审核 → 完成
```

三份产物是**链条**不是并列：PRD 是唯一源头，接口文档从 PRD 推导，提示词套件消费前两者。

---

## 2. 最重要的一件事：分清「定论」与「未定」

**不要把下面的文档当成全部已确认的定论。** 按可靠性分三层：

### ✅ 已用真实 LLM 验证过的（可以信）

| 结论 | 证据 |
| --- | --- |
| 澄清阶段 JSON 输出可稳定解析，无 ``` 包裹、无前言 | **S0 / S1 / S2 / S4** 四个阶段干净。**S3、S5 从未跑过** —— 脚本里根本没有这两个分支，见 §4 坑 #12 |
| 每轮问题数 ≤ 3、**每问都带建议答案** | 100% 命中 |
| S2 从 12 类边界里**挑**而非全列 | 只触及 2 类 |
| S4 给具体数值与理由，不反问 | 4 组默认值 + 数字 + 依据 |
| gen_prd 章节级不越界、FR 编号规范 | — |
| **每个接口都标 `PRD 来源`** | 12/12 |
| **`=== FILE: ===` 多文件分隔契约可解析** | 2/2 文件块 |
| 「无接口功能」互斥规则生效 | 修后零冲突 |
| 「输入缺失时禁止顶替」生效 | 小节变成「本次无该维度输入」 |

原始输出留档在 `backend/validation_out/`。

> ⚠️ **这批结论是模型相关的。** 实测换成 `deepseek-flash` 后，其中一条
> （「无接口功能」互斥）失效 —— 见 §4 坑 #14。它们成立的前提是当前配置的模型
> `deepseek-chat`（`backend/.env`）。**换模型必须重跑这一整套验证。**

### ✅ 端到端真机验收（2026-09-21，真实浏览器 + 真实模型，全流程跑通）

一次完整流程：填表 → 3 轮澄清（`stage_status=done`）→ 生成 PRD → 通过 → 生成接口文档
→ 通过 → 生成提示词套件 → 通过 → 完成页 → 三份下载。**29/29 断言通过**，产物留档在
`backend/validation_out/flow_{prd,api,prompts}.txt`。

| 结论 | 实测证据 |
| --- | --- |
| **三份产物能跨进程/刷新存活** | 每一步重启浏览器后都读回上一份产物（`产物 ['prd'] → ['prd','api'] → ['prd','api','prompts']`） |
| **整份 PRD 一次生成完、不截断** | 6723 字符 / 317 行 / 23 个标题，结尾是附录 B 变更记录表。≈4000 输出 token，只占 `LLM_MAX_TOKENS` 的一半。⚠️ **这个数是老结构（15 章 + 2 附录）的**；当前结构（技能包 6 章）实测 2967 字符，**更短**。两种结构都不截断（`validation_out/prd_{skill,15ch}.txt`） |
| **整份接口文档必然被截断** | 17257 字符 / 737 行，`finish_reason=length`，末尾停在 `{ "key": "术语", "entries":` —— 半行 JSON |
| **整份提示词套件必然被截断** | 18354 字符 / 72 个标题 / **8 个 `=== FILE: ===` 块**，末尾停在 `- \`04-step-`（第 5 个文件写到一半） |
| **改成按计划分片后，两份都完整了** | 接口文档 **3 片 → 19763 字符，1–9 章 + 附录 A/B 齐全，任一片 `truncated=False`**；套件 **3 片 → 28331 字符，12 个文件块（含 05-verify），无截断**。证据：`validation_out/split_{api,prompts}.txt` |
| **换成 6 章 PRD 之后，整条链仍然通**（本轮切换结构后的关键验证） | 上游换成 `prd_skill.txt`（6 章 + `FR-01…08` / `AC-01…10`）重跑分片：接口文档 **18244 字符、3 片、1–9 章 + 附 A/B 齐全、无截断**，且 **`FR-01…08` 全部被引用**、5 个数据实体小节、`owner`/`member` 角色、外部依赖命中「飞书」—— 说明技能包新加的**认证与权限 / 数据与存储 / 外部依赖**三个子块**真的被下游取到了数**；套件 **30356 字符、12 个文件块、无截断**，覆盖矩阵里 `FR-01…08` 与 `AC-01…10` 都在。⚠️ 字符数比改前少（18244/30356 对 19763/28331）**不是退化**：上游 PRD 从 15 章变 6 章，下游跟着变短是正常的 |
| 单次整份生成的耗时 | **24–27 秒**（裸调后端接口：首字节 1.6s；浏览器侧同一份同样 24–25s）—— 满足「10 分钟拿到文档」，远好于担忧 |
| 三份下载与页面正文**逐字一致**、无 BOM | `finish` 阶段对三份各做一次 `disk == page` 比对，全通过 |
| 全流程无未捕获 JS 报错 | 探针全程挂着 `error` / `unhandledrejection` 监听，收尾为空 |

> ⚠️ **口径提醒**：上表"整份"三行是**不传 `scope`** 的过渡用法跑出来的（前端现在不会那样调了）。
> 接口文档与提示词套件在整份模式下**每一次都会被截断**；**按计划分片后不再截断**（见上表末行）。
> 分片计划由 `GET /conversation/document-plan` 下发，前端照着逐片调用 —— 见 §4 坑 #20。


### ⚠️ 设计已定、但**未经验证**

**绝大多数文档都属于这一层。** 具体包括：

- 状态机的 13 状态 / 40 转换、`apply_event` 单一入口 —— 一行代码都没写
- 会话持久化方案（浏览器只存指针、孤儿态降级）—— 未实现
- ~~三份文档模板的完整结构（PRD 17 章、接口 11 章、套件五类）—— 只验证了其中 2 个章节~~
  → **本轮已真机跑过整份**：接口文档 11 章、套件 8 个文件（这两份被截断，见上文表），
  PRD 按**当前结构（技能包 6 章）**与**回退结构（15 章 + 2 附录）**各跑过一次，都完整不截断
  （`validation_out/prd_skill.txt` 与 `prd_15ch.txt`）。**结构本身得到验证**，
  未验证的是"每章是否达标"（那是 M8 质检）。
  ✅ **补齐 `FR-xx`/`AC-xx` 之后已重跑**（1 次调用 10.0 秒）：3915 字符、6 章齐、
  `FR-01…08` 与 `AC-01…10` 均连续、10 条验收标准每条都指向真实存在的 `FR-xx`（无孤儿）、
  第 4 章五个子块都在、无残留占位符、`finish_reason=stop` 不截断 —— 也就是说
  "技能包没有编号、链条断在 PRD"这个**阻碍默认切换的前提已经不成立**
- 质检规则 F8.1–F8.6 —— 没有任何实现
- **`known_info` 的数据流要求**（`docs/状态数据设计.md` §6.4）—— 提示词层已写，数据层未实现，**因此无法验证**

### ❓ 未决，需要人或产品拍板

见 §6「待拍板清单」，共 **8 项**（本轮新增第 8 项，且它已从"建议"升级为"必做"）。

### 🚢 部署产物（**已写好，但本机没构建过镜像**）

| 文件 | 作用 | 验证到哪一步 |
| --- | --- | --- |
| `backend/Dockerfile` + `.dockerignore` | 后端镜像（python:3.12-slim + `requirements.txt` + `COPY . .`） | `docker compose config` 解析通过；**镜像未构建**（本机 Docker 引擎没运行）。其中那条 `CMD` 已**原样在本机跑通**（`/health` 与 `document-plan` 都 200） |
| `frontend/Dockerfile` + `.dockerignore` | 前端多阶段镜像（node 构建 → nginx 出静态） | 同上未构建。它内部的 `pnpm build` 在本机通过；镜像里靠 corepack 按 `packageManager` 取 pnpm 12.4.2，**这一步没实跑过** |
| `deploy/nginx.conf` | 静态 + `/api` 反代。**关缓冲 / 放大读超时 / 访问控制**三条注释是功能性的，不是调优 | 写了但**没在 nginx 里 `-t` 过**（本机没装 nginx） |
| `deploy/preflight.sh` | 服务器上的**上线前自查**：docker 引擎是否可达、`.env` 有没有 Key、`LLM_MAX_TOKENS` 是否被调小、端口是否被占、**容器能不能连到 LLM 供应商**、有没有访问控制 | **未做语法校验**：本机的 `bash` 是 WSL，被沙箱拒（`E_ACCESSDENIED`），没有可用的 POSIX shell 来跑 `sh -n`。用前请自行 `sh -n deploy/preflight.sh` |
| `docker-compose.yml` 的 `api` / `web` | 应用本体两条服务（数据层 postgres/redis 保持原样、仍未接入） | `docker compose config` 通过：4 个服务、构建上下文、`8080` 端口、nginx 配置挂载都解析正确 |
| `docs/部署.md` | 部署说明书（Compose 与裸机两条路径 + 上线前必做的三件事 + 验收步骤） | — |
| `skills/prd-generator/` | **PRD 技能包，产品的默认 PRD 结构**：`skill.yaml`（**运行时契约 + 内容契约**：`id`/`applies_to`/`artifacts`(path+role)/`enabled`/`priority` + `inputs`/`outputs`/`constraints`/`metadata`）、`instructions.md`（四步工作流：校验输入 → 归一化需求 → 按模板渲染 → 生成后质检）、`references/{field-schema.json, prd-template.md, writing-rules.md}`（8 字段输入契约 / 6 章模板（含 `FR-xx`、`AC-xx` 编号与验收标准表）/ 写作规范（含"编号与追溯"一节））。后端已接：`services/skill_loader.py` 按 `skill.yaml` 的 `artifacts` 读文件并按 `role` 分组（instructions → template → reference → schema），`document_service._skill_system_prompt()` 拼 system prompt、`_skill_schema_text()` 供摘要回填读 `role: schema`、`prd_prompts()` 是"走技能包还是走老结构"的**唯一分支点**，由 `PRD_USE_SKILL` 控制（默认 `true`）。⚠️ 它在仓库根、**不在 `backend/` 构建上下文里**（`docker build ./backend` 只拷 `backend/`），所以镜像里读不到 —— 与下面 `config/skills.yaml` 是同一个缺口 | `smoke_check.py` 覆盖 16 条：注册表可加载且通过校验（含 `applies_to` 交叉校验）、**六个产物各自绑对 skill**、**`skills: []` 合法 / 空 bundle 在消费侧退化成空**、**五种写错的注册表都要报错**、`skill.yaml` 运行时字段完整、bundle 含 template 与 schema、`compose_prompt_sections` 顺序 = `ROLE_ORDER`、instructions 可读、**拒绝目录穿越（两种写法）**、prompt 每份带来源标题、prompt 含 6 章、**含 `FR-01`/`AC-01` 与验收标准**、开关默认打开、默认 system prompt 来自技能包且不含 15 章模板、默认 human message 是技能包模板、**显式关掉时仍拿得到 15 章回退模板** |
| `skills/api-docs-generator/` | **接口文档技能包 —— 它顶掉的是原来的 RAG 检索**：`instructions.md`（怎么用下面这些规范）+ `references/team-api-guidelines.md`（团队 HTTP 接口规范：`/api/v1/` 前缀、统一响应、错误码格式、分页 / 幂等 / 时间与金额、三类条件约定，浓缩自 `docs/接口文档模板.md` 的规范部分）+ `references/writing-rules.md`（可追溯 / 双向闭合 / 枚举穷举 / 示例质量，浓缩自 `gen_api.md`）+ `references/history-weekly-report-api.md`（`split_api.txt` 里一个接口的**逐字**摘录）。`document_service._skill_human_block()` 把 instructions + reference + example **全量**拼在 human message 末尾。**章节结构与每个接口的固定 8 项仍在 system prompt（`gen_api.md`）**，两处不重复 | `smoke_check.py`：api-docs 绑对 skill 且含 reference/example、注入体积在 `skill.yaml` 声明的上限内、**真实 HTTP 生成时 human message 里确有规范与示例且规范排在示例前**、`run_summary.injected_skills` 有 api-docs-generator |
| `skills/prompts-generator/` | **提示词套件技能包**（原来没有检索那一步，加它是为了让三份产物走**同一条**注入管线）：`instructions.md` + `references/prompts-template.md`（五类提示词、目录与命名、每类要点与字数上限、类别 4 的固定 7 项 = `template`）+ `references/writing-rules.md`（覆盖完整 / 依赖无环 / 可判定 / 命名一致 / 无冗余 + `=== FILE: ===` 多文件契约） | `smoke_check.py`：prompts 绑对 skill、含 template/reference、注入体积在声明上限内、生成时 human message 里有模板与写作规则 |
| `config/skills.yaml` | **全局技能绑定表**：`artifact → skill id 列表`（6 个 Job artifact 必须写全，空列表表示"这个产物没有技能包"）。现在 **六个都绑上了**：prd / optimize-prd → `prd-generator`，api-docs / optimize-api-docs → `api-docs-generator`，prompts / optimize-prompts → `prompts-generator` | `smoke_check.py` 的注册表断言（见上一行）。⚠️ 与 `skills/` 同一个部署缺口：仓库根的东西不在 `backend/` 构建上下文里 |
| **RAG 检索（已删除）** | 原来接口文档生成前要走「前端 `POST /conversation/retrieve-api-docs-rag` → `RagHitsPanel` 让用户勾选 → 选中片段塞进 `known_info`」这条**带人工确认**的路，语料是 `document_service.RAG_CORPUS`（`docs/接口文档模板.md` / `gen_api.md` / `validation_out/*api*.txt`）加两个拖放目录 `rag/接口规范`、`rag/历史示例`，检索是**词法 BM25 近似**（同义改写召回不到）。**整条链路已删**：`RAG_CORPUS` / `RAG_DROP_DIRS` / `_rag_*` / `retrieve_api_docs_rag_hits` / 该路由 / 前端 `RagHitsPanel` 与 `retrieveApiDocsRag` **全部移除**，规范改由 `api-docs-generator` 技能包全量注入（固定、可预期，不再看关键词）。另：`docs/功能清单.md` §8「本期明确不做」本来就写着"参考资料上传与 RAG 检索"，这一篇让代码与设计重新一致 | `smoke_check.py` 4 条：`services/` 里没有 `rag_service` / `retrieve_api_docs` 残留、`document_service` 的 5 个检索符号都不在了、路由不在 OpenAPI 里、`skills: []` 与空 bundle 仍然合法；`job_check.py` 1 条：Job 收尾的 `run_summary.injected_skills` 非空 |
| `services/quality_gate.py` → `metadata.quality_gate` | **确定性结构校验（格式层体检，不调模型、不碰 DB）**：`run_quality_gate(doc_type, content, context)` 是纯函数。⚠️ 规则按**本仓库的真实规范**写，**不是**照 04 篇工单的字面 —— 工单那套（接口文档的「核心/边缘接口」分区、提示词套件的「功能开发/接口实现/前端开发/代码审查」五段）在本仓库**一次都没出现过**，照字面实现会让每一份已过评审的好文档全红。现在的规则：PRD = 6 章 + MVP 表格 + 范围两侧 + `[待确认]` ≤ 5；接口文档 = 接口清单 / 接口详情 / 错误码表 / **无接口功能**（F8.3 的互补分区）/ 统一错误体 / `/api/v1` / 接口条目数 / `FR-xx` 可追溯；提示词套件 = 五类文件（project-brief / scaffold / data-layer / step-* / verify）+ 与 PRD 功能数对齐（`context.prd_content` 缺 → 该条 skip）。`passed` **只看 high**，`score` 加权（high 2、medium/low 1）。`job_runner` 在**三条收尾路径**都算（生成 / 优化 / 失败半成品），写进 `document_versions.metadata.quality_gate`。⚠️ 与 `review` 语义**相反**：review 只在没有值时才写（优化不许抹掉审稿结论），gate **每次都覆盖**（它描述的是眼前这份正文，留旧结论就是撒谎）。前端 `QualityReportPanel` 挂在版本侧栏「审查意见」**下方**、**三份产物都挂**（审查意见只有 PRD 有），没有 gate 的老数据显示「暂无质量报告」而不报错 | `smoke_check.py` 24 条：**真实 PRD 留档与真实接口文档留档都 100 分全过**（规则若把好文档判红，这里先红 —— 这条是防止"照字面实现"的关键守卫）、缺「项目范围」→ `passed=False` 且点到 `prd.section.scope`、只缺 medium 时 `passed` 仍为 true 但扣分、score 加权口径、空正文 / 未知 doc_type 都失败（不静默全过）、`gate_error` 兜底、五类文件齐 / 缺一类、无上下文时 skip、三产物 check id 不重复、纯函数两次一致、不改传入的 context；`job_check.py` 7 条：PRD / 接口文档 / 提示词三种 Job 完成后 metadata 都有 gate、**PRD 的 gate 与「最终稿」重算逐项一致**（rewrite 之后那一稿，不是 draft_v1）、优化 Job 覆盖 gate、prompts 确实拿到了 PRD 上下文 |

> ⚠️ **技能包是产品当前唯一的 PRD 结构，`gen_prd.md` 的 15 章 + 2 附录只作回退保留。**
> `PRD_USE_SKILL` **默认 true**；关掉它才会走老结构，而老结构与技能包的章节号**互不兼容** ——
> 改这个开关要同步 `backend/core/config.py` 的 `prd_use_skill` 注释里列的那一串
> （分片计划、前端文案、两个校验脚本、`gen_api`/`gen_prompts`/`clarify_s*` 的章节引用、docs）。
>
> **为什么要把下游提示词一起改**：`gen_api.md` 与 `gen_prompts.md` 原本按老结构的章号取数
> （数据模型取"PRD 第 9 章"、鉴权取"第 10 章"、外部依赖取"第 12 章"、待确认取"第 15 章"）。
> 技能包是 6 章、没有这几章，照旧引用会让接口文档**取不到数、只能编**。现在这些引用已改为
> **按章节名 + 子块名取数**（"第 4 章技术规格的认证与权限 / 数据与存储 / 外部依赖子块"、
> "第 2 章 MVP 表 + `FR-xx`"），`[待确认]` 改为**汇总 PRD 全文的就地标注**。
> 为此技能包补了 `FR-xx`/`AC-xx` 与验收标准（套件的覆盖矩阵与验收自检要靠它们）。
>
> 另：技能包的输入契约是 8 个字段，与本项目表单的 20 题**不是一套**。`field-schema.json` 里留了
> `x-source-mapping` 做对照；目前 human message 仍把手表单数据整块交给模型，由模型自己对字段。
> 这一步**有意没做**（见 §10 决策 7）：先把结构切过去，字段映射单独做。

> ⚠️ **上公网前必须补的**：本服务**没有用户体系**，谁拿到地址谁就能烧你的 LLM 额度 ——
> `deploy/nginx.conf` 里已备好 Basic Auth 的三行注释（另有 IP 白名单 / VPN 两档）。
> 这不是"可选优化"，是上线前置条件。
>
> ⚠️ **填 `.env` 时最容易踩的一个坑（本轮实测）**：pydantic-settings 对**不认识的变量是静默忽略**的 ——
> 把别的项目的模板（`ADMIN_TOKEN` / `DATA_DIR` / `TOKEN_BUDGET_MODE` 那一套）填进来，
> 应用照常启动、一个都不生效，只有点"生成"时才发现全 503。
> `smoke_check.py` 已加两条断言把这件事提前暴露（"`.env` 里没有本项目不认识的变量名" +
> "配了当前 provider 的 Key"），并且**自测过**：喂一个坏 `.env` 会精准报出是哪几个变量、
> 以及 `DEEPSEEK_API_KEY` 是空的。**所以部署后跑一次 `smoke_check.py` 是有意义的。**

---

## 3. 文档地图

### 3.1 核心设计（按依赖顺序，这就是建议的阅读顺序）

| # | 文件 | 是什么 | 什么时候读 |
| --- | --- | --- | --- |
| 1 | `docs/功能清单.md` | **65 项功能**、优先级、现状对照、变更记录 | **最先读**。它定义了做什么 |
| 2 | `backend/core/questions_config.json` | **20 题表单定义**（唯一来源） | 做表单相关的一切之前 |
| 3 | `backend/core/form_submission.schema.json` | 表单提交数据的 JSON Schema | 同上；**由脚本生成，不要手改** |
| 4 | `docs/对话阶段设计.md` | 对话的 6 个阶段（S0–S5）、双向对话机制、过渡规则 | 做对话之前 |
| 5 | `docs/状态机设计.md` | 13 状态、40 转换（含触发条件与回退代价）、降级处理 | **做任何流程控制之前** |
| 6 | `docs/状态数据设计.md` | 数据契约、四层统一接口、持久化要求、4 个边界 | 建数据模型之前 |
| 7 | `docs/会话持久化方案.md` | 浏览器刷新场景、存储选型、版本兼容、多标签页 | 做前端或接口之前 |
| 8 | `docs/PRD模板.md` | PRD 的 **15 章 + 2 附录**结构 —— ⚠️ **这是回退结构，产品当前不用**；现在走 `skills/prd-generator/` 的 6 章（见 §10 决策 7） | 做生成环节之前 |
| 9 | `docs/接口文档模板.md` | 接口文档的 11 章结构 + 设计原则 + 错误码约定 | 同上 |
| 10 | `docs/提示词套件模板.md` | 套件五类 + 多文件输出契约 | 同上 |
| 11 | `docs/技术栈.md` | **技术栈清单**、版本口径、本期不需要什么及原因 | 搭环境 / 开工前 |
| 12 | `docs/部署.md` | **怎么跑到服务器上**：Compose / 裸机两条路径、反代必须调的三项、上公网前必须补的访问控制、部署后验收步骤 | 要上线 / 交给运维之前 |

### 3.2 检查报告（记录「某版有什么问题、怎么修的」）

| 文件 | 检查什么 |
| --- | --- |
| `docs/表单与对话覆盖度检查.md` | 表单字段能否支撑三份产物；对话阶段是否接得住 |
| `docs/状态机覆盖度检查.md` | 4 类「生成中」的降级是否覆盖；状态遗漏 |
| `docs/三份文档模板覆盖度检查.md` | 20 字段 → 三份模板的落点；覆盖是否闭环 |

> 这三份是**过程记录**，随修复会过期。可以当"为什么这么设计"的说明读，别当现状读。

### 3.3 Prompt（11 份可用）

| 文件 | 用途 |
| --- | --- |
| `backend/core/prompts/clarify_common.md` | 澄清阶段的角色基线 + **引导技巧清单 26 条** + JSON 输出契约 |
| `clarify_s0.md` … `clarify_s5.md` | S0 开场复盘 / S1 场景落地 / S2 功能细化 / S3 边界与优先级 / S4 约束与验收 / S5 摘要确认 |
| `gen_common.md` | 生成阶段的角色基线、输入说明、**输入缺失时的行为**、分片生成规则 |
| `gen_prd.md` / `gen_api.md` / `gen_prompts.md` | 三份产物的生成 Prompt |

**组装方式**：`system prompt = {产物}_common.md + {阶段/产物}.md`，顺序不可颠倒。
已由 `backend/core/prompts/__init__.py` 的 `build_system_prompt()` 实现，**禁用 `str.format`**；
`scripts/validate_prompts.py` 也调用同一份实现，不再自带一份 `render()`。

### 3.4 脚本

| 文件 | 用途 |
| --- | --- |
| `backend/scripts/gen_form_schema.py` | 从 `questions_config.json` **生成** `form_submission.schema.json`（改题目后必须重跑） |
| `backend/scripts/validate_prompts.py` | **提示词层验证**：真实 LLM 跑「澄清 S0/S1/S2/S4 → 生成三产物」。`--recheck` 复查历史输出、不花钱。⚠️ 它**不跑表单提交**，也**没有 S3/S5 分支**（§4 坑 #12），不是全链路端到端 |
| `backend/scripts/smoke_check.py` | 离线自检：配置 / 提示词清单 / 提示词组装渲染 / 模型构造 / 路由与占位状态码。断言已随 §7 清理同步更新，全过 |
| `backend/scripts/http_check.py` | 对着运行中的服务打真实 HTTP（不做 LLM 调用）。会断言旧 `/prd` 路由已 404 |
| `backend/scripts/e2e_flow.py` | **全流程端到端验收**（真实浏览器 + 真实模型）：填表 → 追问 → 三份产物 → 通过 → 完成页 → 三份下载。`all` 一次跑完，也可按阶段续跑（`chat` / `prd` / `api` / `prompts` / `finish` / `status` / `diag` / `trace`）。**`validation_out/flow_*.txt` 就是它生成的**。前置：后端 8000 + 前端 5173 都已启动；Chromium 在受限沙箱下起不来（见脚本 docstring） |
| `backend/scripts/split_check.py` | **分片生成验收**（真实模型，6 次调用）：按 `GET /conversation/document-plan` 逐片生成、按前端同一套逻辑拼接，核对"每片都没截断 + 章节/文件一个不少"。产物留档 `validation_out/split_{api,prompts}.txt`。**这是坑 #20 的守卫** —— `smoke_check` 只管计划的形状，管不了输出长度 |

> 已删除：`preview.py`（旧骨架的提示词预览），随 `clarify.md` / `system_prd.md` 一并移除。

---

## 4. 实测踩过的坑（**动手前务必先看这一节**）

这些都付过一次 LLM 调用的代价才发现的，重踩一遍很浪费。

| # | 坑 | 应对 |
| --- | --- | --- |
| 1 | **`str.format()` 渲染 Prompt 必炸** | 文件里既有运行时占位符 `{generate_scope}`，也有格式记号 `{模块缩写}{3位序号}`。实测 `KeyError: '模块缩写'`。**用 `str.replace` 或 `string.Template`** |
| 2 | **字数上限对「详细」型章节不可靠** | 实测第 7 章 1772/1500（+18%），而第 5、6 章守住了。别把字数当硬约束 |
| 3 | **输入缺失时模型不会留空，会用邻近内容顶替** | 实测：`compliance` 为空时，模型把「仅第三方登录」「JWT 鉴权」写进了「安全与合规约定」——**全是权限，没有一条合规**。错位内容比空话危险得多，因为它看起来像真的 |
| 4 | **`known_info` 少一项，生成阶段就编** | 实测：缺 S3 优先级 → **7 条功能全标 P0**；缺 S4 默认值 → 合规小节被顶替。**这是数据流要求，提示词救不了**（见 `docs/状态数据设计.md` §6.4） |
| 5 | **「无接口功能」表必须互斥** | 不加互斥规则时，模型会同时把一条 FR 放进两张表（理由写「某子能力是本地行为」）。一旦允许，覆盖闭合就无法机械校验 |
| 6 | **孤儿生成态** | 会话停在 `*_generating` 但任务已消失（重启/发版）→ 用户永远卡住。必须降级为 `generation_failed`。**内存方案下不会发生，切 Postgres 后必然发生** |
| 7 | **表单版本漂移** | 会话要记 `form_version`，按它解释 `form`。本项目表单已改过 3 次（10→15→20 题）。新增题 = minor，删题或改 `id` = **major** |
| 8 | **编号前缀冲突（3 处）** | `S1–S5` 同时是「对话阶段名」和「状态机待确认项」；`A1–A5` 同时是「提问技巧」和「接口文档待确认项」；`D1–D5` 被两份文档各用一套。**引用时务必带文件名** |
| 9 | **术语表方向会翻** | 修「重复」之后变成了「遗漏」（2 次 → 0 次）。必须**明确指定放哪个文件**，否则两边都可能错 |
| 10 | **模板有两份来源** | 章节清单**内嵌在 `gen_*.md`**，同时也在 `docs/*模板.md` 里。手工同步的错误率是 100%（本轮我改了 3 次错）。要么改成运行时注入，要么每次改都跑对比校验 |
| 11 | **`str.replace` 对缺失键静默跳过** | 这是坑 #1 的修法**引入的新失败模式**：`str.format` 会抛 `KeyError`（吵但看得见），换成 `str.replace` 后，没传进来的占位符原样留在 prompt 里、不报任何错。实测 `gen_common.md` 声明为必需输入的 `{doc_outline}`、`{generate_scope}`、`{scope_spec}` **没有任何调用方注入**，三个「验证通过」的 gen 步骤都是带着字面量 `{generate_scope}` 在跑。应对：`core/prompts.declared_placeholders()` + 每次运行打印未注入清单（已实现） |
| 12 | **S3、S5 没有验证分支** | `validate_prompts.py` 的 `main()` 只有 `want("s0"/"s1"/"s2"/"s4")`，`recheck()` 也只遍历这 4 个 → 6 个澄清阶段里有 2 个**结构性跑不到**，不是"没跑"而是"跑不了"。应对：补分支后重跑，或明确接受不验证 |
| 13 | **`deepseek-chat` 不在 API 的模型清单里** | 实测 `GET {base_url}/models` 只返回 `deepseek-flash`、`deepseek-v4-pro`；`deepseek-chat` 仍能调通。官方定价页脚注说明「旧模型名仍被接受，请求由当前模型服务并按该模型价格计费」—— **既不知道实际用的哪个模型，也不知道按哪个价扣费**。这是**未决项**，见 §10 第 2 条 |
| 14 | **换模型会让「已验证」的结论失效（实测，代价 7 次调用）** | 把默认模型换成正式清单里的 `deepseek-flash` 并重跑全量验证：JSON 契约、每问带建议、分片契约、`=== FILE: ===`、compliance 不顶替 **全部仍然成立**，但 **「一个 FR 不同时出现在两张表里」（坑 #5 的修复）失效** —— 6 条 FR 同时出现在接口清单与无接口表里。另：flash 默认开启思考模式，输出 token 膨胀 3–4 倍（S4 一次 13049 输出 token 只换 3461 字），单次耗时 21–59 秒，7 次调用共 3.6 分钟，直接威胁「10 分钟拿到文档」的目标。**已回退到 `deepseek-chat`**，flash 留档在 `backend/validation_out_deepseek-flash/` |
| 15 | **AI 回复直接显示就是「一坨 JSON + 字面量 `\n`」** | 实测（浏览器截图）：`chunk` 里推的是 `{"message": "我看了你的表单…", "questions": [{id, question, suggested_answer, reason}], "conflicts": [], "stage_status": "asking", "open_questions": []}` —— 与 §6.2 契约一致，**所以后端没错**；错的是把它当散文渲染。表现有两个：整屏 JSON 语法，以及正文里的 `\n` 显示成两个字符而不是换行。**修法**：`ChatMessage` 拆成 `content`（原始 JSON，用于回传 history）+ `display`（抠出的 `message`，用于渲染），见 §10 第 4 条。⚠️ 流式**最开头**还会闪一截残片（模型刚吐 `{` 而 `"message"` 键尚未出现），所以派生函数对"看起来是 JSON 但还没抠出 message"的情况在流式期间**返回空串**而不是回落原文 —— 这一条已加浏览器回归断言 |
| 16 | **上游产物被反复内联，system prompt 体积按引用次数翻倍** | `str.replace` 替换的是**每一处**，而占位符在提示词里被引用多次。实测计数：`{prd_content}` 在 `gen_common.md` 出现 2 次 + `gen_api.md` 3 次 = **5 次**，于是整份 PRD 被内联 5 遍。用 2723 字的上游 PRD 夹具实测，`gen_api` 的 system prompt 达到 **28453 字符**（各部分朴素相加只有约 14000）。**真实 15 章 PRD 会直接把上下文撑爆**，而报错发生在很后面，很难联想到是提示词体积。同理 `{known_info}` 在 `gen_prd.md` 里用 9 次、`{doc_outline}` 4 次、`{form_summary}` 4 次。**应对**：`document_service` 已加体积告警（超 30000 字符就 warning，常量 `_PROMPT_WARN_CHARS`）；**根治要改提示词文件** —— 大块输入只引用一次，其余地方改成"见上文"。⚠️ 改提示词等于改验证条件，必须重跑 `validate_prompts.py`。**真机复测（端到端跑通时）**：真实 PRD 只有 6723 字符（见 `docs/PRD模板.md` §4.3 的实测；那是**老结构**的 PRD，当前技能包结构只有 2967 字符，所以这条体积问题**只会更轻**），而 `gen_api` 的 system prompt 实测 **48670 字符**（≈ 6723 × 5 + 模板本体），告警如期触发。**结论修正**：不会「撑爆上下文」（当前模型窗口装得下，实测生成成功），真正的代价是**白烧约 2 万输入 token + 同一份 PRD 在 5 个位置各说一遍、注意力被稀释** —— 属于该修但不紧急 |
| 17 | **显式配了 `DEFAULT_LLM_MODEL` 就绕过 `FALLBACK_MODELS`，换 provider 时会跨厂发模型名** | `core/config.py` 的 `active_llm_model` 是 `self.llm_model or FALLBACK_MODELS[self.llm_provider]`。那张回退表的注释写的是「有这张表，就不会因为忘改 `DEFAULT_LLM_MODEL` 而把 A 厂的模型名发给 B 厂」—— 但**只要 `.env` 里显式写了模型名，回退就永远不会生效**。实测：`DEFAULT_LLM_PROVIDER=openai` + `.env` 里留着的 `DEFAULT_LLM_MODEL=deepseek-chat` → `/health` 报 `provider=openai, model=deepseek-chat`，于是会把 `deepseek-chat` 发给 OpenAI。**应对**：换 provider 时必须同时清掉 `DEFAULT_LLM_MODEL`；根治要么在启动时校验"显式模型名与 provider 是否匹配"（需要一张前缀映射表），要么让 `/health` 在两者不匹配时报警。|
| 18 | **`LLM_MAX_TOKENS` 一直是个摆设，而"撞上限被截断"完全静默** | 三件事叠在一起，少看一件就会得出错误结论：① langchain-openai 1.6 会把 `ChatOpenAI(max_tokens=N)` **改名成 `max_completion_tokens`**（实测 `_get_request_payload()` 里只有这个键、没有 `max_tokens`）—— 而 `model_kwargs={"max_tokens": N}` **也会被改名**（我第一版修法就错在这里）；**DeepSeek 不认这个字段、直接忽略**，实测发 `max_completion_tokens=16` 仍吐出 1500 个分片且 `finish_reason=stop`。只有 `extra_body={"max_tokens": 16}` 会在 16 token 处截断并报 `length`。**配置里的输出上限从未生效。** ② 真正在截断的是**厂商默认上限 8192** —— 数字恰好等于我们的 `LLM_MAX_TOKENS=8192`，所以看起来像"配置生效了"，极具迷惑性。③ 截断在正文里**看不出来**：末尾就是半行表格（实测停在 ``| `group_chat_config_id` | string | 来源群聊``），`_stream_document` 又从不看 `finish_reason`，前端于是显示成"待审核"—— 用户会拿残文档去通过、下载、交给下游。**整份接口文档与提示词套件必然如此**（实测 17438 / 18354 字符）。**已修**：上限改走 `extra_body`；`services/llm.py` 增 `finish_reason_of()` / `is_truncated()` / `StreamOutcome`；**6 个流式路由**（对话 2 + 文档 4）都把 `truncated` 带进 `done` 帧；前端落盘 `StoredDocument.truncated` 并显示警告横幅，对话侧被截断则标红并给明确文案（`CHAT_TRUNCATED_NOTICE`）。**根治靠分片生成 —— 已实现**，见下面第 20 条 |
| 19 | **跑浏览器探针时改前端源码 = 自己把探针弄死** | Vite 的 HMR 只要 `frontend/src` 里有保存就**整页重载**，而 CDP 探针装的 `window.__h` 是普通全局 —— 重载即消失，之后每个 `Runtime.evaluate` 都抛 `window.__h is undefined`。探针的 `ev()` 会把异常包成 `{"__error__": ...}` **当正常返回值交回**，于是崩在一个与真实问题毫无关系的地方：本轮实测崩在 `text[-70:]`，报 `KeyError: slice(None, None, None)`，把一次真实生成的现场（耗时、字符数、失败提示）全毁了。**应对**：① 探针运行期间**只改后端和文档，不碰前端源码**；② 把 helpers 一并注册进 `Page.addScriptToEvaluateOnNewDocument`，重载后自动重装；③ **`ev()` 的返回值必须先 `isinstance(x, str)` 再当字符串用**。②③ 已落在 `backend/scripts/e2e_flow.py` 里（它的 docstring 把这条列为必读坑）。另有两条同源经验：**`status()` 是内存派生的、`docText()` 读的是 localStorage**（落盘有防抖，刚翻转就读必然读到旧值 → 本轮误判出"生成结果为空"的假 bug）；**用户消息是乐观上屏的**，所以"消息行数 ≥ 1"在 AI 回复到达**之前**就成立（本轮量出过"首轮耗时 0.1s"这种不可能的数值） |
| 20 | **"整份生成装不下"到底装不下的是哪一份？**（这条是第 18 条的根因修复） | 早先文档把结论写反了：**整份 PRD 装得下、不截断**（老结构实测 6723 字符 ≈ 4000 输出 token，15 章 + 2 附录齐全；当前技能包结构更短，3915 字符），**装不下的是接口文档与提示词套件**。**已修**：新增 `services/document_plan.py` —— 分片计划**从提示词文件里解析**（接口文档取 `gen_api.md` 的「章节清单」表并按字数上限贪心分批；套件取 `gen_prompts.md` 的「目录结构」代码块，按"固定文件前段 / 可变组 / 固定文件后段"分），**不另抄一份清单**（否则就是坑 #10）；`GET /conversation/document-plan` 把它交出去；前端照着逐片调用再拼接（`stitchParts()` 只做"丢空片 + 去掉重复 H1"这种保守清理，不假装能修 Markdown 结构）。**实测效果（上游换成 6 章 PRD 后本轮复测）**：接口文档 3 片 → **18244 字符、1–9 章 + 附 A/B 齐全、任一片都没截断**；套件 3 片 → **30356 字符、12 个文件块（04-step-01…07 + 05-verify 全在）、无截断**（更早那一轮上游是 15 章 PRD，得到 19763 / 28331）。守它的东西：`smoke_check.py` 断言"每一章恰好落在一个分片里"（独立解析一遍清单，不复用被测函数）+ `scripts/split_check.py` 真跑 6 次调用核对完整性。⚠️ **留白**：套件的可变组（`04-step-*`）没有按步骤数再切 —— 步骤数由 PRD 决定而计划是无状态纯函数；步骤特别多的项目那一片仍可能超上限（届时会由截断横幅暴露） |
| 21 | **同一个"缺附录"被误判了两次 —— 断言盯同义词而不是盯那一节在不在** | `split_check.py` 校验接口文档附录时，第一版写 `"附 A" in text`，模型实际写 `## 附录 A 枚举值汇总` → 把**完整**文档报成缺附录；于是改成只认 `"附录 A"`，本轮模型又写成 `## 附 A`（`gen_api.md` 自己的章节清单里写的也是"附 A"），**同一个误判再来一次**。方向相反的另一类错误是"弱断言恒为真"（写 `"附录" in text` 就永远通过）。**已修**：改成按**标题**匹配 `^#{1,3}\s*附(?:录)?\s*([AB])(?![A-Za-z])`，两种写法都认、`附 ABC` 不认。**教训**：这类"模型换个同义词就翻车"的断言，判据要落在结构位置（标题级 / 表格列数）上，不要落在措辞上 —— 校验技能包章节数用的是同一思路（解析 `## N. 标题`，而不是找"产品概述"这个字符串） |
| 22 | **机器上起着一个改动之前就启动的后端，验证脚本会安静地验旧代码** | 本轮跑 `split_check.py` 时端口 8000 已被占用，而占用者是一个**我改动之前**（14:02）就起着的 uvicorn：它加载的是旧的 `config.py` / `document_plan.py`（`PRD_USE_SKILL` 默认还是 false、PRD 计划标签还是"15 章 + 2 附录"），拿它跑分片验证等于验旧代码，而脚本照样会打印"通过"。更麻烦的是**诊断工具本身不可靠**：`Get-NetTCPConnection -LocalPort 8000` 返回空（看着像端口空闲），只有 `netstat -ano` 能看到 `LISTENING 14888` —— 只看前者会得出"端口没被占"的错误结论，然后一头撞在 `[Errno 10048]` 上。**应对**：① `split_check.py` 的地址改成可用 `HARNESS_API_BASE` 覆盖，不再硬编码 8000；② 验证前先 `netstat -ano | findstr :<port>` 确认占用者是谁、启动时间是不是**在本次改动之后**（`Get-Process -Id <pid> | Select StartTime`）；③ **不要**为了让脚本跑起来去杀别人的进程 —— 换个端口起自己的（本轮就是这样：临时 8124，跑完即关） |

---

## 5. 从哪开始动手

### 建议的第一步：**先把表单和对话闭环跑通**

理由：这是整条链路的入口，也是唯一能立刻验证「20 题表单用户填得完吗」「S0–S5 真的按设计走吗」的地方。

具体顺序建议（不强制）：

1. 表单渲染 + 提交接口（吃 `questions_config.json` 和 `form_submission.schema.json`）
2. `SessionStore` + `apply_event` 单一入口（`docs/状态数据设计.md` §4）
3. 澄清阶段接入（`clarify_common` + `clarify_s0..s5`，注意坑 #1）
4. `known_info` 的合并逻辑（坑 #4，**这一步最容易漏**）
5. 再往后接生成

`docs/对话阶段设计.md` §8 有一份 **10 项重构清单**，是照着实现时可以直接用的工单。

### 实现顺序上的一个建议

`docs/状态机设计.md` §2 的表列了**每个状态用户能做什么**。
可以直接把它当成路由/按钮的验收清单 —— 状态机不只是流程图，它是交互规格。

---

## 6. 待拍板清单（**开工前请先确认这几项**）

| # | 问题 | 影响 |
| --- | --- | --- |
| 1 | 字数上限对「详细」型章节：放宽 / 接受超标 / 改成分段计数？ | 生成环节的输出控制 |
| 2 | 章节内内容错位要不要加显式禁令？ | 实测第 6 章（场景）写了第 4 章（角色）和第 7 章（边界）的内容。现有禁令只管到「章节级」 |
| 3 | S2 挑边界的优先级策略？ | 实测 S2 只问 2/12 类边界 → PRD 里出现 8 处 `[待确认]`，其中一些本可用默认值解决 |
| 4 | 接口文档能否新增 PRD 之外的实体？ | 实测出现了 `/api/v1/message-summaries`（PRD 数据模型里没有这个实体）。允许但要标注来源，还是禁止？ |
| 5 | URL 命名两处冲突：`GET .../export`（动作型操作）、路径含供应商名 `wecom` | 与 `docs/接口文档模板.md` §3.1 的规则冲突 |
| 6 | `{doc_outline}` / `{generate_scope}` / `{scope_spec}` 是否改为运行时注入？ | **注入这一半已经做了**（`DocumentService` 把三者连同其余声明项一次性注入，见 §10 第 5 条）。**仍未定的是"值从哪来"**：现在由调用方通过 `DocumentScope` 传，`doc_outline` 不自动生成。是否改成解析 `docs/*模板.md` 得到清单（那样提示词里就不该再内嵌一份）仍未决定 —— 它与下面的第 8 项是同一个决定的两面，而第 8 项**已被实测逼成必做** |
| 7 | **流式输出与 JSON 契约如何共存？** | F3.14 要求 15s 内界面不能空白 → 要么流式要么轮询；而 §6.2 要求输出是结构化 JSON。 | **显示侧已解决**（本轮实测确认后端确实推结构化 JSON，前端用 `extractStreamingMessage()` 抠出 `message` 渲染，原始 JSON 留给 history 回传 —— 见 §4 坑 #15、§10 第 4 条）。**仍未解决的是「一键采纳建议」**：`questions[].suggested_answer` 现在**收得到但没用上**，需要决定是①等整个 JSON 收完再一次性解析出 `questions`，还是②改成边流边增量解析。三个更大口径的候选仍待拍板：① 流式 + 结束后整体解析；② 让模型先只输出 `message` 正文、结构化部分后补（**要改提示词契约**）；③ 不流式，改用等待指示（与 §7.5 的轮询方案一致） |
| 8 | **接口文档 / 提示词套件改成分片生成**（即 `docs/PRD模板.md` §4.3 的 R4） | ✅ **本轮已实现**（`HANDOFF.md` §4 坑 #20）：计划由 `services/document_plan.py` 从提示词文件解析，`GET /conversation/document-plan` 下发，前端逐片调用再拼接。实测接口文档 19763 字符（1–9 章 + 附录 A/B 齐全）、套件 28331 字符（12 个文件块），**都不再截断**。**仍然要你拍板的只剩切分粒度**：现在是"接口文档按章节攒批到 4500 字上限、套件按结构分三段"，要不要改成"接口文档按模块切、套件按步骤切"（后者需要把 PRD 摘要传进计划，因为步骤数由 PRD 决定） |
| 9 | **6 章 PRD 没有「成功指标」与「风险与假设」的落点**，但澄清阶段仍在收这两类信息（S4 第 1 组问成功指标、S0 问可行性与依赖风险） | 现在这两类信息只能落进第 1 章「产品概述」的核心问题（期望效果）与第 4 章的依赖子块，**没有一个显式的表格/子块承接**。三个候选：① 保持现状（信息散在正文里，够用）；② 第 1 章加「成功指标」子块（表格：指标 / 当前值 / 目标值 / 期限）；③ 第 1 章加「成功指标」+「风险与假设」两个子块（接近老结构的第 3、14 章）。**注意这是改动使用方给定的 6 章结构，所以留给用户拍板**，我不动 |
| 10 | **20 题表单 → 技能包 8 字段的显式映射**（`field-schema.json` 的 `x-source-mapping` 已备好对照） | 现在 human message 把表单数据**整块**交给模型，由模型自己往 8 个字段上对。风险：字段名不同（`problem`/`core_features` vs `product_goal`/`mvp_features`），模型可能对错或漏掉某个字段 → 技能包第 1 步"校验输入"就形同虚设。候选：① 保持整块交给模型；② 在 `document_service` 里做显式映射，按 8 字段拼 human message（技能包的校验规则才真正生效）。本轮**有意没做**（见 §10 决策 7 第 ④ 条） |
| 11 | **`flow_api.txt` / `flow_prompts.txt` 这两份"整份被截断"的证据，重跑 `e2e_flow.py` 就会被覆盖掉** | 本轮顺手发现的**旧问题**：`e2e_flow.py` 驱动的是前端，而前端**已经改成分片生成**，所以重跑不会再产出被截断的文档 —— 一跑就等于把"改前"的证据换成"改后"的。更糟的是 `phase_api` 里还写着 `expect_truncated=True`（分片前的结论），**重跑必然失败**，而报错看起来像功能坏了（本轮已把该断言改成 `False` 并在写入处加了警告，**没有动那两个文件**）。候选：① 把这两份改名为 `flow_api_presplit.txt` 之类的历史留档，让 e2e 从此写新名字；② 接受覆盖（那"改前被截断"就只剩 `split_*.txt` 的对比说明作证）。**没有替用户改文件名的原因**：留档改名属于"动证据"，得先确认 |

另有 **56 条待确认项**散在 8 份文档里（分 7 套编号，见 §4 坑 #8）。**不要在开工前把它们全部收敛** ——
多数会在实现时自然解决。但上面这 **11** 项会直接影响代码结构，建议先定
（第 8 项本轮被实测从"建议"升级为"必做"；第 9、10 项是本轮切换 PRD 结构时新暴露出来的；
第 11 项是核对留档时顺手发现的旧债）。

---

## 7. 必须删掉的旧文件

| 文件 | 为什么 |
| --- | --- |
| `backend/core/prompts/clarify.md` | 已被 `clarify_common.md` + `clarify_s0..s5.md` 取代。它是「一次性输出问题清单 + `ENOUGH` 哨兵」，没有阶段、给不出建议 |
| `backend/core/prompts/system_prd.md` | 已被 `gen_prd.md` 取代。它是「一句话需求 → 固定八章」，没有分片生成、没有条件章节、没有质量校验 |

**不要把它们留作降级路径** —— 留着就是两套逻辑长期并存。

删的时候记得同步清理 `backend/scripts/smoke_check.py` 里对 `clarify` / `system_prd` 的断言，
以及 `backend/services/prd_service.py` 里用它们的 `draft()` / `clarify()`。

> **✅ 已完成（本轮）**：删除了 `clarify.md`、`system_prd.md`，以及**连带失效**的
> `services/prd_service.py`、`api/prd.py`、`scripts/preview.py`（后三个只服务于这两个提示词）；
> 同步更新 `api/router.py`、`api/deps.py`、`services/__init__.py`、`scripts/smoke_check.py`、
> `scripts/http_check.py`、`backend/README.md`。验证：离线自检全过、HTTP 全过、
> `--recheck` 仍是那 1 项已知未通过。
>
> ⚠️ **与 `docs/对话阶段设计.md` §8 的冲突（需要你知道）**：那份重构清单的第 3、4、9 项
> 要求**改写** `api/prd.py` 的 `ClarifyResponse`、`services/prd_service.py` 的 `clarify()`、
> 以及旧表单入参 —— 但这三个文件现已不存在。
> 落地时应把这些内容实现到 `api/conversation.py`（§8 第 5 项）与新增的表单接口（第 6、7 项）里，
> 而不是"恢复旧文件再改"。

---

## 8. 怎么再跑一次验证

```powershell
cd backend
$env:PYTHONIOENCODING='utf-8'

# 不带 --only：跑 7 次真实调用（约 35 秒）
.venv\Scripts\python.exe scripts\validate_prompts.py

# 只验证某几步（会复用 validation_out/ 里已保存的上游产物）
.venv\Scripts\python.exe scripts\validate_prompts.py --only gen_api,gen_prompts

# 复查历史输出，不调 LLM、不花钱
.venv\Scripts\python.exe scripts\validate_prompts.py --recheck
```

前置：`backend/.env` 里有可用的 `DEEPSEEK_API_KEY`（已配置）。

**当前 `--recheck` 结果：只有 1 项未通过**（`gen_prd` 第 7 章 1772/1500），
对应 §6 待拍板第 1 项。

---

## 9. 规模参考

| 内容 | 数量 |
| --- | --- |
| 设计文档 | 11 份 / 约 11.5 万字符 |
| Prompt 文件 | **11 份**：`clarify_common` + `clarify_s0..s5` + `gen_common` + `gen_prd` / `gen_api` / `gen_prompts`。交接时为 13 份，旧骨架的 `clarify.md`、`system_prd.md` 已按 §7 删除 |
| 数据配置 | 2 份（`questions_config.json`、`form_submission.schema.json`） |
| 脚本 | **7 份**（`gen_form_schema.py`、`validate_prompts.py`、`smoke_check.py`、`http_check.py`、`e2e_flow.py`、`split_check.py`、`skill_prd_check.py`） |
| 验证留档 | `backend/validation_out/` **共 19 个文件**：7 份来自 `validate_prompts.py`（4 份澄清 + `gen_prd` / `gen_api` / `gen_prompts`）；2 份来自 `document_service` 的服务层调用；2 份走完整 HTTP + SSE 的路由调用；**3 份是全流程真模型端到端跑出来的**（`flow_prd/api/prompts.txt`）；2 份是分片生成的产物（`split_api/prompts.txt`）；2 份是 PRD 两种结构的对照（`prd_skill.txt` 当前结构 / `prd_15ch.txt` 回退结构）；再加 1 份 `README.md` 说明来源。另有 `backend/validation_out_deepseek-flash/` 7 份（换模型实验证据，见 §4 坑 #14）。⚠️ **几组留档的差别很重要**（分片占位符注没注入、走的是哪一层、技能包改动前后），见 `validation_out/README.md` |
| 业务代码 | 对话侧（澄清 S0–S5 流式）+ 三份产物的生成与修订（`services/document_service.py`）。**仍缺**：会话状态机、后台生成任务、M8 审核质检、数据层接入 |

---

## 10. 本轮由 AI 代决的事项（留痕）

用户授权代决。**八条**决定与依据记录在此，便于回溯与推翻。

| # | 事项 | 决定 | 依据 |
| --- | --- | --- | --- |
| 1 | `docs/对话阶段设计.md` §8 第 3/4/9 项与本文档 §7 冲突 | **不恢复旧文件**；落点为 `api/sessions.py`（会话生命周期与状态推进）+ `api/conversation.py`（题目下发与流式回复）+ `api/schemas.py`（对外契约） | §8 自己的开头就是「不要试图在旧结构上打补丁」；且 `api/prd.py` 这个模块名在新设计下职责已错位（PRD 只是三份产物之一）。已在 §8 加「状态更新」小节 |
| 2 | DeepSeek 模型 ID | **先切到 `deepseek-flash`，实测退化后回退到 `deepseek-chat`；最终决策推迟到生成阶段** | 切换动机成立：`deepseek-chat` 不在 `/models` 清单、实际模型与计费不可知（坑 #13），且 flash 输出价约为 v4-pro 的 1/3。**但实测退化**（坑 #14）：FR 互斥假设失效 + 思考模式导致 token / 耗时膨胀。按事先声明的判据「若退化就回退」执行。 |
| 3 | `docs/功能清单.md` §7 现状对照 与 `docs/对话阶段设计.md` §8 的陈旧指向 | 一并修正 | 同一事实不允许存在两份互相矛盾的记录 |
| 4 | `ChatMessage` 要不要拆 `content` / `display` 两个字段 | **拆**。`content` 存模型原始输出（结构化 JSON 原文），`display` 存从中抠出的 `message` 正文；`display` 是纯派生、**不落本地存储**，读回来时按 `content` 重算 | 二者用途不同且都不能省：`content` 要作为 `history` 原样回传（抠掉 `questions` 等于让模型忘了自己问过什么），`display` 要给用户看。见坑 #15 |
| 5 | `{doc_outline}` / `{generate_scope}` / `{scope_spec}` 怎么注入（§6 第 6 项） | **做成显式参数 `DocumentScope`，由调用方传；服务不设默认章节清单**。三个占位符一律注入（不适用的填「（尚无）」），并在 `_stream_document` 里对"声明了却没注入"打 warning | ① 做成显式参数让"忘了传"变成类型上不可能，而不是变成一句静默留在 prompt 里的 `{generate_scope}`（坑 #11）；② **不自带章节清单**：提示词里已内嵌一份，再抄一份必然漂移（坑 #10），"是否改成解析 `docs/*模板.md`"是 `gen_common.md` 的待决策项，不该由实现顺手决定 |
| 6 | 分片占位符注入后，human message 要不要跟着改 | **不改，保持与 `validate_prompts.py` 逐字一致**（`本次只生成：{generate_scope}\n\n详细规格：\n{scope_spec}`）。三个占位符是**叠加**注入到 system prompt 的，不是替换 human message | 那 7 份留档是在这个 human message 下产出的，换措辞就换了验证条件。叠加是纯增益：原来模型看到的是一句字面量 `{generate_scope}`，现在是真实范围，而 human message 没变 |
| 7 | PRD 技能包（`skills/prd-generator/`）的定位与默认结构（用户授权代决；**用户已拍板"只保留技能包这一套"**） | **技能包升为产品唯一的 PRD 结构**：`PRD_USE_SKILL` 默认 `true`，`gen_prd.md` 的 15 章 + 2 附录降为**回退路径**（显式关开关才走）。为补上下游追溯链，技能包增加了 `FR-xx`/`AC-xx` 编号与验收标准表，以及第 4 章的权限 / 数据实体 / 外部依赖三个子块；`gen_api.md`、`gen_prompts.md`、`clarify_s*.md` 里的"PRD 第 N 章"引用**全部改成按章节名与子块名取数**。**字段映射（20 题表单 → 技能包 8 字段）仍然不做** | ① 用户明确选择"只保留技能包那套"，所以把它做**完整**而不是与老结构并存 —— 两套结构在同一个产品里必然出现"文档说一套、产物另一套"。② 早先之所以不敢默认打开，是因为技能包**没有 `FR-`/`AC-` 编号与验收标准**（实测 `validation_out/prd_skill.txt` 全文 0 个），而接口文档要求"每个接口标注对应的 `FR-xx`"、套件要求"追溯到 PRD 的 FR" —— 这次先把这个缺口补上再切默认，链条才不断。③ 更隐蔽的一层：下游提示词原本按**老结构的章号**取数（数据模型取"PRD 第 9 章"、鉴权取"第 10 章"、外部依赖取"第 12 章"、待确认取"第 15 章"），6 章结构里这些章号**都不存在**，只切 PRD 会让接口文档取不到数、只能自己编 —— 所以引用改成按**名**取数（"第 4 章技术规格的认证与权限子块"这类），`[待确认]` 改成汇总 PRD 全文的就地标注。④ 字段映射仍单独做：它是"结构定下来之后"的独立改动，混在一起会让这次切换没法验证（哪个问题导致的失败分不清）。 |
| 8 | 设计文档里遗留的"PRD 第 N 章"引用（`docs/对话阶段设计.md`、`docs/功能清单.md`、`docs/状态数据设计.md`、`docs/三份文档模板覆盖度检查.md` 等十余处） | **不动**。只在 `docs/PRD模板.md` 顶部加"这是回退结构"的横幅，并把**仍在使用**的 `docs/接口文档模板.md` 的取数来源改成新结构 | 这些文档是**设计记录**（记录当时为什么那样设计、发现了什么），不是操作手册 —— 按新结构重写会让它们失去"当时判断"的价值。它们描述的是老结构这一点，由 `docs/PRD模板.md` 的横幅与本节这条记录统一交代；`docs/接口文档模板.md` 不同，它是接口文档当前的规格，取数来源写错会直接误导实现 |

> **模型切换的下一步（建议在生成阶段一并做）**：
>
> 1. 先试 `deepseek-flash` **+ 关闭思考模式**（API 的 `reasoning_effort: "none"`，经
>    `extra_body` 传入）—— 若 token 与耗时降下来、且 FR 互斥恢复，flash 才是比
>    `deepseek-chat` 更好的默认；
> 2. 单点验证只要 1 次调用：`validate_prompts.py --only gen_api`（会复用
>    `validation_out/gen_prd.txt` 作为上游输入）；
> 3. 确认后再重跑 7 次全量，并**保留切换前的留档**用于对比。
>
> `deepseek-v4-pro` 也已实测可调用，但输出价为 flash 的 3.3 倍。

---

## 11. 已拍板事项（用户确认）

| # | 事项 | 结论 | 落到哪 |
| --- | --- | --- | --- |
| 1 | **前端技术栈** | React 18 + TypeScript + Vite + Tailwind CSS + shadcn/ui + TanStack Query + react-markdown + remark-gfm + React Router + react-hook-form；测试 Vitest + Playwright | `docs/技术栈.md` §2.3；`功能清单.md` Q8 已标为已定 |
| 2 | **接口路径形状** | 采用 `会话持久化方案.md` 的**会话中心**形状（`/sessions/*`），**不采用** `功能清单.md` F1.2 写的 `/conversation/sessions` | 占位落到 `api/sessions.py`（会话）+ `api/conversation.py`（对话阶段）；`功能清单.md` F1.2 已更正 |
| 3 | **版本控制** | 暂不 `git init`，**等本轮开发工作完成后统一提交一次** | 见 `docs/技术栈.md` §5 风险 R1 |
| 4 | **对话阶段命名空间** | 新增 `/conversation/*`（题目下发、流式回复）；`/sessions/*` **保持不动**，两者是兄弟命名空间 | `api/conversation.py`；`/form/questions` 并入 `/conversation/questions`，`api/form.py` 已删 |
| 5 | **路由层纪律** | 路由**只做参数解析与响应组装**，不写业务逻辑；服务层不 import `api.*` | 文件读写与建模放 `core/questions.py`；服务占位在 `services/conversation_service.py` / `document_service.py` |
| 6 | **提示词常量的落点** | `services/prompts.py` 只放**名字**，正文仍只在 `core/prompts/*.md`；常量由 `core.prompts.assemble_prompt_template()` 组装 | 避免第二份来源（§4 坑 #10）。`smoke_check.py` 有一条断言**逐字比对**常量与 `.md`——谁复制正文进 Python，它立刻失败 |
| 7 | **LLM 工厂门面** | 新增 `services/llm_factory.py` 的 `get_llm()`，**转发**到既有的 `build_chat_model()`，不复制实现；**保留 deepseek** | 只留 anthropic / openai 会断掉唯一配了 Key 的链路（`validation_out/` 那 7 份留档全靠它） |
| 8 | **API 版本段** | **保留 `/api/v1/`**，不改造成裸 `/api/` | 自检与文档全部按 `/api/v1` 写；版本段是将来做破坏性变更时唯一的退路（`docs/接口文档模板.md` 的原则也是「路径前缀 `/api/v1/`，破坏性变更才升版本」）。已因漏 `/v1` 排查过三次，故写进本节，并在 `backend/README.md` 加了可直接复制的 curl |
| 9 | **维护范围：只维护 V2** | 以后**只维护 `/v2*`**（`/v2` 新建、`/v2/:id` 编辑、`/v2/list` 列表）；**V1 的 `/` 保留现状、不再维护** —— 不删、不补功能、不为它写新分支。localStorage 持久化层**暂时保留**（V2 仍在用它：表单草稿 / 结构化录入摘要 / 入口模式偏好 + 首屏秒开缓存） | `/` 与 `/v2*` 的路由都在 `frontend/src/main.tsx`；V2 外壳是 `frontend/src/pages/V2Workbench.tsx`。⚠️ 两种版本渲染**同一个** `App`（`frontend/src/App.tsx`），所以「只改 V2」通常意味着**只动外壳/路由层** |
| 10 | **生成任务化（Generation Job）** | 新增 `/api/jobs/*`（创建 / 快照 / 订阅 SSE）：任务状态与草稿落 `generation_jobs` 表，执行在后台 `asyncio.Task`，进度走进程内广播；**断开 SSE 不 cancel 任务**；同一份文档只允许一个在跑的任务（重复 → 409）；收尾时同步会话（`activeJobId` / `viewState` / `documents.<kind>.content` / `prdReviewResult`）；有 running 任务时 `generating-*` **不再被降级**；服务重启把遗留 `running` 标成 failed。四个 `generate-*-stream` 保留兼容、标 `deprecated=True` | `services/job_models.py` / `job_repository.py` / `job_service.py` / `job_bus.py` / `job_runner.py`；`api/jobs.py`；`services/session_service.py` 的降级与同步；`main.py` 的 lifespan（建表 + 扫描遗留任务）。验收：`scripts/job_check.py`（离线 66 项）+ `--live`（三种产物真模型各一个任务） |
| 11 | **前端接上 Generation Job** | 四条链路（PRD / 接口文档 / 提示词套件 的**生成** + **AI 优化**）都改成「创建 Job + 订阅 Job stream」，不再有前台 `POST *-stream`；刷新按 `activeJobId` 自动重连、快照全文立刻显示（不重播打字机）；Job 生命周期**不进** `App.tsx`（在 `hooks/useGenerationJob.ts` / `utils/jobStream.ts` / `services/jobApi.ts`）。本轮把 `optimize-document-stream` 也任务化：`optimize-*` artifact、**全程停在 `review-*`**（不切 `generating-*`、无 Stepper）、`draft_content` 是"那一节"、收尾由后端拼回整篇（`services/section_edit.py`），失败也拼回整篇再落库 | 新增 `frontend/src/{types/job.ts,services/jobApi.ts,utils/jobStream.ts,utils/jobViews.ts,utils/docMeta.ts,hooks/useGenerationJob.ts}`；`App.tsx` 删掉前台产物流（`docAbortRef` / `docPartialRef` / `lastPartialWriteRef` 与 `inFlightDoc` 参数一并删） |

> 第 2 条是本轮发现的**第三处设计文档互相矛盾**（前两处见 §10 第 1、3 条）。判定依据：
> `状态数据设计.md` 与 `会话持久化方案.md` 详细定义了数据契约与接口面，而 `功能清单.md`
> 只在某行功能描述里带过；且 `接口文档模板.md` §3.1 的原则是「路径不带版本以外的前缀」——
> `/api/v1/conversation/sessions` 多了一层无意义前缀。

> **第 9 条（只维护 V2）落地时注意三件事**，都是读代码时容易踩的：
>
> 1. **`App` 是两版共用的**。在 `App` 内部的改动会**同时出现在 V1 的 `/`** —— 这不是 bug，
>    是复用（见 `pages/V2Workbench.tsx` 的文件头）。所以「只改 V2」= 只动外壳层与路由；
>    真要动 `App`，验收只看 V2 三条路由即可，不必再为 `/` 补测试。
> 2. **不能按 `sessionId` 判断「是不是 V1」**：`/v2`（新建）与 `/` 的 `sessionId` 都是 `null`
>    （`V2Workbench.tsx` 里 `id && !fromSelf ? id : null`）。要区分必须由 V2 外壳**显式传开关**。
> 3. **V1 现在也会写服务端库**：`ensureSessionSaved()` 没设门槛（`App.tsx` 里挂在「开始对话」
>    「生成 / 重新生成」「PRD 入口」三个按钮上），所以 V1 建的方案会进 `backend/harnessprd.db`，
>    也会出现在 `/v2/list`。既然 V1 不维护，暂时**接受**这个行为；将来若要把 V1 隔离成本地-only，
>    做法见第 2 条（加开关，别按 `sessionId` 判）。

> **第 10 条（生成任务化）落地时注意四件事**：
>
> 1. ✅ **前端已接入**（同一轮改动）：三条生成链路都走「`POST /api/jobs` → 订阅
>    `GET /api/jobs/{id}/stream`」。入口在 `hooks/useGenerationJob.ts`，页面（`App.tsx`）
>    只传会话上下文、只把 hook 写回的状态渲染出来 —— 页面里**没有** `readJobStream`、
>    没有 `createJob`、没有重连 effect、没有产物流的 `AbortController`。
>    刷新重连的入口是快照里的 `activeJobId`（前端 `buildSnapshot` 必须带着它，
>    否则下一次整份覆盖会把后端写的那份抹掉）。
>    ⚠️ `AppV2.tsx` **在这个仓库里不存在**：V2（`/v2*`）与 V1（`/`）渲染的是**同一个**
>    `App.tsx`，所以这次改造落在共用文件上，没有新增 V1 分支（HANDOFF §11 第 9 条）。
> 2. **接口文档 / 提示词套件在任务里仍然分片**（`document_plan` + `services/stitch.py`）。
>    这是对需求的一处**有意偏离**：需求只说"复用现有 generator"，但实测整份必然被输出上限截断
>    （§4 坑 #18/#20），退回单次调用等于把已修好的 bug 引回来。PRD 不分片（整份装得下）。
> 3. **降级规则的语义变了**：`generating-*` 不再一律当孤儿态 —— 服务端有 `status=running`
>    的任务时它是**活的**，降级会让前端不去重连、用户把半截草稿当终稿。
>    `session_service.downgrade_session_data(..., running_job_ids=…)` 是唯一的判断入口。
> 4. **单进程边界**：`job_bus` 是进程内字典，多实例部署时"创建任务的实例"与"订阅的实例"
>    若不是同一个，订阅者只拿得到 snapshot 与最终 done。换 Redis pub/sub 的改动点只在
>    `job_bus.publish` / `subscribe` 两个函数里（需求本次明确不做 Celery / Redis / 独立 Worker）。
> 5. ⚠️ **SSE 订阅必须"先注册订阅、再读任务状态"**（`api/jobs.py` 的 `_job_stream`）。
>    反过来的顺序有一个真实竞态，**实测复现过**：读状态时任务还在跑，而 runner 恰好在
>    "读状态"与"注册订阅"之间跑完并广播 `done` + 哨兵 —— 那些事件推给了没人，订阅者
>    于是等一个永远不会再来的 `done`，界面上**永远停在"生成中"**（复现场景：在审查阶段刷新页面）。
>    回归用例见 `scripts/job_check.py` 的「竞态」一条（用 `wait_for` 把"挂住"变成失败断言）。

> **第 11 条（AI 优化任务化）落地时注意五件事** —— 前四条都是"优化与生成不是一回事"：
>
> 1. **`draft_content` 的语义随 artifact 变**：生成任务里它是"产物全文的增量"，
>    优化任务里它是**"模型这一节写了多少"**（服务层只回一节，`generate_scope` 被强制对齐）。
>    拼接在后端收尾做（`services/section_edit.py` 的 `replace_section`，是
>    `DocumentReview.tsx` 里同名 TS 函数的镜像），结果放 `result_json.content` 与 `done.content`。
>    ⚠️ **`GET /api/jobs/{id}` 与 SSE 首帧都必须带 `section`**：客户端要靠它 + 会话里的整篇
>    自己拼出"整篇 + 正在改写的那一节"。本轮实测踩到过 —— 首帧漏了 `section`，优化中的正文
>    只显示那一节片段，前后章节全不见，看起来像"整篇被替换了"。
> 2. **优化全程停在 `review-*`**，跑的时候**不切** `generating-*`（与生成任务相反）：
>    用户就停在审阅页上看这一节被改写，切走再切回来界面会闪；`session_service` 里
>    `sync_job_started` / `sync_optimize_*` 按 `is_optimize_artifact` 分支。
>    界面上也**没有 Stepper**（优化是单段流，硬画三步条是骗人），只有 `beginOptimizeGeneration()`
>    那一行 hint。
> 3. **优化失败时不能把草稿直接写进 `documents.*.content`**：那是把整篇换成一段话。
>    能拼就拼回整篇（`job_runner._optimize_partial`），拼不了就一个字不动 ——
>    前端失败路径同口径（`subscribeJob` 的 optimize 分支）。同理**不写 `truncated`**
>    （那是"整篇被截断"的标记）、**不清 `prdReviewResult`**（没重新审稿）。
> 4. **判重按"冲突组"而不是 artifact 字符串**（`job_models.ARTIFACT_CONFLICT_GROUP`）：
>    `prd` 与 `optimize-prd` 写同一个字段，必须互斥（→ 409）；不同文档之间不冲突。
>    前端另有一道 UI 守卫：生成中 `DocumentReview` **不显示**优化面板（反之按钮一律禁用），
>    所以正常操作撞不到 409 —— 但服务端那道必须有，否则绕过 UI 就能让两份输出互相覆盖。
> 5. **改 `artifact` 的 CHECK 要迁移旧库**：SQLite 不能改 CHECK，`CREATE TABLE IF NOT EXISTS`
>    对已存在的表也不动手。`job_repository.ensure_schema()` 会检测旧表 → 复制数据重建
>    （实测 16 行旧任务一行没丢，两条索引按"先重建后建索引"的顺序恢复）。
