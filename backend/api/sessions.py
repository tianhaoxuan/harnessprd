"""会话（session）路由 —— 占位。

**路径形状取自 `docs/会话持久化方案.md`：以「会话」为中心，而不是以「对话消息」为中心。**
设计里所有状态变更只有**一条入口** —— ``POST /sessions/{id}/events``（对应 `apply_event`），
其余接口要么是读取，要么是表单草稿的增量保存。

| 方法 | 路径 | 用途 | 出处 |
| --- | --- | --- | --- |
| POST | `/sessions` | 创建会话 | `docs/状态数据设计.md` §4.1 `create()` |
| GET | `/sessions` | 会话列表（`states` / `limit` / `cursor`） | 同上 `list()` |
| GET | `/sessions/{id}` | 会话快照 `SessionSnapshot`（**含 `actions`**） | 同上 §4.3 |
| PUT | `/sessions/{id}/form` | 表单草稿（前端防抖 800ms 自动保存） | `docs/会话持久化方案.md` §5.1 |
| POST | `/sessions/{id}/events` | **唯一的状态变更入口** | 同上 §7.2、`状态数据设计` §4.2 |
| GET | `/sessions/{id}/documents/{kind}` | 文档正文（**刻意不进快照**，正文可能上万字符） | `状态数据设计` §4.3 |

⚠️ 路径用 `/sessions` 而非 `docs/功能清单.md` F1.2 写的 `/conversation/sessions`：
以详细定义了数据契约与接口面的那两份文档为准，且 `docs/接口文档模板.md` §3.1 的原则是
「路径不带版本以外的前缀」。已在 `功能清单.md` 同步更正，见 `HANDOFF.md`。

当前一律返回 **501**，不返回编造的假数据 —— 假数据会被前端当成"已实现"。
**响应模型等实现时再定义**：契约在 `docs/状态数据设计.md` §4.3 的 `SessionSnapshot`，
且「快照是否带 `actions`」等仍属该文档 §8 的待确认项。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

# 对外契约模型统一放 api/schemas.py（单一来源）。枚举本身定义在 services/state.py，
# 由 schemas 复用 —— 依赖方向仍是 api → services → core。
from api.schemas import DocKind, EventRequest

router = APIRouter()

_NOT_IMPLEMENTED = (
    "会话接口尚未实现（占位）。数据契约见 docs/状态数据设计.md §4.3 的 SessionSnapshot；"
    "状态推进与刷新恢复见 docs/会话持久化方案.md §7。"
)


def _todo() -> None:
    raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail=_NOT_IMPLEMENTED)


@router.post("", summary="[占位] 创建会话", status_code=status.HTTP_501_NOT_IMPLEMENTED)
async def create_session() -> None:
    """创建会话。

    `form_version` / `prompt_version` 由**服务端**按当前配置写入，不由客户端指定
    （否则客户端可以伪造版本锚点，历史会话的解释规则就失效了）。
    """
    _todo()


@router.get("", summary="[占位] 会话列表", status_code=status.HTTP_501_NOT_IMPLEMENTED)
async def list_sessions(
    states: str | None = None,
    limit: int = 20,
    cursor: str | None = None,
) -> None:
    """会话列表。`states` 为逗号分隔的状态过滤；`cursor` 为分页游标。"""
    _todo()


@router.get(
    "/{session_id}",
    summary="[占位] 会话快照",
    status_code=status.HTTP_501_NOT_IMPLEMENTED,
)
async def get_session(session_id: str) -> None:
    """返回 `SessionSnapshot`：进度、表单、对话摘要、各文档状态、当前失败、以及 `actions`。

    `actions` 是关键一环 —— 前端**不自己判断"现在能不能点通过"**，直接用服务端给的动作
    列表渲染按钮，这样转换表是唯一真相（`docs/状态数据设计.md` §4.3）。
    """
    _todo()


@router.put(
    "/{session_id}/form",
    summary="[占位] 保存表单草稿",
    status_code=status.HTTP_501_NOT_IMPLEMENTED,
)
async def save_form_draft(session_id: str, form: dict[str, str]) -> None:
    """表单草稿保存（前端防抖 800ms 调用）。

    刻意走服务端而不是只写 localStorage：表单是用户最核心的输入，本地存储在
    多标签页 / 清缓存 / 换设备时都不可靠。localStorage 只作同步失败时的降级兜底。
    """
    _todo()


@router.post(
    "/{session_id}/events",
    summary="[占位] 提交状态变更事件（唯一入口）",
    status_code=status.HTTP_501_NOT_IMPLEMENTED,
)
async def apply_event_route(session_id: str, request: EventRequest) -> None:
    """**所有状态变更的唯一入口**。转换表校验与 guard 必须在这里硬编码执行。

    前端的按钮显隐是体验问题，服务端的校验是正确性问题 —— 绕过这个入口的写入
    等于绕过转换表（`docs/状态数据设计.md` §4.2）。

    进入 `*_generating` 时必须**派发后台任务后立即返回**，不能把生成绑在 HTTP 请求上：
    否则用户刷新会 abort 请求，生成结果无人接收（`docs/会话持久化方案.md` §7.1）。
    """
    _todo()


@router.get(
    "/{session_id}/documents/{kind}",
    summary="[占位] 取文档正文",
    status_code=status.HTTP_501_NOT_IMPLEMENTED,
)
async def get_document(session_id: str, kind: DocKind) -> None:
    """按产物类型取 Markdown 正文。

    正文**不进快照** —— 一份 PRD 可能上万字符，每次拉快照都带上会白白浪费带宽。
    """
    _todo()


@router.get(
    "/{session_id}/messages",
    summary="[占位] 取对话历史",
    status_code=status.HTTP_501_NOT_IMPLEMENTED,
)
async def get_messages(session_id: str, limit: int = 50, cursor: str | None = None) -> None:
    """取某个会话的对话消息。

    放在会话资源下（而不是 `/conversation/...`）：`messages` 是**会话的子资源**
    —— `docs/状态数据设计.md` §4.3 说它「单独分页接口获取」，没给它独立命名空间。

    刻意不放进 `SessionSnapshot`：快照每 2 秒轮询一次（`会话持久化方案` §7.5），
    带上完整对话历史是纯浪费。
    """
    _todo()
