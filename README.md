# harnessprd

一个把「一句话产品想法」变成三份工程文档的工具：先通过表单和 AI 对话把需求问清楚，再依次生成
**PRD**、**接口文档**、**提示词套件**，每一份都要人工审核通过才进入下一份。

生成靠调用大模型完成，提示词不写在代码里 —— 全部外置在 `backend/core/prompts/*.md`，改提示词不用改代码。

## 现在能做什么，不能做什么

能做的：

- 填表 → AI 追问（默认最多 4 轮）→ 生成三份文档 → 逐份审核、编辑、单节 AI 优化 → 完成页下载 Markdown
- 接口文档和提示词套件**分片生成**：服务端按提示词里内嵌的章节清单算出分片计划，前端逐片调用再拼接
- 生成过程是流式的（SSE），页面能看到文字逐段出现和分片进度

还不能做的（都是已知缺口，不是 bug）：

- **没有用户体系**，谁拿到地址谁就能用，而每次生成都在消耗你的模型额度。放公网前必须先加访问控制
- **服务端不存任何状态**：会话和三份产物都在**访问者自己的浏览器 localStorage** 里。换设备、清缓存就没了；多用户之间也不共享
- **生成是前台请求**（SSE）：生成过程中关掉页面或刷新，这一轮就丢了
- **审核「通过」是前端本地推进**，没有真的调服务端状态变更接口（那个接口仍是 501 占位）
- **质检（M8 / F8.1–F8.6）没有实现**，所以「待审核」目前只能靠人眼看
- 数据层（Postgres/Redis）在 compose 里备着，但**业务代码还没接入**，起来也不会多出任何能力

## 本地跑起来

需要 Python 3.12 和 Node 20+（实测 Node 24、pnpm 12.4.2）。

**1. 后端**

```powershell
cd backend
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt      # 依赖的唯一来源
Copy-Item .env.example .env                        # 然后填 Key
```

`.env` 里至少改这三项：

```ini
DEFAULT_LLM_PROVIDER=deepseek      # deepseek | openai | anthropic
DEEPSEEK_API_KEY=sk-你的key
ENVIRONMENT=local                  # 只接受 local / dev / staging / prod
```

> 值后面不要写行内注释（会被当成值的一部分）。`DEFAULT_LLM_MODEL` 留空即按 provider 用默认模型。

```powershell
.venv\Scripts\python -m uvicorn main:app --host 127.0.0.1 --port 8000
```

**2. 前端**

```powershell
cd frontend
pnpm install
pnpm dev
```

打开 http://127.0.0.1:5173 。前端只写相对路径 `/api/v1/...`，开发期由 Vite 代理到后端，所以不涉及跨域。

**3. 确认它真的就绪**

```powershell
curl.exe -s http://127.0.0.1:8000/health
```

`llm_configured` 必须是 `true`，否则点生成会返回 503。

## 目录结构

```
backend/
  api/            路由层（只做参数解析与响应组装，不写业务逻辑）
  services/       业务逻辑：对话编排、三份产物的生成与优化、分片计划、模型工厂
  core/           配置、提示词加载与渲染、表单题目
    prompts/*.md  11 份提示词正文（生成逻辑主要在这里）
  scripts/        校验脚本，见下
  validation_out/ 真实模型跑出来的留档（结论的证据）
frontend/
  src/App.tsx     流程编排（对话、产物、视图状态、本地持久化）
  src/components/ 表单、消息列表、审核页、步骤条
  src/services/   接口封装（含 SSE 读取）、本地存储
deploy/           反代配置与上线前自查脚本
docs/             11 份设计文档
HANDOFF.md        交接总入口：现状、已实测的结论、坑列表、待拍板项
```

## 校验脚本

在 `backend/` 下跑。前两个不花钱，后三个会真实调用模型。

| 脚本 | 作用 | 成本 |
| --- | --- | --- |
| `scripts/smoke_check.py` | 离线自检：配置、提示词资源、模型构造、路由、分片计划、截断判定 | 免费 |
| `scripts/http_check.py` | 对运行中的服务打真实 HTTP，校验状态码与入参校验（需后端已启动） | 免费 |
| `scripts/validate_prompts.py` | 提示词层验证：真实模型跑各阶段与三份产物（`--recheck` 免费复查历史输出） | 7 次调用 |
| `scripts/split_check.py` | 分片生成验收：证明按计划分片后不再被输出上限截断 | 6 次调用 |
| `scripts/e2e_flow.py` | 全流程端到端：真实浏览器 + 真实模型，填表到下载走一遍（需前端也在跑） | 约 5 次调用 |

## 部署

见 `docs/部署.md`。两个容器：`api`（uvicorn）和 `web`（nginx 出静态 + 反代 `/api`）。

三条容易踩的：反代必须关响应缓冲并放大读超时（否则 SSE 不流式、长文档 504）；
`LLM_MAX_TOKENS` 别调小；**上公网前必须加访问控制**（配置文件里已备好 Basic Auth 的写法）。

## 两个硬约束（改代码时最容易违反）

1. **提示词正文只存在于 `backend/core/prompts/*.md`**。渲染用 `str.replace`，不能用 `str.format`
   （提示词里有给人看的格式记号 `{模块缩写}` 之类，`format` 会当成占位符报错）。
2. **章节/文件清单只有一份真相，就在提示词文件里**。分片计划是**解析**出来的
   （`services/document_plan.py`），不要在任何别处再抄一份清单。

更多踩过的坑见 `HANDOFF.md` 第 4 节。
