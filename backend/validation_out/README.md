# validation_out —— 真实 LLM 输出留档

**这里全是真实模型产出，不是夹具、不是手写的。** 用途是让 `HANDOFF.md` §2「已用真实 LLM
验证过的结论」有据可查 —— 结论成立与否，直接看文件。

生成器与关键差别如下。**别把它们当成同一类东西**：

| 文件 | 生成器 | 说明 |
| --- | --- | --- |
| `clarify_S0.txt` / `S1` / `S2` / `S4` | `scripts/validate_prompts.py` | 澄清阶段四个阶段。**S3、S5 没有分支，从未跑过**（见 `HANDOFF.md` §4 坑 #12） |
| `gen_prd.txt` | `scripts/validate_prompts.py` | PRD 第 5–7 章 |
| `gen_api.txt` | `scripts/validate_prompts.py` | 接口文档第 6 章 |
| `gen_prompts.txt` | `scripts/validate_prompts.py` | 提示词套件 00-README + 01-project-brief |
| `gen_api_via_service.txt` | `services/document_service.py`（一次性驱动脚本，已删） | 接口文档第 6 章，**走服务层**组装 |
| `optimize_api_via_service.txt` | `services/document_service.py`（同上） | 对上一份的「无接口功能」小节做流式修订（F8.6） |
| `gen_api_via_route.txt` | `POST /api/v1/conversation/generate-api-docs-stream`（一次性驱动脚本，已删） | 同样内容，但**走完整 HTTP + SSE** —— 用来证明路由把参数正确转给了服务层 |
| `optimize_api_via_route.txt` | `POST /api/v1/conversation/optimize-document-stream`（同上） | 单节修订，走 HTTP |
| `flow_prd.txt` | **真实浏览器 + 真实模型的全流程**（`scripts/e2e_flow.py`） | 端到端第一份产物：**整份生成、完整不截断**（6723 字符 / 23 个标题，结尾是附录 B 变更记录表） |
| `flow_api.txt` | 同上 | 端到端第二份产物：**整份生成必然被截断**（17257 字符，`finish_reason=length`，末尾停在半行 JSON） |
| `flow_prompts.txt` | 同上 | 端到端第三份产物：**同样被截断**（18354 字符 / 8 个 `=== FILE: ===` 块，第 5 个文件写到一半） |
| `split_api.txt` | `scripts/split_check.py`（**分片**生成，3 次真实调用，上游 = `prd_skill.txt`） | 按 `GET /conversation/document-plan` 逐片生成的接口文档：**18244 字符、1–9 章 + 附 A/B 齐全、任一片都没截断**。**本轮已按 6 章 PRD 重跑**：`FR-01…08` 全部被引用、5 个数据实体小节、`owner`/`member` 角色、外部依赖命中「飞书」—— 证明技能包新加的三个子块**真的被下游取到了数** |
| `split_prompts.txt` | 同上（上游 = PRD + 上一行的接口文档） | 同上方式生成的提示词套件：**30356 字符、12 个 `=== FILE: ===` 块（00/01/02/03 + 04-step-01…07 + 05-verify）、无截断**，覆盖矩阵里 `FR-01…08` 与 `AC-01…10` 都在 |
| `prd_skill.txt` | `scripts/skill_prd_check.py`（**产品当前结构**，1 次真实调用） | 走 `skills/prd-generator/` 的 **6 章**结构生成的 PRD：3915 字符、6 章齐全、`FR-01…08` 与 `AC-01…10` 连续、无截断。**本轮已按补齐编号后的技能包重跑** |
| `prd_15ch.txt` | `scripts/skill_prd_check.py --legacy`（**回退结构**，1 次真实调用） | 走 `gen_prd.md` 的 **15 章 + 2 附录**结构、**与上一行完全相同的输入**：6834 字符、17 个标题、无截断 |

`prd_skill.txt` 与 `prd_15ch.txt` 是**同一份输入、两种结构**的对照留档。技能包那份是
**补齐编号之后**重跑的（旧版 2967 字符、0 个编号，那份已被覆盖，不再存在）：

| 指标 | `prd_skill.txt`（技能包 6 章） | `prd_15ch.txt`（回退 15 章） |
| --- | --- | --- |
| 字符数 | 3915 | 6834 |
| 行数 | 152 | 255 |
| 章节（`## `）数 | **6** | 17（15 章 + 2 附录） |
| `FR-xx` 编号数 | **8**（FR-01…08，连续） | 8 |
| `AC-xx` 编号数 | **10**（AC-01…10，连续） | 17 |
| `[待确认]` 数 | 2 | 3 |
| 表格行数 | 53 | 92 |
| 单次调用耗时 | 10.0 s | 15.4 s |
| 是否截断 | 否（`finish_reason=stop`） | 否 |

