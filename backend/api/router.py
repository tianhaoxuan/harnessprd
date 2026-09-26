"""路由聚合：main.py 只需挂这一个 router。

挂载后的接口面（`/api/v1` 前缀由 `main.py` 加，本文件的 prefix 都不含它）::

    /api/v1/
      ├── health                   健康检查（/health 与 /api/v1/health 同源）
      ├── sessions/*               会话生命周期、表单草稿、事件（唯一变更入口）、文档正文、对话历史
      ├── conversation/*           对话阶段：题目下发、首轮/接续流式对话（SSE）、
      │                            产物的流式生成与修订（SSE，4 条）
      └── config                   运行配置

⚠️ `/conversation/*` 里那 7 个 POST 都是**无状态补全**（不落状态、客户端自带上文），
与「唯一变更入口」的纪律不冲突但**必须随会话层一起改形** —— 见 `api/conversation.py`
的模块 docstring。其中 4 个生成接口还只是**前台流式**，不等于设计要求里的后台生成任务。

已删除的东西，别再往回加：

- 旧 `prd` 路由 —— 随 `system_prd.md` / `clarify.md` 删除（`HANDOFF.md` §7）
- 旧 `conversation/messages` 占位 —— 形状不对（以"对话消息"为中心 → 改为以"会话"为中心）
- `form` 模块 —— 它的 `/form/questions` 已并入 `/conversation/questions`
- `conversation/stream`（GET 占位）—— 被 `start-stream` / `continue-stream` 取代
"""

from __future__ import annotations

from fastapi import APIRouter

from api import config, conversation, health, sessions

api_router = APIRouter()

api_router.include_router(health.router, tags=["health"])
api_router.include_router(sessions.router, prefix="/sessions", tags=["sessions"])
api_router.include_router(conversation.router, prefix="/conversation", tags=["conversation"])
api_router.include_router(config.router, tags=["config"])
