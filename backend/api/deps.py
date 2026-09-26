"""依赖注入。

用 `Annotated` 别名而不是在每个签名里写 `Depends(...)`：
- 路由函数签名干净，一眼看出依赖了什么
- 换实现（例如测试里替换 service）只需改这一处

当前服务都是无状态的轻量对象，每次请求新建一个即可。
将来若要复用模型客户端连接池，把工厂改成返回单例，路由代码不用动。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends

from core.config import Settings, get_settings
from services.conversation_service import ConversationService
from services.document_service import DocumentService


def get_conversation_service() -> ConversationService:
    return ConversationService()


def get_document_service() -> DocumentService:
    return DocumentService()


SettingsDep = Annotated[Settings, Depends(get_settings)]
ConversationServiceDep = Annotated[ConversationService, Depends(get_conversation_service)]
DocumentServiceDep = Annotated[DocumentService, Depends(get_document_service)]