技能包那份**通过的结构检查**（脚本核对，不是印象）：6 章齐；`FR`/`AC` 各自从 01 起连续、
不跳号不重号；10 条验收标准**每条都指向一个真实存在的 `FR-xx`**（无孤儿）；
`FR-xx` 同时出现在第 2 章 MVP 表与第 6 章「在范围内」，两处编号一致；
第 4 章的五个子块（认证与权限、数据与存储、前后端技术、部署方式、外部依赖）都在；
无残留占位符（`{...}`）。

两处需要**正读**的地方：

- 15 章那份"少"的三项事实是 `容器化` / `JWT` / `PostgreSQL`，这是**故意的**：
  老结构的规则不许 PRD 写技术选型，那部分信息按设计进接口文档与提示词套件；
  而技能包允许在第 4 章写选型，所以它覆盖到了。**覆盖率不是纯质量分**，不能只看这一行判定谁更好。
- 技能包那份**短一半**不是"内容更少就等于更差"：它的 6 章里没有老结构的
  数据需求、权限矩阵、风险假设、开放问题这几章，那部分信息按设计移到接口文档与就地 `[待确认]`。

**复现方式**：

```powershell
cd backend
$env:DEEPSEEK_API_KEY = '...'                        # 只走进程环境变量，不落盘
$env:DEFAULT_LLM_PROVIDER = 'deepseek'               # 不设会走配置默认的 anthropic，那个 Key 没配
.venv\Scripts\python scripts\skill_prd_check.py            # 生成 prd_skill.txt（跟产品默认走）
.venv\Scripts\python scripts\skill_prd_check.py --legacy   # 生成 prd_15ch.txt（回退结构）
```

## ⚠️ `flow_*` 与 `split_*` 是同一件事的**改前 / 改后**（`flow_*` 已过期，`split_*` 是本轮的）

`flow_api` / `flow_prompts` 是**整份一次生成**（不传 `scope`）的产物，**尾部都被截断**；
`split_api` / `split_prompts` 是**按服务端计划分片**生成的产物，**完整不截断**。
两组放在一起就是"分片修好了静默截断"的证据（`HANDOFF.md` §4 坑 #18 与 #20）。
PRD 只有 `flow_prd.txt` —— 它整份就装得下，**刻意不分片**。

⚠️ **两组的"改前 / 改后"关系只在数量级上成立，别逐字对齐**：

- `flow_*` 三份是**分片落地之前**跑出来的，且 `flow_prd.txt` 是**老结构（15 章）**的 PRD。
  现在的代码走分片、PRD 走技能包 6 章，**重跑不会再得到这三份的样子**。
- `split_*` 两份本轮已按**当前结构**（上游 = `prd_skill.txt`，6 章 + `FR`/`AC`）重跑，
  所以字符数比上一版少（18244 / 30356 对 20126 / 32949）—— 这**不是退化**：
  上游 PRD 从 15 章变成 6 章，接口文档与套件跟着变短是正常的；要盯的是
  "`FR-xx` 有没有被追溯、分片有没有截断"，两项都通过。

## ⚠️ `flow_*` 三份和上面所有留档**不是一类东西**

上面每一份都是"**只生成某一章 / 某一节**"的分片留档，而 `flow_*` 是**整份产物**（不传
`scope`）—— 也就是"用户点了生成、前端一次性调完"的那种请求。它们的价值正在于此，实测出来的结论是：

| 产物 | 整份生成结果（分片之前） | 分片生成结果（`split_*`） |
| --- | --- | --- |
| PRD | **完整**（6723 字符 ≈ 4000 输出 token，上限 8192 用了一半；老结构 15 章） | 刻意不分片（同上） |
| 接口文档 | **被截断**（17257 字符，末尾半行 JSON） | **完整**（本轮 18244 字符，1–9 章 + 附 A/B） |
| 提示词套件 | **被截断**（18354 字符，第 5 个文件写一半） | **完整**（本轮 30356 字符，12 个文件块） |

所以"整份 PRD 装不下单次输出上限"这个早先的判断是**反的**：真正需要分片的是第二、三份
（`HANDOFF.md` §4 坑 #18 / #20）。`flow_*` 里后两份的末尾就是截断点的原始证据。

**复现方式**：驱动它们的是 **`backend/scripts/e2e_flow.py`**（常驻脚本，不再是"一次性、已删"）：

