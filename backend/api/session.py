"""会话存储接口（4 条）—— 工作台快照的存取。

## 为什么路径是 `/api/session/*` 而不是 `/api/v1/sessions/*`

需求把路径定成 `/api/session/{list, save, {id}}`，本模块**照办**：router 自带前缀
`/api/session`，在 `main.py` 里**不再加** `/api/v1`。

它与 `/api/v1/sessions/*` 那组**不是一回事**，两组并存、**不要合并**：

| 路径 | 是什么 | 现状 |
| --- | --- | --- |
| `/api/v1/sessions/*` | 设计里以「会话」为中心的接口面（`events` 唯一变更入口、表单草稿、文档正文） | 占位 501，契约未定 |
| `/api/session/*` | 本次要的**会话快照存储**：把整份工作台 state 存/取 | 已实现（本文件） |

名字接近是需求给定的，区别在"存整份快照"还是"按状态机改某一个字段"。

## 本层只做两件事

1. **校验入参**：形状与取值交给 Pydantic（`api/schemas.py` 的 4 个模型），
   业务合法性（快照必须是 JSON 对象、枚举取值）交给 Service；
2. **转发**给 `SessionService`。

降级规则、标题解析、"不存在"的语义**全在** `services/session_service.py` ——
在路由里再判一遍就是两份实现，改一处忘一处不会有任何报错。

`SessionNotFound` → **404** 由 `session_not_found_handler` 统一转换
（在 `main.py` 注册），所以路由里不需要写 try/except。
"""

from __future__ import annotations

import json
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from api.deps import SessionServiceDep
from api.schemas import (
    SessionDeleteResponse,
    SessionSaveRequest,
    SessionSaveResponse,
    SessionStoreListView,
)
from services.delivery_export_service import build_delivery_zip, build_lineage
from services.session_models import SessionLoad
from services.session_service import SessionNotFound

router = APIRouter(prefix="/api/session", tags=["session"])


async def session_not_found_handler(_: Request, exc: Exception) -> JSONResponse:
    """把 `SessionNotFound` 转成 **404**。

    注册在 `main.py`（FastAPI 的异常处理器只能挂在 app 上，挂不到 router 上）。
    这样路由函数保持"纯转发"，不用每条都写一遍 try/except。
    """
    return JSONResponse(status_code=404, content={"detail": str(exc)})


@router.get("/list", response_model=SessionStoreListView, summary="会话摘要列表")
async def list_sessions(
    service: SessionServiceDep,
    limit: int = 50,
    offset: int = 0,
) -> SessionStoreListView:
    """按 `updated_at` 倒序返回摘要。**不含 `session_data`** —— 列表页不该拉整份快照。

    `limit` 由 Service 夹到 1..200（列表接口不该被拿来导出全库）。
    """
    return SessionStoreListView(items=service.list_sessions(limit=limit, offset=offset))


@router.get("/{id}", response_model=SessionLoad, summary="查询单条会话（含完整快照）")
async def get_session(id: str, service: SessionServiceDep) -> SessionLoad:
    """取完整会话。**若 `viewState` 是 `generating-*`，先降级再返回**（规则与工作台一致）。

    响应里额外带两条：`downgraded_from`（原状态）与 `interrupted_kind`（被打断的是哪份产物）——
    调用方据此提示「上次生成被刷新打断了」，而不是靠猜。

    ⚠️ 路径参数就叫 `id`（按需求给定的路径原文），所以响应里的主键字段也是 `id`。
    """
    return service.get_session(id)


@router.post("/save", response_model=SessionSaveResponse, summary="新增 / 更新会话")
async def save_session(
    payload: SessionSaveRequest,
    service: SessionServiceDep,
) -> SessionSaveResponse:
    """`id` 为空 → 新建并返回新 id；给了 `id` → 更新该记录并返回同一个 id。

    **两种情况都只回 `{id, title}`**（需求如此）：调用方不需要再查一次就能拿到落库的标题。
    更新时**标题不变** —— 用户可能手工改过，而且更新时正文常常还没写完。
    """
    result = service.save_session(payload.session_data, payload.id)
    return SessionSaveResponse(id=result.id, title=result.title)


@router.delete("/{id}", response_model=SessionDeleteResponse, summary="删除会话")
async def delete_session(id: str, service: SessionServiceDep) -> SessionDeleteResponse:
    """删除指定会话。**不存在 → 404**（由 `session_not_found_handler` 统一转换）。"""
    service.delete_session(id)
    return SessionDeleteResponse(ok=True)


@router.get("/{id}/artifact-lineage", summary="三份产物的版本与上游关系（05 篇）")
async def artifact_lineage(id: str, service: SessionServiceDep) -> dict:
    """给审核页算"下游产物是否过期"用。

    为什么不让前端自己算：版本列表接口是按 `doc_type` 一次查一份的，站在接口文档页时
    前端手上**没有 PRD 的当前版本号** —— 那正是比较所需的另一半。这里一次给全，
    并把过期文案也一起给（文案只在后端一处实现，与交付包里的提示一字不差）。

    返回：`{session_id, documents: {prd|api_docs|prompts: {...}}, stale: {api_docs|prompts: {stale, message}}}`
    """
    session = service.get_session(id)
    snapshot = json.loads(session.session_data or "{}")
    return build_lineage(documents=service.documents, session_id=id, snapshot=snapshot)


@router.get(
    "/{id}/export",
    summary="导出交付包（ZIP）",
    response_class=Response,
    responses={200: {"content": {"application/zip": {}}, "description": "交付包 ZIP 字节流"}},
)
async def export_delivery(id: str, service: SessionServiceDep) -> Response:
    """把三份产物打成一个交付包：`README.md` + `manifest.json` + 三份 md（+ `prompts/` 拆分）。

    为什么是 **GET** 而不是 POST：它**不改任何状态**（只读版本层与会话快照），
    产出是一个文件。GET 还能让"在新标签页打开这个地址"直接触发下载。

    ⚠️ 组包**全在服务层**（`services/delivery_export_service.py`），这里只做两件事：
    取会话快照、把字节和文件名塞进响应。路由里不算业务（同本模块其他四条）。

    ⚠️ 文件名可能含中文（`{产品名}-delivery.zip`），所以按 RFC 5987 写成
    `filename*=UTF-8''...`，同时留一个 ASCII 的 `filename=` 兜底 —— 老浏览器只认后者，
    只给 `filename*=UTF-8''` 的话它会用一个乱码名字保存。
    """
    session = service.get_session(id)
    snapshot = json.loads(session.session_data or "{}")
    data, file_name = build_delivery_zip(
        documents=service.documents, session_id=id, snapshot=snapshot
    )
    return Response(
        content=data,
        media_type="application/zip",
        headers={
            "Content-Disposition": (
                'attachment; filename="harnessprd-delivery.zip"; '
                f"filename*=UTF-8''{quote(file_name)}"
            ),
            # 交付包里有 manifest 的版本号，缓存住会让用户拿到旧的
            "Cache-Control": "no-store",
        },
    )
