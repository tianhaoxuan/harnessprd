"""文档槽位与版本链接口（6 条）—— PRD / 接口文档 / 提示词套件的「槽位 + 版本链」。

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/api/session/{id}/documents` | 三种槽位摘要（尚无版本也返回，current 为 null） |
| GET | `.../documents/{doc_type}/versions` | 版本列表（**不含全文**，按 version_no 降序） |
| GET | `.../documents/{doc_type}/versions/{version_id}` | 单版详情（**含全文**，供预览） |
| POST | `.../documents/{doc_type}/versions/checkpoint` | 保存为新版本 |
| POST | `.../documents/{doc_type}/versions/{version_id}/restore` | 恢复此版本 |
| PATCH | `.../documents/{doc_type}/versions/{version_id}/note` | 补充 / 修改用户备注 |

## 路径为什么是 `/api/session/{id}/documents/*` 而不是 `/api/v1/sessions/*`

与 `api/session.py`、`api/jobs.py` 同一条理由：**需求把路径定成这样**，本模块照办。
router 自带前缀，`main.py` 里**不再加** `/api/v1`。

⚠️ 它挂在已有 `/api/session/*` 那一族的**下面**（`/api/session/{session_id}/documents`）。
两者不会抢路由：`/api/session/{id}` 是两段、本模块的路径都是三段以上，
而 `/api/session/list` 与本模块的 `/api/session/list/documents` 段数也不同。

## 本层只做三件事

1. **校验入参的形状**（Pydantic，`api/schemas.py`）；
2. **转发**给 `DocumentVersionService`；
3. 把领域模型**映射成对外视图**（`from_slot` / `from_record`）—— 哪些字段对外可见是
   传输层的决定，写在 `api/schemas.py` 里。

业务规则（`version_no` 怎么算、restore 的边界、什么时候升号）**全在**
`services/document_version_service.py`。在路由里再判一遍就是两份实现，改一处忘一处
不会有任何报错。

## `doc_type` 为什么不写成 `Literal`

写成 `Literal["prd", "api-docs", "prompts"]` 的话，非法取值会被 FastAPI 在**校验阶段**
拦成 **422**，而需求 §6 明确要求非法 `doc_type` → **400**（"对不存在的 doc_type 调 versions"）。
所以路径参数收 `str`，由服务层判枚举并抛 `InvalidDocumentRequest` → 400
（枚举清单仍然只有一份：`document_version_repository.DOC_TYPES`）。

## 不存在 / 不合法的语义

`DocumentNotFound` → **404**、`InvalidDocumentRequest` → **400**，
都由本模块导出的两个处理器统一转换（在 `main.py` 注册）——
与 `api/session.py` 的 `session_not_found_handler` 是同一个形状，
所以路由函数保持"纯转发"，不必每条都写 try/except。
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from api.deps import DocumentVersionServiceDep, SessionServiceDep
from api.schemas import (
    DocumentCheckpointRequest,
    DocumentCheckpointResponse,
    DocumentRestoreResponse,
    DocumentSlotListView,
    DocumentSlotView,
    DocumentVersionDetailView,
    DocumentVersionListView,
    DocumentVersionNoteRequest,
    DocumentVersionNoteResponse,
    DocumentVersionSummaryView,
)

router = APIRouter(prefix="/api/session/{session_id}/documents", tags=["documents"])


async def document_not_found_handler(_: Request, exc: Exception) -> JSONResponse:
    """版本 / 槽位 / 会话查不到 → **404**。

    注册在 `main.py`（FastAPI 的异常处理器只能挂在 app 上，挂不到 router 上）。
    """
    return JSONResponse(status_code=404, content={"detail": str(exc)})


async def invalid_document_request_handler(_: Request, exc: Exception) -> JSONResponse:
    """请求本身不合法 → **400**。

    四类：`doc_type` 不在枚举里、restore 的目标就是 current、restore 的历史版没有正文、
    checkpoint 时"既没有 current 也没带 content"（无内容可保存）。

    为什么是 400 而不是 422：请求的**形状**完全合法（`doc_type` 是个字符串、
    版本号是个整数），不合法的是它与当前数据状态的关系 —— 422 是"你传的东西格式不对"，
    400 是"你这个请求在当前状态下做不到"（与 `api/jobs.py` 用 409 表达"同产物已有任务在跑"
    是同一种区分）。
    """
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@router.get("", response_model=DocumentSlotListView, summary="三种文档槽位摘要")
async def list_documents(
    session_id: str, service: DocumentVersionServiceDep
) -> DocumentSlotListView:
    """返回三种 `doc_type` 的摘要，**顺序固定**（prd / api-docs / prompts）。

    尚无版本时的两个字段：

    - `current_version_id` / `current_version_no` 为 `null`；
    - `updated_at` 也为 `null`（**不是**槽位行的创建时间）。

    ⚠️ 首次调用会**建出三个空槽位行**（所以 `document_id` 有值）。
    这是需求 §6.1 的形状要求：侧栏永远能拿到稳定的三行、每行有自己的 `document_id`。
    """
    return DocumentSlotListView(
        items=[DocumentSlotView.from_slot(slot) for slot in service.get_documents_by_session(session_id)]
    )


@router.get(
    "/{doc_type}/versions",
    response_model=DocumentVersionListView,
    summary="版本列表（不含全文）",
)
async def list_versions(
    session_id: str, doc_type: str, service: DocumentVersionServiceDep
) -> DocumentVersionListView:
    """按 `version_no` **降序**返回版本列表，供侧栏使用。

    **不返回全文**，只给 `content_preview`（前 200 字）与用户备注 —— 一次列几十版时
    带上全文就是几十份 Markdown 白传；要看全文走单版详情。

    尚无版本 → `versions: []`（**不是 404**：没有版本是合法状态）。
    非法 `doc_type` → 400。
    """
    # 先取槽位身份：`current_version_id` 要用来标 `is_current`。
    slot = service.ensure_document(session_id, doc_type)
    versions = service.list_versions(session_id, doc_type)
    return DocumentVersionListView(
        doc_type=slot.doc_type,
        document_id=slot.id,
        current_version_id=slot.current_version_id,
        versions=[
            DocumentVersionSummaryView.from_record(
                record, current_version_id=slot.current_version_id
            )
            for record in versions
        ],
    )


@router.get(
    "/{doc_type}/versions/{version_id}",
    response_model=DocumentVersionDetailView,
    summary="单版详情（含全文）",
)
async def get_version(
    session_id: str, doc_type: str, version_id: str, service: DocumentVersionServiceDep
) -> DocumentVersionDetailView:
    """单版详情，**含全文 `content` 与 `metadata`**（`run_summary` / `review` / …）。

    版本不存在、或**不属于这个槽位** → 404（两种情况刻意不区分，见服务层说明）。
    """
    slot = service.ensure_document(session_id, doc_type)
    record = service.get_version(session_id, doc_type, version_id)
    return DocumentVersionDetailView.from_record(
        record, current_version_id=slot.current_version_id
    )


@router.post(
    "/{doc_type}/versions/checkpoint",
    response_model=DocumentCheckpointResponse,
    summary="保存为新版本",
)
async def checkpoint(
    session_id: str,
    doc_type: str,
    service: DocumentVersionServiceDep,
    sessions: SessionServiceDep,
    payload: DocumentCheckpointRequest | None = None,
) -> DocumentCheckpointResponse:
    """把当前正文固化成新的一版并设为 current（**旧版一律原样保留**）。

    请求体整个可选：

    - `{"content": "..."}` → 用前端编辑器里的全文（用户手改过、还没同步到服务端时用这个）；
    - `{}` 或**不带请求体** → 用服务端 current 版本的正文（只是打个标记）。

    **不收集备注**：那时候用户还没法引用"第几版"，备注由第 6 个接口事后写到任意版本上。

    边界：尚无 current 且没带 `content` → **400**（"无内容可保存"）；`doc_type` 非法 → 400；
    会话不存在 → 404。

    ⚠️ 成功后会把这个新 current 的正文**写回 `session_data` 镜像**（需求 §七）：
    checkpoint 改的是 current，而镜像（`documents.<kind>.content`）是前端的主读源，
    不跟着走的话"侧栏显示的版本"与"编辑器里的正文"会立刻不一致。
    这一步是**尽力而为**的（`SessionService.sync_document_content` 不抛）——
    版本已经写好了，不该为一个镜像写失败把接口判成 500。
    """
    record = service.checkpoint(
        session_id, doc_type, payload.content if payload is not None else None
    )
    sessions.sync_document_content(session_id, doc_type, record.content)
    return DocumentCheckpointResponse(
        version_id=record.id, version_no=record.version_no, document_id=record.document_id
    )


@router.post(
    "/{doc_type}/versions/{version_id}/restore",
    response_model=DocumentRestoreResponse,
    summary="恢复此版本",
)
async def restore_version(
    session_id: str,
    doc_type: str,
    version_id: str,
    service: DocumentVersionServiceDep,
    sessions: SessionServiceDep,
) -> DocumentRestoreResponse:
    """以历史版的正文**新建下一版**并设为 current。

    **不修改、不删除任何历史行**：恢复是"往前走一步"，不是"把指针退回去"。
    所以 current=v2 时恢复 v1 会得到 v3（正文与 v1 逐字相同），而 v1、v2 仍原样在列表里 ——
    用户之后还能再恢复回 v2。

    请求体：无（或预留空对象）。要恢复哪一版已经在路径里了。

    边界：目标是 current → **400**；历史版没有正文 → **400**；版本不属于本槽位 → 404。

    响应比 checkpoint 多一个 `content`，供 Session 镜像同步（前端拿到就能直接刷新编辑器）。

    ⚠️ 与 checkpoint 一样，成功后把新 current 的正文写回镜像（尽力而为）。
    需求 §八 的改动清单只点了 checkpoint，但 restore **同样会切换 current** ——
    漏掉它等于每次恢复都制造一次"侧栏 v3 / 编辑器还是旧正文"的不一致。
    """
    record = service.restore_version(session_id, doc_type, version_id)
    sessions.sync_document_content(session_id, doc_type, record.content)
    return DocumentRestoreResponse(
        version_id=record.id,
        version_no=record.version_no,
        document_id=record.document_id,
        content=record.content,
    )


@router.patch(
    "/{doc_type}/versions/{version_id}/note",
    response_model=DocumentVersionNoteResponse,
    summary="补充 / 修改用户备注",
)
async def update_version_note(
    session_id: str,
    doc_type: str,
    version_id: str,
    service: DocumentVersionServiceDep,
    payload: DocumentVersionNoteRequest | None = None,
) -> DocumentVersionNoteResponse:
    """给**任意**一版（历史版或 current）写用户备注 → `metadata.change_note`。

    **不改正文、不升 `version_no`**。传空字符串或省略 `change_note` = **清空**备注
    （键会被删掉，不是留一个空串 —— 前端于是可以统一用"有没有这个键"判断）。

    版本不属于本槽位 → 404；`doc_type` 非法 → 400；会话不存在 → 404。
    """
    record = service.update_version_note(
        session_id,
        doc_type,
        version_id,
        payload.change_note if payload is not None else None,
    )
    return DocumentVersionNoteResponse(
        version_id=record.id, version_no=record.version_no, change_note=record.change_note()
    )
