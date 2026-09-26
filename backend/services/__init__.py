"""services —— 业务逻辑层。

- `llm.py`                  厂商差异的唯一收敛点（LangChain ChatModel 工厂）
- `state.py`                会话状态的**内部**契约（枚举；完整状态模型待实现）
- `conversation_service.py` 对话阶段编排（澄清 S0–S5，已实现）
- `document_service.py`     三份产物的生成 / 优化（已实现）+ 审核 / 质检（**占位**）

**依赖方向单向**：`api → services → core`。

- 本层不 import `api.*`（服务层被路由调用，不反过来）
- 本层不 import `fastapi`（输入输出是 Pydantic 模型 / dataclass，可被 CLI、批处理、测试直接复用）

反过来的复用是允许的：`api/schemas.py` 会 import 本层的枚举，避免两边各写一份。
"""
