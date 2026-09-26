"""日志配置：应用启动时调用一次即可。"""

from __future__ import annotations

import logging
import sys

_LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# 这些库在 INFO 级别噪音很大，压到 WARNING，避免淹没业务日志
_NOISY_LOGGERS = ("httpx", "httpcore", "urllib3", "asyncio", "watchfiles", "multipart")


def configure_logging(level: str | int = "INFO") -> None:
    """配置根 logger。

    用 ``force=True`` 覆盖已有 handler，因此可安全重复调用
    （uvicorn --reload 重载、测试中多次 create_app 都不会日志翻倍）。
    """
    resolved: int | str = level
    if isinstance(level, str):
        resolved = logging.getLevelName(level.strip().upper())
    if not isinstance(resolved, int):
        resolved = logging.INFO

    logging.basicConfig(
        level=resolved,
        format=_LOG_FORMAT,
        datefmt=_DATE_FORMAT,
        stream=sys.stdout,
        force=True,
    )
    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