```powershell
# 前置：后端 8000、前端 Vite 5173 都已启动
cd backend
.venv\Scripts\python scripts\e2e_flow.py all      # 约 3–5 分钟，含 5 次真实调用
```

⚠️ **它会把 `flow_prd.txt` / `flow_api.txt` / `flow_prompts.txt` 原地覆盖。**
而 `flow_api.txt` / `flow_prompts.txt` 现在是**分片之前**的"整份被截断"证据，
重跑不会再产出被截断的文档（前端已按计划分片）—— 也就是说**一跑就把这两份证据换掉了**。
想留住它们先改名，见 `HANDOFF.md` §6 第 11 项（那一条是核对留档时发现的旧债，尚未处理）。
`flow_prd.txt` 同样是老结构（15 章）的产物，重跑会变成技能包 6 章版本。
也可以只跑某一段（`prd` / `api` / `prompts` / `finish`），会话状态在浏览器 profile 里续着。
注意：脚本用的是临时目录下的**独立 profile**，与本机的浏览器互不干扰，删掉那个目录就是全新会话。

## ⚠️ 上面两组的关键差别：分片占位符有没有被注入

`gen_common.md` 声明了 `{doc_outline}` / `{generate_scope}` / `{scope_spec}`，但
**`validate_prompts.py` 不注入它们** —— 它把范围与规格放进 human message，于是这三个
占位符**以字面量留在 system prompt 里**（`HANDOFF.md` §4 坑 #11）。
模型看到 `{generate_scope}` 这种字样照样能输出像样的结果，所以"验证通过"掩盖了
"输入根本没送到"。

`document_service` 把三者连同其余声明项**一次性全部注入**（`missing_placeholders()` 兜底）。
因此：

- **前 7 份留档 ≠ 对 `document_service` 的验证** —— 它们是"占位符未注入"条件下的产物
- **带 `_via_service` 的 2 份才是服务层的留档**，而且它们还**没有对应的常驻脚本**，
  复现要重写驱动（这是当前的一个缺口，见下）

## 复现方式

```powershell
cd backend
$env:DEEPSEEK_API_KEY = '...'                                  # 只走进程环境变量，不落盘
$env:DEFAULT_LLM_PROVIDER = 'deepseek'                         # 不设会走配置默认的 anthropic，那个 Key 没配
.venv\Scripts\python scripts\skill_prd_check.py                # PRD 当前结构（1 次调用）
.venv\Scripts\python scripts\skill_prd_check.py --legacy       # PRD 回退结构（1 次调用）
.venv\Scripts\python scripts\split_check.py                    # 分片验证（6 次调用；需后端跑当前代码）
.venv\Scripts\python scripts\split_check.py --recheck           # 免费：只复查已归档的 split_*.txt 结构
.venv\Scripts\python scripts\validate_prompts.py               # 7 份分片留档（7 次调用）
.venv\Scripts\python scripts\validate_prompts.py --recheck      # 只对已有留档重跑结构化检查（免费）
```

⚠️ `split_check.py` 默认连 `127.0.0.1:8000`。**机器上常有一个改动之前就起着的后端占着这个端口**
（本轮实测撞到：`netstat` 看得到、`Get-NetTCPConnection` 看不到），拿它跑等于验旧代码 ——
用 `HARNESS_API_BASE` 指到自己新起的那个端口：

```powershell
$env:HARNESS_API_BASE = 'http://127.0.0.1:8124/api/v1/conversation'
```

带 `_via_service` / `_via_route` 的四份目前**只能手工复现**（`split_check.py` 覆盖的是
`flow_*` 与 `split_*` 那几份）。

## 待办（已知缺口）

**1. 验证路径与运行时路径是两份实现。**
`validate_prompts.py` 的 `run_gen()` **自己拼了一套 system + human**，绕过了
`document_service`。这正是坑 #11 能长期潜伏的原因：
"验证过的提示词"和"实际发出的提示词"不是同一个东西。

**应该把 `run_gen` 改成走 `DocumentService`**，让留档天然代表运行时行为。
没顺手改的原因是：那会改变 system prompt（三个占位符从字面量变成真实值），
上面 7 份基线可能因此变化，必须作为独立改动 + 重跑全部留档来做。

**2. 那四份带后缀的留档没有常驻生成器。**
它们证明了服务层与路由层跑得通（真实 SSE、`done.chars` 对得上、参数确实转到了服务层），
但生成它们的驱动脚本是一次性的、已删。要么把它们并进 `validate_prompts.py` 的某个
`--only` 分支，要么接受"这几份是一次性证据、不可复现"。

另：`validation_out_deepseek-flash/` 是换模型的对照实验留档，结论见 `HANDOFF.md` §4 坑 #14。
