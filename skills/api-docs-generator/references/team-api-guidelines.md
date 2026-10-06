<!-- 来源 docs/接口文档模板.md §2.3、§3.1–3.7、§4.1–4.3、§4.5；本节选已压缩。 -->
<!-- 切法：结构与校验点已在 gen_api.md，不重复；写作规则见 writing-rules.md。 -->

# 团队接口规范（RESTful 为默认主干）

## 1 写操作通用要求

改变服务端状态的接口（POST / PUT / PATCH / DELETE）必须明确：

| 问题 | 必须回答 |
| --- | --- |
| 重复调用会怎样 | 幂等性说明 + `Idempotency-Key` 用法 |
| 并发修改会怎样 | 乐观锁字段（如 `version`）或 `If-Match` 头 |
| 部分成功怎么办 | 整体回滚还是返回部分成功 |
| 是否触发异步任务 | 若是，返回 `202 Accepted` + 任务查询接口 |

## 2 URL 命名

| 规则 | ✅ | ❌ |
| --- | --- | --- |
| 用名词复数，不用动词 | `/weekly-reports` | `/getWeeklyReport` |
| 多词用 kebab-case | `/weekly-reports` | `/weekly_reports`、`/weeklyReports` |
| 资源层级不超过两层 | `/teams/{id}/members` | `/teams/{id}/projects/{pid}/tasks/{tid}` |
| 资源 ID 放路径，过滤条件放查询串 | `/reports/{id}`、`/reports?status=draft` | `/reports/status/draft` |
| 子资源表达归属，不表达操作 | `POST /reports/{id}/publish` | `POST /reports/{id}/doPublish` |

非 CRUD 的动作型操作（发布、归档、撤回）用 `POST /资源/{id}/动作名词`。

## 3 HTTP 方法语义

| 方法 | 语义 | 幂等 | 成功码 |
| --- | --- | --- | --- |
| GET | 查询 | ✅ | 200 |
| POST | 创建 / 动作 | ❌ | 201 / 200 / 202 |
| PUT | 全量替换 | ✅ | 200 / 204 |
| PATCH | 局部更新 | ❌ | 200 / 204 |
| DELETE | 删除 | ✅ | 204 |

不要用 POST 做查询（除非参数复杂到放不进 URL，那应重新设计资源）。

## 4 统一响应格式

成功直接返回资源，不套信封；失败统一用错误体（§8）。若团队已有信封规范可改，但必须全项目一致。

## 5 命名与格式

| 项 | 约定 |
| --- | --- |
| JSON 字段命名 | `snake_case`；Node / TypeScript 主导时改 `camelCase` |
| 时间 | ISO 8601 + UTC，如 `2025-01-13T02:15:00Z` |
| 金额 | 整数分（int），如 `1999` 表示 19.99 元 |
| 布尔 / 空值 | `true` / `false` 不用 0/1；无值返回 `null` 不省略字段（省略 = 不适用，`null` = 有概念无值） |

## 6 分页、排序、过滤、幂等、版本

| 项 | 约定 |
| --- | --- |
| 分页参数 | `page`（从 1 起）+ `page_size`，默认 20，上限 100 |
| 分页响应 | `items` / `page` / `page_size` / `total` |
| 排序 | `sort=field` / `sort=-field`（`-` 降序），白名单字段，不在白名单直接 422 |
| 过滤 | 平铺查询参数，如 `?status=draft&chat_id=xxx` |
| 幂等 | 写操作支持 `Idempotency-Key`；相同 Key 24 小时内返回首次结果 |
| 版本 | 路径前缀 `/api/v1/`；破坏性变更才升版本 |

## 7 三类条件约定（触发即不可省）

| 小节 | 触发条件 | 必须约定 |
| --- | --- | --- |
| 入站回调 Webhook | `integrations` 含支付 / 消息类服务 | 签名算法、重试策略与次数、幂等键、来源 IP 白名单、超时 |
| 文件上传 / 下载 | PRD 出现附件、头像、导入导出 | `multipart/form-data` 还是 Base64、大小上限、类型白名单、直传/中转 |
| 实时接口 | PRD 出现实时 / 推送要求 | SSE 还是 WebSocket、心跳间隔、断线重连策略、消息体格式 |

## 8 错误响应体

```json
{ "code": "RPT001", "message": "时间范围不能超过 31 天", "details": [ { "field": "range_end", "issue": "EXCEEDS_MAX_RANGE", "limit": 31 } ], "trace_id": "0af7651916cd43dd" }
```

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `code` | 是 | 业务错误码，全局唯一 |
| `message` | 是 | 给开发者定位问题的中文描述，不是终端用户文案 |
| `details` | 否 | 字段级错误明细；参数校验失败时必填 |
| `trace_id` | 是 | 链路 ID |

## 9 错误码格式

`{模块缩写}{3 位序号}`：模块缩写 3 个大写字母，取自 PRD 实体或功能域（`USR`/`AUTH`/`RPT`/`CHT`/`EXT`）；序号从 `001` 起；组合全局唯一。

分段不是硬要求，同一模块内按此顺序分配：`001–099` 参数校验｜`100–199` 鉴权权限｜`200–299` 业务规则冲突｜`300–399` 状态不允许｜`900–999` 系统与外部依赖。

## 10 HTTP 状态码与业务码

HTTP 码表达「哪一类」，业务码表达「具体哪一种」；一个 HTTP 码可对应多个业务码。

| HTTP | 何时用 | 典型业务码 |
| --- | --- | --- |
| 401 | 未登录 / 令牌过期 | `AUTH001` |
| 403 | 已登录但无权限 | `AUTH002`、`CHT001` |
| 404 | 资源不存在 | `RPT404` |
| 409 | 并发冲突 / 状态冲突 | `RPT301` |
| 422 | 参数合法但业务校验不过 | `RPT001`、`RPT002` |
| 429 | 触发限流 | `GEN429` |
| 500 | 服务端未预期错误 | `GEN500` |
| 502 / 503 | 外部依赖不可用 | `EXT001`、`GEN503` |

400 = 请求不合法（非合法 JSON、缺 Content-Type）；422 = 请求合法但业务规则不过（如跨度 > 31 天）。

## 11 接口必须考虑的错误

| 类别 | 何时适用 |
| --- | --- |
| 参数校验失败 | 有请求参数 |
| 未登录 | `need_auth` ≠ 不需要 |
| 无权限 | 存在角色差异 |
| 资源不存在 | 路径含资源 ID |
| 状态冲突 | 资源有状态字段 |
| 并发冲突 | 支持多端修改 |
| 外部依赖失败 | 依赖外部服务 |

## 12 非 RESTful 分支

`api_convention` 非 RESTful 时 §2–§3 作废（替换 URL 与请求响应形态），§4–§6 仍适用。

| `api_convention` | 替换方式 |
| --- | --- |
| GraphQL | 单入口 `POST /api/v1/graphql`；Query / Mutation 用「动词 + 名词」；字段级错误放 `errors[].extensions.code`，HTTP 一律 200；分页改 cursor |
| gRPC / RPC | 方法名 `Service.Method`；protobuf 定义请求响应；无 URL 概念；时间用 `google.protobuf.Timestamp` |
| RESTful + Session / OAuth 2.0 | §2–§3 不变，只改鉴权章 |
