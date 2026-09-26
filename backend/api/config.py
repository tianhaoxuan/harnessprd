"""运行配置路由 —— 占位。

⚠️ 实现这个接口时有一条硬约束：**只暴露白名单字段**。

不要写 ``settings.model_dump()`` —— ``SecretStr`` 在 model_dump 里会被解开成明文，
等于把 API Key 通过 HTTP 发出去。当前 /health 已经给出了安全的子集
（provider、模型名、是否已配 Key 的布尔值），需要更多就先往白名单里加。

仅返回 501 而不是当前配置，原因同上：宁可没数据，不可给错数据。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

router = APIRouter()

_NOT_IMPLEMENTED = (
    "配置接口尚未实现（占位）。实现时只暴露白名单字段，"
    "禁止直接序列化整个 Settings（会泄露密钥）。"
)


@router.get(
    "/config",
    summary="[占位] 运行配置",
    status_code=status.HTTP_501_NOT_IMPLEMENTED,
)
async def read_config() -> None:
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED, detail=_NOT_IMPLEMENTED
    )
