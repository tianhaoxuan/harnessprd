"""应用配置：全部环境变量的唯一入口。

其他模块一律通过 ``get_settings()`` 读取配置，不直接读 ``os.environ``，
这样每个配置项都有类型、有默认值，写错时报错位置明确。

命名约定：字段名用 ``llm_*`` 保持代码简洁，环境变量用 ``DEFAULT_LLM_*``
语义更明确，两者用 ``validation_alias`` 绑定。
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Literal

from dotenv import load_dotenv
from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# backend/ 目录（本文件位于 backend/core/）
BACKEND_DIR = Path(__file__).resolve().parents[1]
ENV_FILE = BACKEND_DIR / ".env"

# 让直接读 os.environ 的第三方库（langchain-openai 等）也能拿到 .env 的值。
# override=False：真实环境变量优先于 .env 文件。
load_dotenv(ENV_FILE, override=False)

LlmProvider = Literal["deepseek", "openai", "anthropic"]
Environment = Literal["local", "dev", "staging", "prod"]

# DEFAULT_LLM_MODEL 留空时，按 provider 回退到这里。
# 有这张表，就不会因为忘改 DEFAULT_LLM_MODEL 而把 A 厂的模型名发给 B 厂。
FALLBACK_MODELS: dict[str, str] = {
    # 注意：`deepseek-chat` 不在 API `GET /models` 返回的清单里（只有 deepseek-flash、
    # deepseek-v4-pro），它仍能调通但实际模型与计费不可知。
    # 之所以仍用它是为了与 `validation_out/` 的验证留档保持一致 —— 已实测过
    # `deepseek-flash`，它会让「一个 FR 不同时出现在两张表里」这条设计假设失效
    # （见 HANDOFF.md §4 坑 #14）。模型切换应当作为一次独立决策、连带重跑验证，
    # 不要顺手改这一行。
    "deepseek": "deepseek-chat",
    "openai": "gpt-4o-mini",
    "anthropic": "claude-sonnet-4-20250514",
}


class Settings(BaseSettings):
    """进程级配置。字段名或别名即环境变量名（大小写不敏感）。"""

    model_config = SettingsConfigDict(
        env_file=ENV_FILE,
        env_file_encoding="utf-8",
        case_sensitive=False,
        # 允许 Settings(llm_provider=...) 按字段名传参（测试/工厂里用）
        populate_by_name=True,
        extra="ignore",
    )

    # ---------- 应用 ----------
    app_name: str = "harnessprd"
    version: str = "0.1.0"
    environment: Environment = "local"
    debug: bool = True
    log_level: str = "INFO"

    # ---------- 服务 ----------
    # 0.0.0.0 = 监听所有网卡（容器/局域网可访问）；仅本机自用建议改 127.0.0.1
    host: str = "0.0.0.0"
    port: int = 8000

    # Annotated[..., NoDecode] 关掉 pydantic-settings 对复杂类型的 JSON 预解析，
    # 否则 "http://localhost:5173" 这种裸字符串会在校验前就抛 SettingsError。
    cors_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["http://localhost:5173"]
    )

    # ---------- LLM ----------
    llm_provider: LlmProvider = Field(
        default="anthropic", validation_alias="DEFAULT_LLM_PROVIDER"
    )
    # 留空（或写空值）时回退到 FALLBACK_MODELS[provider]
    llm_model: str | None = Field(default=None, validation_alias="DEFAULT_LLM_MODEL")
    llm_temperature: float = 0.2
    # 4096 是旧默认值，多份设计文档点名不够用：
    #   docs/PRD模板.md §4.2/§4.3  —— 新模板上限约 9000–10000 输出 token，"需提到 8192 以上"
    #   docs/接口文档模板.md §提示   —— "远超 llm_max_tokens = 4096，必须分章生成"
    #   docs/提示词套件模板.md §提示 —— 套件上限 19400 字，远超 4096
    #   docs/功能清单.md §非功能    —— max_tokens 上限（现为 4096）
    # 取 8192 只满足文档下限、不预设"一次性生成 vs 分章生成"（那是 docs/PRD模板.md R4 待拍板项）。
    # 注意：这是**上限**，模型没写那么长就不花那么多 token。
    # DeepSeek 当前 API 允许 1–384K、默认 8K；显式写 4096 反而低于它自己的默认值。
    llm_max_tokens: int = 8192
    llm_timeout: float = 120.0

    anthropic_api_key: SecretStr | None = None
    openai_api_key: SecretStr | None = None
    deepseek_api_key: SecretStr | None = None

    deepseek_base_url: str = "https://api.deepseek.com/v1"
    # 留空走官方地址；填了可接中转站或本地 Ollama
    openai_base_url: str | None = None

    # ---------- 技能包（skills/）----------
    # PRD 生成是否用 `skills/prd-generator/` 技能包组装 system prompt。
    # **默认打开**：技能包是产品 PRD 的**唯一结构**（6 章：产品概述 / 功能需求 / 页面设计 /
    # 技术规格 / 非功能需求 / 项目范围，第 2 章带 FR-xx 与 AC-xx 编号）。
    # 关掉它才走 `gen_prd.md` 的老结构（15 章 + 2 附录）—— 那条路只作**回退**保留，
    # 产品上已经不再使用，两套结构互不兼容。
    #
    # ⚠️ 改这个开关必须同步下面这些地方，否则会出现"文档说一套、产物是另一套"：
    #   - services/document_plan.py 的 PRD 分片计划（标签与 outline 跟着开关走）
    #   - frontend/src/App.tsx 与 services/api.ts 的 PRD 文案（现在写的是 6 章）
    #   - scripts/smoke_check.py 与 scripts/skill_prd_check.py 的断言
    #   - backend/core/prompts/ 里 gen_api.md / gen_prompts.md / clarify_s*.md 的章节引用
    #     （它们按技能包的 6 章与 FR/AC 编号取数）
    #   - docs/ 与 HANDOFF.md 里描述 PRD 结构的地方
    prd_use_skill: bool = True

    # ---------- 数据层 ----------
    # docker-compose.yml 已提供 Postgres 16 / Redis 7，业务代码暂未接入，
    # 这里先把连接信息收拢，接入时直接取用，避免各处硬编码。
    postgres_host: str = "127.0.0.1"
    postgres_port: int = 5432
    postgres_user: str = "harnessprd"
    postgres_password: SecretStr = SecretStr("harnessprd_dev")
    postgres_db: str = "harnessprd"
    redis_url: str = "redis://127.0.0.1:6379/0"

    # ---------------- 校验 ----------------
    @field_validator("cors_origins", mode="before")
    @classmethod
    def _parse_cors_origins(cls, value: Any) -> Any:
        """接受三种写法：

        - ``CORS_ORIGINS=http://localhost:5173``                单个
        - ``CORS_ORIGINS=http://a,http://b``                    逗号分隔
        - ``CORS_ORIGINS=["http://a","http://b"]``              JSON 数组（兼容旧写法）

        也接受代码里直接传 list/tuple（测试用）。
        """
        if isinstance(value, str):
            raw = value.strip()
            if not raw:
                return []
            if raw.startswith("["):
                try:
                    parsed = json.loads(raw)
                except json.JSONDecodeError as exc:
                    raise ValueError(
                        f"CORS_ORIGINS 看起来是 JSON 数组但解析失败：{exc}；"
                        "也可以直接用逗号分隔，如 http://a,http://b"
                    ) from exc
                if not isinstance(parsed, list):
                    raise ValueError("CORS_ORIGINS 的 JSON 形式必须是数组")
                return [str(item).strip() for item in parsed if str(item).strip()]
            return [item.strip() for item in raw.split(",") if item.strip()]
        return value

    @field_validator(
        "llm_model",
        "anthropic_api_key",
        "openai_api_key",
        "deepseek_api_key",
        "openai_base_url",
        mode="before",
    )
    @classmethod
    def _blank_to_none(cls, value: object) -> object:
        """`.env` 里写了 `FOO=`（空值）应等同"未配置"。

        否则会得到 ``SecretStr('')`` / ``""`` 这种"看似已配置"的假象：
        前者表现为鉴权失败而不是缺配置，后者会把空模型名发给厂商。
        """
        if isinstance(value, str) and not value.strip():
            return None
        return value

    # ---------------- 派生状态 ----------------
    # 均为 property（不参与序列化），避免密钥随 model_dump 外泄。
    @property
    def postgres_dsn(self) -> str:
        password = self.postgres_password.get_secret_value()
        return (
            f"postgresql://{self.postgres_user}:{password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def active_llm_model(self) -> str:
        """实际使用的模型名：显式配置优先，否则按 provider 回退。"""
        return self.llm_model or FALLBACK_MODELS[self.llm_provider]

    @property
    def active_llm_api_key(self) -> SecretStr | None:
        """当前 provider 对应的密钥（仅 SecretStr，取值需显式 get_secret_value）。"""
        return {
            "deepseek": self.deepseek_api_key,
            "openai": self.openai_api_key,
            "anthropic": self.anthropic_api_key,
        }[self.llm_provider]

    @property
    def llm_configured(self) -> bool:
        """当前 provider 是否具备调用条件（只检查配置，不检查连通性）。"""
        return self.active_llm_api_key is not None

    @property
    def is_local(self) -> bool:
        return self.environment == "local"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """带缓存的配置读取。测试中可用 ``get_settings.cache_clear()`` 重置。"""
    return Settings()
