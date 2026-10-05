r"""会话业务层：API 直接调它。四个操作 + 两条会话特有的规则。

## 四个操作

| 操作 | 行为 |
| --- | --- |
| `list_sessions()` | 按 `updated_at` 倒序；**只回摘要**，不带 `session_data` |
| `get_session(id)` | 回完整快照；若 `viewState` 是 `generating-*`，**先降级再返回** |
| `save_session(data, id?)` | 无 id → 新建并返回新 id；有 id → 更新；**写入前降级**；标题只在新记录时解析写入 |
| `delete_session(id)` | 不存在 → 抛 `SessionNotFound`（HTTP 层映射 404） |

## 规则一：`generating-*` 降级

与前端 `App.tsx` 的 `loadSession()` **同一张表**（那边叫 `IN_FLIGHT_VIEWS`）：

    generating-prd        → review-prd        （被打断的是 PRD）
    generating-api-docs   → review-api-docs   （接口文档）
    generating-prompts    → review-prompts    （提示词套件）

为什么必须降级：`generating-*` 是"**当时**正在生成"的意思，而生成过程**只活在内存里** ——
进程没了、请求断了，就没有任何东西在跑。把它原样返回，客户端会显示一个永远不会完成的
"生成中"，用户等到天荒地老（`会话持久化方案` §7.4 的孤儿生成态）。降级到"待审核"是诚实的表述：
**稿子可能已经写了一半或写完了，但没人知道** —— 所以调用方还需要知道"是哪一份被打断了"。

⚠️ 只做降级，**不**移植前端 `fallbackView()` 那套（"没有 viewState 时按产物有没有内容推断"）。
那是另一条规则（兼容缺少 `viewState` 的旧数据），本次没有要求，不做也不该顺手加。

## 规则二：标题

优先级：**PRD 正文的第一个 `#` 一级标题** → **`产品名称：xxx`** → **记录 id**。

- 只在**新建**时解析写入。更新已有记录**不动 title** —— 用户可能手工改过，
  而且更新时正文常常还没写完（拿半份正文去重命名是负优化）。
- 优先级 1 要求是**一级**标题：正则写 `^#\s+`，所以 `## 2. 功能需求` 不会被误当成标题。

## 分层

本层**不直接写 SQL**：存储、校验、时间戳都委派给 `PlanService`（同一张表）。
另起一套实现就等于两份真相，改一处忘一处不会有任何报错。本层只加会话特有的两条规则。
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from collections.abc import Collection, Mapping
from typing import Any

from core.config import Settings
from services.document_version_repository import DOC_TYPES
from services.document_version_service import (
    DocumentVersionRecord,
    DocumentVersionService,
)
from services.job_models import (
    ARTIFACT_DOC_KIND,
    ARTIFACT_GENERATING_VIEW,
    ARTIFACT_REVIEW_VIEW,
    PRD_REVIEW_RESULT_KEY,
    content_artifact_of,
    is_optimize_artifact,
)
from services.job_service import JobService
from services.plan_models import (
    ENTRY_MODES,
    STAGES,
    TITLE_FALLBACK,  # noqa: F401 - 供调用方参考的兜底文案（本层兜底是记录 id）
    PlanCreate,
    PlanUpdate,
)
from services.plan_service import PlanService, TITLE_MAX_LENGTH, normalize_snapshot
from services.session_models import (
    SessionLoad,
    SessionSaveResult,
    SessionSummary,
)
from services.state import DocKind

logger = logging.getLogger(__name__)

DOWNGRADES: dict[str, tuple[str, DocKind]] = {
    "generating-prd": ("review-prd", "prd"),
    "generating-api-docs": ("review-api-docs", "api"),
    "generating-prompts": ("review-prompts", "prompts"),
}
"""`generating-*` → (对应的审核视图, 被打断的产物)。与前端 `IN_FLIGHT_VIEWS` 一致。"""

_H1_TITLE = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)
"""**一级**标题：`# ` 后面直接跟内容。`## ` 不会命中（`#` 后面不是空白）。"""

_NAME_LINE = re.compile(r"产品名称\s*[：:]\s*(.+?)\s*$", re.MULTILINE)
"""`产品名称：xxx`（中英文冒号都认）。"""

_TITLE_KEYS = ("product_name", "productName")
"""快照里可能放产品名的两处键：20 题表单用 `product_name`，结构化表单用 `productName`。

