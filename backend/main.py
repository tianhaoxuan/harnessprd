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
    /api/v1/conversation/*   对话阶段：题目下发（**已实现**）、流式回复与产物生成（**已实现**）
    /api/v1/sessions/*       会话生命周期 / 表单草稿 / 事件（唯一变更入口）/ 文档 / 对话历史（占位，501）
    /api/session/*           会话**快照存储**：list / {id} / save / delete（**已实现**，4 条）
    /api/session/{id}/documents/*  文档槽位与**版本链**：槽位摘要 / 版本列表 / 单版详情 /
                             checkpoint / restore / 备注（**已实现**，6 条）
    /api/jobs/*              生成任务（Generation Job）：创建 / 快照 / 订阅 SSE（**已实现**，3 条）
    /api/v1/config           运行配置（占位，501）

⚠️ `/api/session/*` **刻意不带版本前缀**：路径是需求给定的，而它与 `/api/v1/sessions/*`
那组不是一回事（一组是"存整份工作台快照"，一组是设计里的状态机接口面）。见 `api/session.py`。
文档版本接口（`api/documents.py`）挂在同一族的下一层，理由相同。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.documents import (
    document_not_found_handler,
    invalid_document_request_handler,
)
from api.documents import router as documents_router
from api.health import router as health_router
from api.jobs import router as jobs_router
from api.router import api_router
from api.session import router as session_router
from api.session import session_not_found_handler
from core.config import Settings, get_settings
from core.logging import configure_logging
from fastapi import Request
from fastapi.responses import JSONResponse
from services.document_version_service import (
    DocumentNotFound,
    DocumentVersionService,
    InvalidDocumentRequest,
)
from services.job_runner import shutdown as shutdown_job_runner
from services.job_service import DuplicateRunningJob, JobNotFound, JobService
from services.token_estimator import BudgetExceededError

from middleware.request_id import REQUEST_ID_HEADER, RUN_ID_HEADER, RequestIdMiddleware
from services.session_service import SessionNotFound, SessionService

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
    # 方案库/会话库（同一个 SQLite 文件）：**启动即建表**，幂等。
    #
    # 为什么放在 lifespan 而不是 `create_app()`：`create_app()` 在**导入期**也会被调用
    # （模块末尾那句 `app = create_app()`），导入时就往磁盘落一个库文件不合适 ——
    # 测试导入、打包、只读环境都会碰到它。lifespan 只在"进程真的要开始服务"时跑。
    session_service: SessionService = app.state.session_service
    session_service.ensure_ready()
    # 生成任务表（同一库文件里的第二张表）。**启动即建表**，与方案表一起。
    job_service: JobService = app.state.job_service
    job_service.ensure_ready()
    # 文档槽位与版本链表（同一库文件里的第三、四张表）。与上面两张一起建。
    # ⚠️ **必须在这里也建**：`SessionService.ensure_ready()` 只负责它自己读到的那两张
    # （plans / generation_jobs），版本表若漏建，报错点是第一次访问版本接口时的
    # `no such table: documents` —— 离"少调了一次 ensure_ready"这个原因很远。
    document_versions: DocumentVersionService = app.state.document_version_service
    document_versions.ensure_ready()
    # 上一轮进程留下的 pending / running 任务永远不会有 runner 接管了（`asyncio.create_task`
    # 活不过重启），所以**启动时如实标记中断**，而不是让界面永远显示"生成中"。
    _stale = job_service.fail_stale_jobs()
    if _stale:
        logger.warning("服务重启：%d 个未完成的生成任务已标记为 failed", len(_stale))
    if not settings.llm_configured:
        # 明确告知"能起来但调不通"，避免把 503 误判成代码 bug
        logger.warning(
            "未检测到 %s 的 API Key，需要 LLM 的接口将返回 503；"
            "请在 backend/.env 中配置（可从 .env.example 复制）",
            settings.llm_provider,
        )
    yield
    # 关停：取消在跑的生成任务。收尾（标记 failed）留给**下次启动**做 ——
    # 取消点可能落在任何 await 上，此时写库发事件都不可靠（见 services/job_runner.shutdown）。
    shutdown_job_runner()
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
    # 会话业务层（API 直接调它）。建表在 lifespan 里做，见上面的说明。
    # 挂在 `app.state` 上而不是模块级单例：`create_app(settings)` 可以造多个独立实例
    # （测试就是这么用的），模块级单例会让它们共用一条库文件。
    #
    # 任务层先建、再注入会话层：会话层要用它回答"这个 job 还在跑吗"（降级判断）。
    # 顺序反过来的话，会话层会自己造一个 JobService（同一个库路径，功能上没问题，
    # 但测试注入的临时库就只对其中一半生效 —— 那正是最难查的一类污染）。
    app.state.job_service = JobService(settings)
    app.state.session_service = SessionService(settings, jobs=app.state.job_service)
    # 文档槽位与版本链（api/documents.py）。建表同样在 lifespan 里做。
    # 与另外两个一样挂在 `app.state` 上，好让 `create_app(settings)` 造的临时库
    # 对**所有**依赖注入生效（`get_settings()` 是进程级单例，绕过 app.state 就写错库）。
    app.state.document_version_service = DocumentVersionService(settings)

    # 请求工号：先于业务中间件注册，让**所有**响应都带上 X-Request-ID，
    # 并让后续任意深度的 LLM 调用都能从上下文读到它。
    app.add_middleware(RequestIdMiddleware)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        # `*` = 放行预检里 `Access-Control-Request-Headers` 列出的**所有**头，
        # 所以分片观测头 `X-Run-ID` / `X-Part-Index` / `X-Part-Total` 不会被预检拦掉。
        # ⚠️ 改成白名单时**必须**把这三个头加进去：浏览器对自定义请求头的预检失败
        # 表现为"请求根本没发出去"，而服务端日志里一个字都不会有。
        allow_headers=["*"],
        # 浏览器默认**读不到**自定义响应头：不暴露的话前端拿不到回显的工号
        # （`X-Request-ID` / `X-Run-ID` 都在响应头里，供用户报错时给我们查日志）。
        expose_headers=[REQUEST_ID_HEADER, RUN_ID_HEADER],
    )

    # 带版本号的业务接口
    app.include_router(api_router, prefix=API_PREFIX)

    # 会话快照存储（4 条）——**路径不在 /api/v1 下**，router 自带 `/api/session` 前缀。
    # 原因见 `api/session.py`：它与 `/api/v1/sessions/*` 那组占位接口不是一回事。
    app.include_router(session_router)

    # 文档槽位与版本链（6 条）——挂在快照存储那一族的下一层
    # （`/api/session/{id}/documents/*`，router 自带前缀），所以同样不加 /api/v1。
    app.include_router(documents_router)

    # 生成任务（3 条，`/api/jobs/*`）——同样**不在 /api/v1 下**（需求给定的路径），
    # router 自带前缀。这是「生成必须是后台任务」的落地入口：POST 只登记任务并返回
    # job_id，执行在后台协程里跑，断开 SSE 不影响它。
    app.include_router(jobs_router)

    # 会话不存在的语义是 **404**。异常处理器只能挂在 app 上（挂不到 router 上），
    # 所以在这里注册；路由函数因此保持"纯转发"，不必每条都写 try/except。
    app.add_exception_handler(SessionNotFound, session_not_found_handler)
    # 文档版本接口的两类失败：查不到 → 404、请求在当前状态下做不到 → 400。
    # 两个处理器定义在 `api/documents.py`（与 `session_not_found_handler` 同一形状），
    # 因为 FastAPI 的异常处理器只能挂在 app 上、挂不到 router 上。
    app.add_exception_handler(DocumentNotFound, document_not_found_handler)
    app.add_exception_handler(InvalidDocumentRequest, invalid_document_request_handler)

    async def job_not_found_handler(_: Request, exc: Exception) -> JSONResponse:
        """任务不存在 → **404**（与 `SessionNotFound` 同口径）。"""
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    async def duplicate_job_handler(_: Request, exc: Exception) -> JSONResponse:
        """同一会话 + 同一产物已有在跑的任务 → **409 Conflict**。

        为什么是 409 而不是 400 / 422：请求本身完全合法，冲突的是**当前资源状态**
        （另一次生成正在写同一份产物）。409 正好表达这个语义，客户端也能据此
        提示"已经有一个在跑了"而不是"参数错了"。
        """
        return JSONResponse(
            status_code=409,
            content={
                "detail": str(exc),
                "type": "JOB_ALREADY_RUNNING",
                "job_id": getattr(exc, "running_id", None),
            },
        )

    app.add_exception_handler(JobNotFound, job_not_found_handler)
    app.add_exception_handler(DuplicateRunningJob, duplicate_job_handler)

    async def budget_exceeded_handler(_: Request, exc: Exception) -> JSONResponse:
        """JSON 接口超预算统一 **422**（SSE 走 error 事件，见 api/conversation.py）。"""
        check = getattr(exc, "check", None)
        return JSONResponse(
            status_code=422,
            content={
                "detail": check.message if check is not None else str(exc),
                "type": "TOKEN_BUDGET_EXCEEDED",
                "context_usage": check.to_context_usage() if check is not None else None,
            },
        )

    app.add_exception_handler(BudgetExceededError, budget_exceeded_handler)

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
