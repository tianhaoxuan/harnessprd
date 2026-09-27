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
DEFAULT_PLANS_DB_FILE = "harnessprd.db"
"""方案库（SQLite）默认文件名，落在 `backend/` 下。`.gitignore` 里有 `*.db`。"""

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
    # 逐步 LLM 观测日志（每次模型调用一行 JSON）。
    # 关掉时**仍会**打整趟的 run_summary、也**仍会**在 SSE 里发 run_summary
    # （前端还能看到汇总），只是不打逐步的 `event=llm_step`。
    # ⚠️ 失败的调用**永远记**（即使这个开关是 false）——出错的账不能因为开关而丢。
    log_llm_metrics: bool = Field(default=True, validation_alias="LOG_LLM_METRICS")

    # ---------- 跨分片观测（一份产物 = 一个 run）----------
    # 一份产物由前端按分片计划循环发多次 SSE 请求，各片的账按 `X-Run-ID` 在**进程内**
    # 合并（见 services/llm_metrics.py）。这份合并结果只活在一次产物生成的窗口里，
    # 所以给 TTL 与容量上限：丢了只是少一份合并汇总，每片的 llm_step 日志仍在。
    # TTL 默认 30 分钟：正常一次产物生成是分钟级，够覆盖"用户中途去接杯水"；
    # 上限默认 256：一个 run 常驻内存极小（几十个步骤对象），但客户端崩了会留下
    # 永远等不到 complete 的 run，得有条兜底把它们清掉（按最后上报时间淘汰最旧）。
    run_metrics_ttl_seconds: float = Field(
        default=1800.0, validation_alias="RUN_METRICS_TTL_SECONDS"
    )
    run_metrics_max_runs: int = Field(default=256, validation_alias="RUN_METRICS_MAX_RUNS")

    # ---------- Token 预算（业务策略 cap）----------
    # 有效 input 上限 = min(这里的 cap, 模型物理顶)；物理顶来自 config/models.yaml。
    # 输出上限**不在 .env**：只来自注册表，否则换模型不跟着变、还会把 PRD 截断。
    context_budget_tokens: int = Field(default=120000, validation_alias="CONTEXT_BUDGET_TOKENS")
    chat_history_budget: int = Field(default=60000, validation_alias="CHAT_HISTORY_BUDGET")
    single_call_input_budget: int = Field(default=100000, validation_alias="SINGLE_CALL_INPUT_BUDGET")
    token_budget_mode: str = Field(default="warn", validation_alias="TOKEN_BUDGET_MODE")
    token_estimator: str = Field(default="tiktoken", validation_alias="TOKEN_ESTIMATOR")

    # ---------- 服务 ----------
    # 0.0.0.0 = 监听所有网卡（容器/局域网可访问）；仅本机自用建议改 127.0.0.1
    host: str = "0.0.0.0"
    port: int = 8000

    # Annotated[..., NoDecode] 关掉 pydantic-settings 对复杂类型的 JSON 预解析，
    # 否则 "http://localhost:5173" 这种裸字符串会在校验前就抛 SettingsError。
    cors_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["http://localhost:5173"]
    )

    # ---------- 数据层：SQLite（方案库） ----------
    # 方案（工作台快照 JSON）落在这个文件里。**默认放在 backend/ 下** —— 与 .env 同级：
    # 整个后端目录拷走就能跑，`.gitignore` 也只要一条 `*.db` 就够。
    #
    # ⚠️ 留空（或写 `SQLITE_PATH=`）等于"用默认值"，而不是"没有数据库" ——
    # 所以类型是可选的 `Path | None`，真正的默认值在 `plans_db_path` 里给。
    # 测试/多实例请显式传一个临时路径，别共用默认文件（否则并发跑测试会互相看到数据）。
    sqlite_path: Path | None = Field(default=None, validation_alias="SQLITE_PATH")
    # SQLite 的写锁是**库级**的：并发写会返回 SQLITE_BUSY。这里是等待毫秒数（不是重试次数）。
    # 5 秒足够单机单进程的写入排队；真出现持续 BUSY 说明该换库了，而不是该调大这个值。
    sqlite_busy_timeout_ms: int = 5000

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

    # ---------- 审核智能体（双智能体里的 Reviewer，可选）----------
    # 默认**留空 = 与写手同一个模型**（同一套 API/Key、同一个 model），只有提示词不同。
    # 配上就换成另一个模型：同厂换 model 名即可（Key 不用多配一份），也可以换 provider。
    # 这是「同一套 API、两个角色」的开关 —— 见 Settings.review_llm_settings。
    review_llm_provider: LlmProvider | None = Field(
        default=None, validation_alias="PRD_REVIEW_LLM_PROVIDER"
    )
    review_llm_model: str | None = Field(default=None, validation_alias="PRD_REVIEW_LLM_MODEL")

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
        "review_llm_model",
        "sqlite_path",
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
    def plans_db_path(self) -> Path:
        """方案库（SQLite）的实际路径：显式配置优先，否则 `backend/harnessprd.db`。

        派生而不是字段默认值，是为了让 `SQLITE_PATH=`（空值）等价于"没配"而不是
        "路径为空字符串" —— 后者会在建表时抛一个很难懂的 `Path('')` 错误。
        放进 `BACKEND_DIR` 而不是当前工作目录：uvicorn 可以从仓库根目录启动
        （`--app-dir backend`），按 cwd 解析会让"同一个应用、两个库文件"。
        """
        return self.sqlite_path or (BACKEND_DIR / DEFAULT_PLANS_DB_FILE)

    @property
    def review_llm_settings(self) -> Settings:
        """审核智能体用的配置（**默认与写手完全一致**）。

        只有显式配了 `PRD_REVIEW_LLM_PROVIDER` / `PRD_REVIEW_LLM_MODEL` 才分叉，
        所以"同一套 API、两个角色"是默认形态；想降低自查偏差时只改环境变量。

        ⚠️ 只配 model、不配 provider 时，模型名可能属于别的厂商
        （例如 provider 还是 deepseek 却写了 `claude-...`）。这种错配交给
        `build_chat_model` 报错，这里**不复制一份厂商白名单**（两份必然漂移）。
        """
        updates: dict[str, object] = {}
        if self.review_llm_provider is not None:
            updates["llm_provider"] = self.review_llm_provider
        if self.review_llm_model is not None:
            updates["llm_model"] = self.review_llm_model
        return self if not updates else self.model_copy(update=updates)

    @property
    def review_llm_active_model(self) -> str:
        """审核实际用的模型名（没单独配就是写手那个）。"""
        if self.review_llm_model is not None:
            return self.review_llm_model
        if self.review_llm_provider is not None:
            return FALLBACK_MODELS[self.review_llm_provider]
        return self.active_llm_model

    @property
    def is_local(self) -> bool:
        return self.environment == "local"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """带缓存的配置读取。测试中可用 ``get_settings.cache_clear()`` 重置。"""
    return Settings()