与 `plan_service._TITLE_KEYS` 同源但**各自持有**：那是方案层的兜底，这里是会话层的
（多一档"表单产品名"）。合成一处反而会让两层互相拖住，改动理由也不同。"""
"""`产品名称：xxx`（中英文冒号都认）。"""


class SessionNotFound(LookupError):
    """记录不存在。

    继承 `LookupError` 而不是自定义 `Exception`：HTTP 层据此返 **404**，
    语义也对（"查不到"就是 `LookupError`）。本层**不 import fastapi** ——
    传输协议的细节归 `api/`。
    """


# ---------------------------------------------------------------- 纯函数（可单测，不碰库）


def downgrade_view_state(view: str) -> tuple[str, DocKind | None]:
    """把单个 `viewState` 降级。返回 `(降级后的视图, 被打断的产物)`。"""
    target = DOWNGRADES.get(view)
    return target if target else (view, None)


def downgrade_session_data(
    session_data: str, *, running_job_ids: Collection[str] = ()
) -> tuple[str, str | None, DocKind | None]:
    """把快照里的 `viewState` 降级。

    返回 `(快照文本, 原 viewState, 被打断的产物)`。

    ⚠️ **不需要降级时原样返回**（不改键序、不改空白）。这个函数在每次保存时都会被调用，
    如果它顺手重新序列化一遍，"存进去什么就拿回什么"这条性质就没了 ——
    而且库里每存一次都会产生一次无意义的文本变更。

    ## `running_job_ids`：有任务真在跑时**不许**降级

    Generation Job 把"生成中"变成了**服务端事实**（`generation_jobs` 表里有一行
    `status=running`），而不只是浏览器里的一个视图状态。这时再按
    "生成中 = 孤儿态"去降级就是错的：

    - 降成 `review-*` → 前端认为稿子已经写完了，于是**不去重连** SSE，
      用户把一份半截草稿当成终稿；
    - 降成澄清页 → 用户被弹回去，而任务其实还在后台写。

    所以只要快照里的 `activeJobId` 指向一个**在跑**的任务，就原样保留 `generating-*`；
    任务收尾时由 runner 自己把 `activeJobId` 清掉并落回 `review-*`
    （见 `SessionService.sync_job_completed`）—— 那时降级判断再也不会命中它。

    ⚠️ 判断依据**只看 `activeJobId`**，不看"这个会话有没有在跑的 job"：同一个会话可能
    有另一个产物的任务在跑（接口文档在生成时，PRD 那个视图不该受影响）。
    """
    try:
        parsed = json.loads(session_data)
    except json.JSONDecodeError:  # pragma: no cover - 调用前已过 normalize_snapshot
        return session_data, None, None
    if not isinstance(parsed, dict):
        return session_data, None, None
    view = parsed.get("viewState")
    if not isinstance(view, str):
        return session_data, None, None
    downgraded, kind = downgrade_view_state(view)
    if kind is None:
        return session_data, None, None
    active_job = parsed.get("activeJobId")
    if isinstance(active_job, str) and active_job in running_job_ids:
        # 任务真的在跑：这是**活的生成中**，不是孤儿态
        return session_data, None, None
    parsed["viewState"] = downgraded
    # 只有真要改的时候才重新序列化（紧凑 + 不转义中文，与 plan_repository 的口径一致）
    return json.dumps(parsed, ensure_ascii=False, separators=(",", ":")), view, kind


def prd_body(session_data: str) -> str:
    """取 PRD 正文。

    来源是前端持久化的形状 `documents.prd.content`；取不到（还没生成过 PRD、
    或快照不是对象）返回空串 —— 调用方据此走下一优先级，而不是报错。
    """
    try:
        parsed = json.loads(session_data)
    except json.JSONDecodeError:
        return ""
    if not isinstance(parsed, dict):
        return ""
    documents = parsed.get("documents")
    if not isinstance(documents, Mapping):
        return ""
    prd = documents.get("prd")
    if not isinstance(prd, Mapping):
        return ""
    content = prd.get("content")
    return content if isinstance(content, str) else ""


def parse_prd_title(session_data: str, fallback: str) -> str:
    """标题解析：一级标题 → `产品名称：xxx` → 表单里的产品名 → `fallback`（记录 id）。

    第三档（表单产品名）是**实测补上的**：新建方案在"开始澄清对话"那一刻就落库，
    那时还没有 PRD 正文可读 —— 只按前两档会退成记录 id，列表里显示一串十六进制，
    而用户刚在表单里填过产品名（`session_data.form.product_name`）。
    """
    body = prd_body(session_data)
    if body:
        for pattern in (_H1_TITLE, _NAME_LINE):
            matched = pattern.search(body)
            if matched:
                title = matched.group(1).strip()
                if title:
                    return title[:TITLE_MAX_LENGTH]
    # 退一步：快照里的表单作答。20 题表单用 `product_name`，结构化表单用 `productName`。
    try:
        parsed = json.loads(session_data)
    except json.JSONDecodeError:
        parsed = None
    form = parsed.get("form") if isinstance(parsed, Mapping) else None
    if isinstance(form, Mapping):
        for key in _TITLE_KEYS:
            candidate = form.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()[:TITLE_MAX_LENGTH]
    return fallback


def session_document_contents(session_data: str) -> dict[str, str]:
    """从快照里取出三种产物的正文 → `{doc_type: content}`（只要**有内容**的那些）。

    ## ⚠️ 真实形状是 `documents.<kind>.content`，不是需求里写的 `prdContent`

    需求 §2 / §4 把这三个字段写成 `prdContent` / `apiDocsContent` / `promptsContent`。
    那三个名字是前端 `App.tsx` 的 **React state 变量名**（`useState`），**不是快照里的键** ——
    真正落库的形状由 `buildSnapshot()` 决定：

        { ..., "documents": { "prd": {"content": ..., "approved": ...},
                              "api": {"content": ...},
                              "prompts": {"content": ...} }, ... }

    所以三个字段的落点是 `documents.prd.content` / `documents.api.content` /
    `documents.prompts.content`。⚠️ 注意中间那个是 **`api`**，不是 `api-docs`
    —— 对外的 `api-docs` 到内部的 `api` 由 `job_models.ARTIFACT_DOC_KIND` 定义一次，
    本函数用 `doc_kind_for()` 取它，不另写一张表。

    空串 / 全空白一律当"没有"：与前端 `buildSnapshot()` 的 `if (!content.trim()) continue`
    一致（只有空白的产物与没有产物是一回事）。
    """
    try:
        parsed = json.loads(session_data)
    except json.JSONDecodeError:
        return {}
    if not isinstance(parsed, Mapping):
        return {}
    documents = parsed.get("documents")
    if not isinstance(documents, Mapping):
        return {}
    contents: dict[str, str] = {}
    for doc_type in DOC_TYPES:
        entry = documents.get(doc_kind_for(doc_type))
        if not isinstance(entry, Mapping):
            continue
        content = entry.get("content")
        if isinstance(content, str) and content.strip():
            contents[doc_type] = content
    return contents


def derive_summary_fields(session_data: str) -> tuple[str | None, str | None]:
    """从快照推导摘要列：`(entry_mode, current_stage)`。

    推导而不是让调用方另传一份 —— 摘要列与快照**必须一致**，否则列表页显示"表单阶段"、
    点进去却停在"提示词"，而且没有任何东西会报错。推导不出来的返回 `None`
    （= "这次别动这一列"），由调用方决定新建时的默认值。
    """
    try:
        parsed = json.loads(session_data)
    except json.JSONDecodeError:
        return None, None
    if not isinstance(parsed, dict):
        return None, None
    entry = parsed.get("entryMode")
    view = parsed.get("viewState")
    return (
        entry if isinstance(entry, str) and entry in ENTRY_MODES else None,
        view if isinstance(view, str) and view in STAGES else None,
    )


# ---------------------------------------------------------------- Service


class SessionService:
    """会话业务入口。存储与校验委派给 `PlanService`（同一张表）。"""

    def __init__(
        self,
        settings: Settings | None = None,
        plans: PlanService | None = None,
        jobs: JobService | None = None,
        document_versions: DocumentVersionService | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._plans = plans or PlanService(self._settings)
        # 任务层：只用来回答"这个 job 还在跑吗"（降级判断）与"把任务进度写回会话"。
        # 依赖方向是 **session → job**，job 那一侧不反向依赖会话（`job_repository` 只懂 SQL），
        # 所以这里不存在循环导入。
        self._jobs = jobs or JobService(self._settings)
        # 文档版本层：保存/读取会话时要把它与 `session_data` 镜像对齐（02 篇）。
        # 方向同样是 **session → documents**。反向（documents → session）会成环，
        # 所以 `document_version_service` 里刻意不 import 本模块（它的异常也自带一套）。
        #
        # ⚠️ 本类**自己持有**并对外暴露（`sessions.documents`）而不是让调用方另传一个：
        # `job_runner` 收尾时要写版本层，而从 `start_job(jobs=…, sessions=…)` 再穿一个
        # 参数下去，会在自检脚本里落进 `get_settings()` 的**默认库**
        # （`job_check.py` 只注入 `SessionService(settings)`，临时库就只对一半生效）。
        # 与 `deps.py` 反复强调的那条污染陷阱是同一件事。
        self._documents = document_versions or DocumentVersionService(self._settings)

    @property
    def plans(self) -> PlanService:
        return self._plans

    @property
    def jobs(self) -> JobService:
        return self._jobs

    @property
    def documents(self) -> DocumentVersionService:
        """文档版本层（`job_runner` 收尾时用它写版本，见 `services/job_runner.py`）。"""
        return self._documents

    def ensure_ready(self) -> None:
        """建表（幂等）。应用启动时调用 —— 见 `main.py` 的 lifespan。

        ⚠️ **三层表都要建**，而不是只建方案表：

        - `generation_jobs`：本层的降级判断要读它（"这个 job 还在跑吗"）；
        - `documents` / `document_versions`：本层的 `save_session` / `get_session` 要写它
          （镜像同步与老数据迁移），`job_runner` 也通过 `sessions.documents` 写它。

        少建一张的表现是 `no such table: …`，而报错点离原因很远（在第 N 次保存、
        或某个任务收尾时才暴露）。`job_check.py` 有一条断言专门盯这件事。
        """
        self._plans.ensure_ready()
        self._jobs.ensure_ready()
        self._documents.ensure_ready()

    # ------------------------------------------------------------ 读

    def list_sessions(self, *, limit: int = 50, offset: int = 0) -> list[SessionSummary]:
        """列表：按 `updated_at` 倒序，**只回摘要**（不读 `session_data` 那一列）。"""
        return [
            SessionSummary(**row.model_dump())
            for row in self._plans.list(limit=limit, offset=offset)
        ]

    def running_job_ids(self, session_id: str) -> set[str]:
        """这个会话下**正在跑**的任务 id（降级判断的唯一依据）。"""
        return self._jobs.running_ids_for_sessions([session_id])

    def get_session(self, session_id: str) -> SessionLoad:
        """取完整快照。`generating-*` **先降级再返回**，除非有任务真在跑。

        降级**不回写**：读操作不该改数据。库里若因故留着 `generating-*`（例如别的写入方
        绕过本层），每次读都会稳定地降级成同一个结果 —— 这是可预期的，不是"时好时坏"。

        ⚠️ `running_job_ids` 必须传：没有它，正在后台生成的任务会被当成孤儿态降级掉，
        前端于是不会去重连 SSE（需求 §十一 的关键一条）。

        ⚠️ **本接口会写库**，两处，而且都只在真有必要时写（见 `_align_documents_on_read`）：
        老 Session 补一个 v1（`source_kind=import`），以及把版本层有、镜像没有的正文回填。
        需求 §5 建议把它放在 save 路径以减少 GET 的副作用，但同一节又要求"打开旧方案应
        直接出现 v1，无需用户操作" —— 那条只有在读路径上做才**不依赖前端防抖保存真的会跑**。
        """
        record = self._plans.get(session_id)
        if record is None:
            raise SessionNotFound(f"会话不存在：{session_id}")
        text, original, kind = downgrade_session_data(
            record.snapshot, running_job_ids=self.running_job_ids(session_id)
        )
        text = self._align_documents_on_read(session_id, text)
        return SessionLoad(
            **record.model_dump(exclude={"snapshot"}),
            session_data=text,
            downgraded_from=original,
            interrupted_kind=kind,
        )

    # ------------------------------------------------------------ 写

    def save_session(
        self,
        session_data: str | Mapping[str, Any],
        session_id: str | None = None,
    ) -> SessionSaveResult:
        """新建（无 id）或更新（有 id）。**写入前降级**，标题只在新记录时解析。

        ⚠️ 写入前的降级同样要跳过"有任务真在跑"的快照：否则用户（或前端）在这次生成
        期间恰好保存了一次，`generating-prd` 就在库里被改成了 `review-prd` ——
        而任务还在后台写，界面从此不再重连。
        """
        text = normalize_snapshot(session_data)
        running: set[str] = set()
        if session_id is not None:
            running = self.running_job_ids(session_id)
        # 写入前降级：库里**不该**出现孤儿态的 generating-*（它是"此刻在跑"的意思，
        # 而落库这个动作本身就说明那一刻已经过去了）——除非确有任务在跑。
        text, downgraded_from, _ = downgrade_session_data(text, running_job_ids=running)
        if downgraded_from is not None:
            logger.info("保存会话时把 %s 降级后再写入（原状态只存在于内存）", downgraded_from)
        entry_mode, current_stage = derive_summary_fields(text)

        if session_id is None:
            new_id = uuid.uuid4().hex
            record = self._plans.create(
                PlanCreate(
                    id=new_id,
                    # 标题的兜底**就是记录 id**：解析不出来也要有个能认的名字，
                    # 空标题在列表里最难查（不知道是哪一条）。
                    title=parse_prd_title(text, new_id),
                    entry_mode=entry_mode or "structured",
                    current_stage=current_stage or "form",
                    snapshot=text,
                )
            )
            # 会话先落库，再同步版本层 —— 版本层的 `ensure_document` 要校验会话存在。
            self._sync_documents_after_save(new_id, text)
            return SessionSaveResult(id=record.id, created=True, title=record.title)

        updated = self._plans.update(
            session_id,
            PlanUpdate(entry_mode=entry_mode, current_stage=current_stage, snapshot=text),
        )
        if updated is None:
            raise SessionNotFound(f"会话不存在：{session_id}")
        self._sync_documents_after_save(session_id, text)
        # ⚠️ 这里**不传 title** —— 更新不改标题（见模块 docstring 的规则二）。
        return SessionSaveResult(id=updated.id, created=False, title=updated.title)

    def delete_session(self, session_id: str) -> None:
        """删除。不存在 → `SessionNotFound`（HTTP 层 404）。"""
        if not self._plans.delete(session_id):
            raise SessionNotFound(f"会话不存在：{session_id}")

    # ------------------------------------------------------------ 文档版本层同步（02 篇）
    #
    # 写入链路（需求 §七）——**两个方向，顺序都是「先版本层、再镜像」**：
    #
    #     Job 完成 / 失败（job_runner）
    #       → sync_from_job(...)             版本层：升新版或就地更新
    #       → sync_job_completed(...)        镜像：documents.<kind>.content
    #
    #     用户手改，前端防抖保存（本层 save_session）
    #       → 会话先落库
    #       → maybe_migrate_from_session     老数据补 v1（import）
    #       → sync_session_contents          镜像 → current（auto_save，**不升号**）
    #
    # ⚠️ **镜像与版本层的对齐是"尽力而为"**：`session_data` 才是用户正在编辑的那份数据，
    # 版本层是本次新加的对外能力。版本层出问题不该让"保存"这个动作返回 500 ——
    # 那时镜像已经落库了，用户会以为没存上，然后反复重试。
    # `job_runner._sync_session` 对会话同步是这个口径，这里反过来对版本同步用同一条。

    def _generating_doc_types(self, session_id: str) -> set[str]:
        """正在被生成任务写入的 `doc_type`（这些**不许**从镜像同步进版本层）。

        ⚠️ **为什么必须剔除**：前端在生成期间会把**半成品**按 1.5 秒节流写进产物
        （`useGenerationJob`），而防抖保存（800ms / 最长 2s）又会把半成品存进
        `session_data`。照单同步的话，这份产物的**第一个版本就是一段半截正文** ——
        验收标准里"生成完成后 `version_no=1`"会直接不成立，侧栏历史也会被半成品污染。
        这份产物的正主是那个 Job，收尾时由 `job_runner` 写权威版本。

        与 `downgrade_session_data` 的 `running_job_ids` 是**同一条判断依据**（有任务在跑），
        只是用途不同：那边决定"降不降级"，这边决定"写不写版本"。

        读不到任务表时返回**全部** doc_type（= 本轮一个版本都不写）：宁可少写一次，
        也不要把半成品固化成历史版本。
        """
        try:
            running = self._jobs.list_running_for_session(session_id)
        except Exception:  # noqa: BLE001 - 读任务表失败不该让保存/读取失败
            logger.exception("读在跑的任务失败：本轮跳过版本同步（避免把半成品写成版本）")
            return set(DOC_TYPES)
        return {content_artifact_of(job.artifact) for job in running}

    def _sync_documents_after_save(self, session_id: str, session_data: str) -> None:
        """保存后把正文同步进版本层：**先迁移老数据，再把本次改动写进 current**。

        顺序不能反（需求 §4）：迁移只处理"没有 current 版本"的槽位，先跑它，
        本次手改才会走 `update_current_content` 而不是又插一版。

        ⚠️ 需求 §4 说这一步在"`conn.commit` 之前"—— 本项目的 `PlanService` 没有对外暴露
        事务（每次 `create`/`update` 各开一条连接），所以顺序是**会话先落库、再同步版本层**。
        这也是必需的：`ensure_document` 要校验会话存在，先建会话行它才过得去。
        """
        contents = self._syncable_contents(session_id, session_data)
        if not contents:
            return
        try:
            self._migrate_session_documents_if_needed(session_id, contents)
            self._sync_session_content_to_documents(session_id, contents)
        except Exception:  # noqa: BLE001 - 版本层失败不该把已落库的保存判成失败
            logger.exception(
                "会话 %s 已保存，但同步文档版本层失败（产物与镜像不受影响）", session_id
            )

    def _align_documents_on_read(self, session_id: str, session_data: str) -> str:
        """读路径上的版本层对齐（需求 §5 / §6）。返回**可能已写回**的快照文本。

        两件事：

        1. **迁移**：老 Session 有正文但没有版本 → 导入成 v1（`source_kind=import`，只做一次）；
        2. **回填**：版本层有正文、而镜像**没有**（键缺失 / 空串）→ 回填进镜像。

        两件都不会每次都产生写（对齐好了就是纯读）。

        ⚠️ 回填**只填镜像缺的那些**，绝不用版本层的旧正文去覆盖镜像里已有的正文。
        需求 §6 的原话是"以 document current 为准覆盖镜像并写回 DB"，但 §4 的写入顺序是
        「先写镜像、再同步版本」—— 一旦版本同步失败（本层是尽力而为），镜像就会比版本新，
        那时"以版本为准"会把用户刚改的正文**静默换回旧版**。所以方向取"只补缺"：
        两边不一致时保留镜像，由下一次保存把镜像推进版本层。
        """
        contents = self._syncable_contents(session_id, session_data)
        try:
            self._migrate_session_documents_if_needed(session_id, contents)
            missing = self._missing_session_contents(session_id, session_data)
            if not missing:
                return session_data
            patch = {"documents": {doc_kind_for(dt): {"content": c} for dt, c in missing.items()}}
            written = self._patch_session(session_id, patch)
        except Exception:  # noqa: BLE001 - 读路径上的对齐失败不该让查询 500
            logger.exception("读取会话时对齐文档版本层失败（返回未改动的快照）")
            return session_data
        if written is None:
            # 会话在读到写的这一瞬间没了（并发删除）：如实返回降级后的快照即可
            return session_data
        logger.info(
            "会话 %s 的快照从文档版本层回填了 %s（镜像里原本没有）",
            session_id,
            "、".join(doc_kind_for(dt) for dt in missing),
        )
        return written

    def _syncable_contents(self, session_id: str, session_data: str) -> dict[str, str]:
        """镜像里**可以**同步进版本层的正文（剔除正在生成的那些产物）。

        剔除只作用于**镜像 → 版本层**这个方向；回填（版本层 → 镜像）要看镜像的真实内容，
        所以那边单独用 `session_document_contents`（不剔除）。
        """
        contents = session_document_contents(session_data)
        blocked = self._generating_doc_types(session_id)
        if not blocked:
            return contents
        return {dt: text for dt, text in contents.items() if dt not in blocked}

    def _migrate_session_documents_if_needed(
        self, session_id: str, contents: Mapping[str, str]
    ) -> list[DocumentVersionRecord]:
        """老 Session 一次性迁移（需求 §5）。语义与"要不要迁"全在版本层。

        这一层只负责"把快照里的正文抠出来"（`contents`），判断与落库委派给
        `DocumentVersionService.maybe_migrate_from_session` —— 规则只有一份。
        """
        return self._documents.maybe_migrate_from_session(session_id, contents)

    def _sync_session_content_to_documents(
        self, session_id: str, contents: Mapping[str, str]
    ) -> list[DocumentVersionRecord]:
        """把镜像里的手改正文同步进 current（`source_kind=auto_save`，**不升号**）。"""
        return self._documents.sync_session_contents(session_id, contents)

    def _missing_session_contents(
        self, session_id: str, session_data: str
    ) -> dict[str, str]:
        """版本层有、镜像**没有**的正文 → `{doc_type: content}`。

        用**未剔除**的镜像内容判断"有没有"：正在生成的那份产物，镜像里是半成品，
        它**存在**，所以不该被版本层的旧正文回填掉。
        """
        present = session_document_contents(session_data)
        missing: dict[str, str] = {}
        for doc_type in DOC_TYPES:
            if doc_type in present:
                continue
            current = self._documents.get_current_version(session_id, doc_type)
            if current is not None and current.content.strip():
                missing[doc_type] = current.content
        return missing

    def sync_document_content(self, session_id: str, doc_type: str, content: str) -> bool:
        """把版本层的 current 正文写回 `session_data` 镜像（`documents.<kind>.content`）。

        给 `api/documents.py` 的 checkpoint / restore 用（需求 §七）：这两个动作用户是
        在侧栏上点的，改的是 current，镜像必须跟着走，否则同一个会话里"侧栏显示的版本"
        与"编辑器里的正文"会立刻不一致。

        返回是否命中（会话不存在 → `False`，**不抛**）：版本层那边已经改好了，
        为一个镜像写失败把接口判成 500 只会让用户以为版本没保存上。
        """
        written = self._patch_session(
            session_id, {"documents": {doc_kind_for(doc_type): {"content": content}}}
        )
        if written is None:
            logger.warning(
                "checkpoint/restore 后镜像同步时会话 %s 已不存在（版本已写入）", session_id
            )
            return False
        return True

    # ------------------------------------------------------------ 任务同步（Generation Job）
    #
    # 职责边界：**Job 表是草稿的权威来源**，会话快照只记"用户做到哪、当前任务是谁"。
    # 所以这里只写四个键（activeJobId / viewState / documents.<kind> / prdReviewResult），
    # 其余字段（messages / form / entryMode / roundIndex / interruptedKind）**原样保留** ——
    # 整份覆盖会把前端自己拥有的 state 抹掉，而且不会有任何报错。
    #
    # 节流：不必每个 chunk 都写会话（那是 job 表的活，见 job_service.DRAFT_PERSIST_INTERVAL_SECONDS）。
    # 只在这三个时刻同步：任务开始、完成、失败。

    def sync_job_started(self, *, session_id: str, job_id: str, artifact: str) -> None:
        """任务开始：`activeJobId` = 本任务；**生成**任务同时把 `viewState` 切到 `generating-*`。

        ⚠️ **优化任务不切视图**（`is_optimize_artifact` 分支）：用户就停在审阅页上看这一节
        被重写，切到 `generating-*` 会让人以为整篇在重生成 —— 而且回头率很高：
        优化完还要落回 `review-*`，一来一回界面会闪一下。

        ⚠️ 只有 `_patch_session` 能写出 `generating-*` 并让它**留在库里**：
        `save_session` 那条路径会在写入前降级（"落库这个动作说明那一刻已经过去了"），
        而任务同步写的正是"此刻确实在跑"，所以走的是单独这条路。
        """
        patch: dict[str, Any] = {"activeJobId": job_id}
        if not is_optimize_artifact(artifact):
            patch["viewState"] = ARTIFACT_GENERATING_VIEW[artifact]
        self._patch_session(session_id, patch)

    def sync_job_completed(
        self,
        *,
        session_id: str,
        job_id: str,
        artifact: str,
        content: str,
        review: Mapping[str, Any] | None = None,
        truncated: bool | None = None,
    ) -> None:
        """任务完成：清 `activeJobId`、落回 `review-*`、把终稿写进 `documents.<kind>.content`。

        - `review` 只对 PRD 有意义，写在 `prdReviewResult`（需求点名要的键）；
        - `truncated` 如实写进文档对象（`HANDOFF.md` §4 坑 #18：截断必须在界面上看得见）。
          `None` = 不说这事（调用方没拿到 outcome 时），**不写成 false** ——
          "不知道"与"确定没截断"是两件事。
        """
        document: dict[str, Any] = {"content": content}
        if truncated is not None:
            document["truncated"] = bool(truncated)
        patch: dict[str, Any] = {
            "activeJobId": None,
            "viewState": ARTIFACT_REVIEW_VIEW[artifact],
            "documents": {doc_kind_for(artifact): document},
        }
        if artifact == "prd" and review is not None:
            patch[PRD_REVIEW_RESULT_KEY] = dict(review)
        self._patch_session(session_id, patch)

    def sync_job_failed(
        self,
        *,
        session_id: str,
        job_id: str,
        artifact: str,
        draft: str = "",
    ) -> None:
        """任务失败：清 `activeJobId`、落回 `review-*`，**把已收到的草稿留在正文里**。

        为什么保留半截草稿而不是清空：那是用户等了几分钟换来的东西，
        清掉等于惩罚用户；而他可以在审核页看到"就断在这里"，再决定手改还是重生成。
        """
        patch: dict[str, Any] = {
            "activeJobId": None,
            "viewState": ARTIFACT_REVIEW_VIEW[artifact],
        }
        if draft:
            patch["documents"] = {doc_kind_for(artifact): {"content": draft}}
        self._patch_session(session_id, patch)

    # ------------------------------------------------------------ 优化任务的同步
    #
    # 与生成任务的**关键差别**（三条，都由 `job_runner` 那边决定并在这里落地）：
    #
    # | | 生成 | 优化 |
    # | --- | --- | --- |
    # | `viewState` | 跑时 `generating-*`，收尾 `review-*` | **全程 `review-*`** |
    # | 正文来源 | 模型输出的全文 | 模型输出**一节** → `job_runner` 拼回整篇后才写 |
    # | `truncated` | 如实写 | **不写**（整篇是否被截断与"改了一节"无关） |
    # | `prdReviewResult` | 完成时更新 | **保留**（优化不改审查结论） |

    def sync_optimize_completed(
        self,
        *,
        session_id: str,
        job_id: str,
        artifact: str,
        content: str,
    ) -> None:
        """优化完成：清 `activeJobId`、写回**整篇**、`viewState` 保持 `review-*`。

        ⚠️ `content` 必须是**拼好的整篇**（`job_runner._patched_document` 的产物）。
        这里不做拼接：本层不知道"哪一节"、也不该知道（拼接规则只有一份，在
        `services/section_edit.py`）。

        ⚠️ **不动 `prdReviewResult`**：优化 PRD 不重新审稿，把审查结论清掉会让顶部的
        "审核通过 / 还有 N 条意见"凭空消失 —— 那是在撒谎。
        """
        self._patch_session(
            session_id,
            {
                "activeJobId": None,
                "viewState": ARTIFACT_REVIEW_VIEW[artifact],
                "documents": {doc_kind_for(artifact): {"content": content}},
            },
        )

    def sync_optimize_failed(
        self,
        *,
        session_id: str,
        job_id: str,
        artifact: str,
        partial: str | None = None,
    ) -> None:
        """优化失败：清 `activeJobId`，`viewState` 保持 `review-*`。

        `partial` 是"已经收到的那一节拼回整篇"的结果（可为 `None` = 一个字都没收到）。
        ⚠️ **不要**把一节的片段直接当 `content` 传进来 —— 那会把整篇替换成一段话。
        拼接由 `job_runner._optimize_partial` 负责，本层只落库。
        """
        patch: dict[str, Any] = {
            "activeJobId": None,
            "viewState": ARTIFACT_REVIEW_VIEW[artifact],
        }
        if partial:
            patch["documents"] = {doc_kind_for(artifact): {"content": partial}}
        self._patch_session(session_id, patch)

    def _patch_session(
        self,
        session_id: str,
        patch: Mapping[str, Any],
    ) -> str | None:
        """把 `patch` 合并进快照并落库。返回**落库后的快照原文**；会话不存在 → `None`（不抛）。

        返回值从"是否命中"改成"新的快照文本"（02 篇）：读路径上的回填要把**它写进去的那份**
        原样返回给调用方，否则 `get_session` 还得再查一次库（而两次读之间可能有别的写入者）。
        三个既有调用方都不看返回值，所以这次改动不影响它们。

        **为什么是合并而不是覆盖**：前端那份快照里有 `messages` / `form` / `entryMode` /
        `roundIndex` 等一堆与产物无关的字段，覆盖式写入会把它们清空 ——
        而症状是"聊过的对话不见了"，排查时很难联想到是任务同步干的。

        `documents` 做**一层深合并**（只认这一层）：`{prd: {content}}` 不该把
        `{prd: {approved: true}}` 顶掉。更深的结构不合并 —— 那是"写一个通用 JSON merge"
        的开始，而这里只有这一处需要（需求里同步的字段就这几个）。

        ⚠️ 直接走 `PlanService.update`，**不过 `save_session`**：那条路径会在写入前把
        `generating-*` 降级掉，而本函数在任务开始时要写的正是 `generating-*`。
        绕开它比在那里加一个"这次别降级"的例外参数更清楚。
        """
        record = self._plans.get(session_id)
        if record is None:
            logger.warning("任务同步时会话已不存在（session=%s），跳过这次同步", session_id)
            return None
        try:
            parsed = json.loads(record.snapshot)
        except json.JSONDecodeError:  # pragma: no cover - 入库前已过 normalize_snapshot
            parsed = {}
        if not isinstance(parsed, dict):
            parsed = {}

        for key, value in patch.items():
            if key == "documents" and isinstance(value, Mapping):
                documents = parsed.get("documents")
                if not isinstance(documents, dict):
                    documents = {}
                for kind, item in value.items():
                    existing = documents.get(kind)
                    if isinstance(existing, dict) and isinstance(item, Mapping):
                        documents[kind] = {**existing, **item}
                    else:
                        documents[kind] = dict(item) if isinstance(item, Mapping) else item
                parsed["documents"] = documents
            else:
                parsed[key] = value

        text = json.dumps(parsed, ensure_ascii=False, separators=(",", ":"))
        # 摘要列跟着快照走（列表页显示"生成中 / 待审核"靠它）；`None` = 不动那一列
        entry_mode, current_stage = derive_summary_fields(text)
        updated = self._plans.update(
            session_id,
            PlanUpdate(entry_mode=entry_mode, current_stage=current_stage, snapshot=text),
        )
        return text if updated is not None else None


def doc_kind_for(artifact: str) -> str:
    """`artifact` → 快照 `documents` 里的键（`api-docs` → `api`）。

    映射本身在 `job_models.ARTIFACT_DOC_KIND`，这里只是取一次 —— 本模块**不**再写第二份。
    """
    return ARTIFACT_DOC_KIND[artifact]
