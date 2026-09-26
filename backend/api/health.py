"""健康检查与运行时自述。"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, Field

from api.deps import SettingsDep
from core.prompts import available_prompts

router = APIRouter()


class HealthResponse(BaseModel):
    status: str = "ok"
    app: str
    version: str
    environment: str
    llm_provider: str
    llm_model: str
    llm_configured: bool = Field(
        description="当前 provider 是否已配置 API Key（只检查配置，不代表连通）"
    )
    prompts: list[str] = Field(description="已加载的提示词资源")


@router.get("/health", response_model=HealthResponse, summary="健康检查")
async def health(settings: SettingsDep) -> HealthResponse:
    """进程存活与运行时配置自述。

    刻意不返回任何密钥值，只返回"是否已配置"。

    注意：这里不探测 Postgres/Redis —— 数据层虽已用 docker-compose 就绪，
    但业务代码尚未接入，硬加一个必然失败的探针只会制造噪音。
    """
    return HealthResponse(
        app=settings.app_name,
        version=settings.version,
        environment=settings.environment,
        llm_provider=settings.llm_provider,
        llm_model=settings.active_llm_model,
        llm_configured=settings.llm_configured,
        prompts=available_prompts(),
    )
