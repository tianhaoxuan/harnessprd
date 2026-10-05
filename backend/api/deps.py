"""依赖注入。

用 `Annotated` 别名而不是在每个签名里写 `Depends(...)`：
- 路由函数签名干净，一眼看出依赖了什么
- 换实现（例如测试里替换 service）只需改这一处

当前服务都是无状态的轻量对象，每次请求新建一个即可。
将来若要复用模型客户端连接池，把工厂改成返回单例，路由代码不用动。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request

from core.config import Settings, get_settings
from services.conversation_service import ConversationService
from services.document_service import DocumentService
from services.document_version_service import DocumentVersionService
from services.job_service import JobService
from services.session_service import SessionService


def get_conversation_service() -> ConversationService:
    return ConversationService()


def get_document_service() -> DocumentService:
    return DocumentService()


def get_job_service(request: Request) -> JobService:
    """生成任务业务层。

    ⚠️ 与 `get_session_service` 同一条理由：**优先取 `app.state.job_service`**，
    否则 `create_app(settings)` 造的临时库会被绕过（`get_settings()` 是进程级单例），
    测试于是写到默认的 `backend/harnessprd.db` 上，互相污染且很难发现。
    """
    service = getattr(request.app.state, "job_service", None)
    if isinstance(service, JobService):
        return service
    return JobService()


def get_session_service(request: Request) -> SessionService:
    """会话业务层。

    ⚠️ **优先取 `app.state.session_service`，而不是每次 `SessionService()`**：
    `create_app(settings)` 允许传入自定义配置（测试就是这么用的），而 `get_settings()`
    是进程级单例 —— 在这里新建一个就会用**默认库路径**，于是"注入的临时库"被绕过，
    测试直接写到 `backend/harnessprd.db` 上（会互相污染，而且很难发现）。
    没有 `app.state` 时（例如不跑 lifespan 直接调路由）回落到新建一个。
    """
    service = getattr(request.app.state, "session_service", None)
    if isinstance(service, SessionService):
        return service
    return SessionService()


def get_document_version_service(request: Request) -> DocumentVersionService:
    """文档槽位与版本链业务层。

    ⚠️ 与 `get_session_service` / `get_job_service` 同一条理由：**优先取
    `app.state.document_version_service`**，否则 `create_app(settings)` 造的临时库会被绕过
    （`get_settings()` 是进程级单例），测试于是写到默认的 `backend/harnessprd.db` 上。
    """
    service = getattr(request.app.state, "document_version_service", None)
    if isinstance(service, DocumentVersionService):
        return service
    return DocumentVersionService()


SettingsDep = Annotated[Settings, Depends(get_settings)]
ConversationServiceDep = Annotated[ConversationService, Depends(get_conversation_service)]
DocumentServiceDep = Annotated[DocumentService, Depends(get_document_service)]
DocumentVersionServiceDep = Annotated[
    DocumentVersionService, Depends(get_document_version_service)
]
JobServiceDep = Annotated[JobService, Depends(get_job_service)]
SessionServiceDep = Annotated[SessionService, Depends(get_session_service)]
