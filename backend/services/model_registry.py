"""模型物理参数注册表：读 `config/models.yaml`，解析别名，给出物理顶。

## 它解决什么

`max_tokens` 与 budget 的物理 input 顶必须来自**同一个数**。以前 `max_tokens` 是全局
`LLM_MAX_TOKENS`（4096/8192），与具体模型无关 —— 换模型不会跟着变，而且 budget 若另算一份
就会出现"日志说顶是 995904、实际 API 顶是 8192"这种自相矛盾。收口到注册表之后：
**改一处，两处同步**（验收标准里明确要求改 yaml 后 API 输出顶与物理 input 顶一起变）。

## 为什么不问厂商 API

多数厂商的 `/v1/models` 不返回可靠的 `context_window` / `max_output_tokens`。拿不到就得回退，
于是"有时准有时不准"—— 不如人工维护一份清单。

## 未知模型怎么办

走 `default` 并打 `warning`。**宁可低估窗口**（多几次 warn），也不要高估 —— 高估会直接在
厂商侧 400，而那是用户可见的失败。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from core.config import BACKEND_DIR

logger = logging.getLogger(__name__)

REGISTRY_PATH = BACKEND_DIR / "config" / "models.yaml"
DEFAULT_KEY = "default"
SAFETY_MARGIN = 0
"""物理顶再留的余量（token）。默认 0：窗口与输出顶之间已经有空间，多留会让人以为配错了。"""


@dataclass(frozen=True)
class ModelCaps:
    """一个模型的物理参数。"""

    name: str
    context_window: int
    max_output_tokens: int
    encoding: str = "cl100k_base"
    provider: str = "openai_compatible"

    @property
    def hardware_input_cap(self) -> int:
        """先给输出留位置，剩下的才是 input 上限。"""
        return max(self.context_window - self.max_output_tokens - SAFETY_MARGIN, 1)


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        import yaml  # 局部导入：没装 PyYAML 时下面会给出可执行的报错
    except ModuleNotFoundError as exc:  # pragma: no cover - 环境问题
        raise RuntimeError(
            "缺少 PyYAML，无法读取 config/models.yaml。请先 `pip install pyyaml tiktoken`"
        ) from exc
    with path.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


@lru_cache(maxsize=1)
def _raw_models(path: str = str(REGISTRY_PATH)) -> dict[str, dict[str, Any]]:
    """读一次缓存住。测试可 `_raw_models.cache_clear()` 后换路径。"""
    data = _read_yaml(Path(path))
    models = data.get("models")
    if not isinstance(models, dict) or DEFAULT_KEY not in models:
        raise RuntimeError(f"{path} 里没有 models.{DEFAULT_KEY} —— 缺了兜底项，未知模型会无参数可用")
    return models


def known_models() -> list[str]:
    return sorted(_raw_models())


def resolve(model_name: str | None) -> ModelCaps:
    """按名字解析（支持 `alias_of`）；未知模型走 `default` 并告警。"""
    models = _raw_models()
    name = (model_name or "").strip()
    entry = models.get(name)
    if entry is None:
        if name:
            logger.warning(
                "模型 %s 不在 config/models.yaml 里，按 default 的保守参数处理"
                "（窗口 %s / 输出顶 %s）；要精确请补一条注册表",
                name,
                models[DEFAULT_KEY].get("context_window"),
                models[DEFAULT_KEY].get("max_output_tokens"),
            )
        entry = models[DEFAULT_KEY]
        name = DEFAULT_KEY

    seen: set[str] = set()
    while "alias_of" in entry:
        target = str(entry["alias_of"])
        if target in seen:
            raise RuntimeError(f"models.yaml 里别名成环：{sorted(seen)} → {target}")
        seen.add(target)
        if target not in models:
            raise RuntimeError(f"models.yaml 里 {name} 指向了不存在的别名 {target}")
        name = target
        entry = models[target]

    return ModelCaps(
        name=name,
        context_window=int(entry.get("context_window", 32768)),
        max_output_tokens=int(entry.get("max_output_tokens", 4096)),
        encoding=str(entry.get("encoding", "cl100k_base")),
        provider=str(entry.get("provider", "openai_compatible")),
    )


def resolve_max_output(model_name: str | None) -> int:
    """传给厂商的 `max_tokens`。**只来自注册表** —— 不读 .env、不按 step 分档。"""
    return resolve(model_name).max_output_tokens


def hardware_input_cap(model_name: str | None) -> int:
    """物理 input 顶 = 窗口 - 输出顶 - 余量。"""
    return resolve(model_name).hardware_input_cap
