"""harnessprd backend —— 应用入口。

启动方式（任选其一）::

    # 在 backend/ 目录下（推荐，带热重载）
    .venv\\Scripts\\python -m uvicorn main:app --reload

    # 在仓库根目录下
    .venv\\Scripts\\python -m uvicorn main:app --app-dir backend --reload

    # 直接跑本文件（监听 settings.host:settings.port，无热重载）
    .venv\\Scripts\\python main.py

接口文档：http://127.0.0.1:8000/docs

路径规划（完整清单见 `backend/README.md`）::

    /                        服务自述
    /health                  健康检查（根路径，给探针 / 负载均衡用）
    /api/v1/health           同上，带版本号的正式路径
    /api/v1/conversation/*   对话阶段：题目下发（**已实现**）、流式回复（占位，501）
    /api/v1/sessions/*       会话生命周期 / 表单草稿 / 事件（唯一变更入口）/ 文档 / 对话历史（占位，501）
    /api/v1/config           运行配置（占位，501）
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.health import router as health_router
from api.router import api_router
from core.config import Settings, get_settings
from core.logging import configure_logging

logger = logging.getLogger(__name__)

API_PREFIX = "/api/v1"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """启动/停止钩子。

    数据库连接池、Redis 客户端将来在这里建立和释放。
    """
    settings: Settings = app.state.settings
    logger.info(
        "%s v%s 启动 | env=%s | 配置监听=%s:%s（实际绑定以 uvicorn 那行日志为准）"
        " | LLM=%s/%s | key_configured=%s",
        settings.app_name,
        settings.version,
        settings.environment,
        settings.host,
        settings.port,
        settings.llm_provider,
        settings.active_llm_model,
        settings.llm_configured,
    )
    if not settings.llm_configured:
        # 明确告知"能起来但调不通"，避免把 503 误判成代码 bug
        logger.warning(
            "未检测到 %s 的 API Key，需要 LLM 的接口将返回 503；"
            "请在 backend/.env 中配置（可从 .env.example 复制）",
            settings.llm_provider,
        )
    yield
    logger.info("%s 已停止", settings.app_name)


def create_app(settings: Settings | None = None) -> FastAPI:
    """应用工厂。支持传入 settings，便于测试构造独立配置的实例。"""
    settings = settings or get_settings()
    configure_logging(settings.log_level)

    app = FastAPI(
        title=settings.app_name,
        version=settings.version,
        debug=settings.debug,
        lifespan=lifespan,
        summary="需求 → PRD 的生成式服务",
        description=(
            "一句话需求生成结构化 PRD，并支持多轮追问迭代。\n\n"
            "当前为骨架版本：conversation / config 为占位接口，"
            "数据层（Postgres/Redis）已就绪但尚未接入代码。"
        ),
    )
    app.state.settings = settings

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # 带版本号的业务接口
    app.include_router(api_router, prefix=API_PREFIX)

    # 根路径 /health：探针/负载均衡用的稳定路径（不带版本号）。
    # 复用 api.health 里同一个 handler，不复制实现，避免两份逻辑随时间长歪。
    app.include_router(health_router, tags=["health"])

    @app.get("/", tags=["meta"], summary="服务自述")
    async def root() -> dict[str, str]:
        return {
            "name": settings.app_name,
            "version": settings.version,
            "docs": "/docs",
            "health": "/health",
            "api_health": f"{API_PREFIX}/health",
        }

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    _settings = get_settings()
    # 传 app 对象而不是 "main:app" 字符串：不依赖当前工作目录在不在 sys.path。
    # 代价是这里没法同时开 reload —— 要热重载请用上面命令行的 uvicorn main:app --reload。
    logger.info(
        "监听 http://%s:%s（接口文档 http://127.0.0.1:%s/docs）",
        _settings.host,
        _settings.port,
        _settings.port,
    )
    uvicorn.run(app, host=_settings.host, port=_settings.port)
