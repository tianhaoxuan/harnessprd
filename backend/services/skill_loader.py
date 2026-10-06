"""Skill 加载器：把 `skills/{id}/` 与 `config/skills.yaml` 读成可注入的 bundle。

## 分层（谁负责什么）

| 层 | 职责 |
| --- | --- |
| `skills/*/skill.yaml` | 单个 skill 的自描述：有哪些文件、各是什么角色、适用于哪些产物 |
| `config/skills.yaml` | 全局绑定：哪种产物用哪些 skill |
| **本模块** | 读文件、校验、按角色分组（**不含任何业务提示词**） |
| `services/document_service.py` | 业务 prompt 组装，调用本模块 |

## 为什么要把「读哪几份文件」从业务代码里搬出来

改造前，PRD 的四份文件路径写死在 `document_service` 的 `SKILL_PRD_ARTIFACTS` 里，
读文件的是它自己的 `_load_skill_artifact()`。后果有三个：

1. 只有 PRD 能走技能包 —— 别的产物想加，只能复制一套读文件的逻辑；
2. 「这个 skill 适用于哪些产物」**没有地方声明**，只能靠读代码；
3. 路径散在业务模块里，加一个 skill 就要再写一遍目录穿越检查
   （而那是安全边界，不是风格问题）。

现在业务代码只问一句：**给定 artifact，要注入哪些文件？**

## 路径（一律不看 cwd）

服务可能从任意工作目录启动（Docker 里是 `/app`，本机是 `backend/`，脚本里是别处），
所以路径全部用 `__file__` 定位：

- skills 根：`{repo_root}/skills/`
- 注册表：`{repo_root}/config/skills.yaml`

## 缓存

manifest、注册表、文件内容都做**进程内缓存**（技能包是静态文件，一次进程读一次就够）。
代价：**dev 期改了 skill 文件或注册表要重启进程**才生效 —— 本期接受，
因为进程启动很便宜，而每次生成都去读盘没有任何收益。

## 错误策略

| 情况 | 行为 |
| --- | --- |
| 注册表引用了不存在的 skill / artifact 写错 / `applies_to` 不含该产物 | **抛 `SkillConfigError`**（配置错误要尽早暴露，且消息里列出全部问题） |
| skill.yaml 缺字段、`id` 与目录名不一致、`artifacts[].path` 越界 | **抛 `SkillConfigError`** |
| `artifacts[].path` 指向的文件不存在 | 非可选角色**抛** `SkillArtifactNotFound`；`example` 角色只 warn 并跳过 |
| 文件读到了但是空的 | `compose_prompt_sections()` 里 warn 并跳过该段（空段落注进提示词只会浪费 token） |
"""

from __future__ import annotations

import copy
import logging
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping, Sequence

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------- 路径

#: 仓库根。`backend/services/skill_loader.py` → parents[2]。
REPO_ROOT = Path(__file__).resolve().parents[2]

SKILLS_DIRNAME = "skills"
MANIFEST_NAME = "skill.yaml"
REGISTRY_RELATIVE = Path("config") / "skills.yaml"

# ---------------------------------------------------------------- 契约常量

#: 角色 → **拼接顺序**。这不是字母序，是有意排的叙述顺序：
#: 先工作流（做什么、按什么顺序做）→ 再模板（输出什么结构）→ 再写作规则（怎么写）
#: → 最后输入契约（字段定义）。
#:
#: ⚠️ 它同时就是提示词里的顺序。改造前 `SKILL_PRD_ARTIFACTS` 的顺序正是
#: `instructions.md → prd-template.md → writing-rules.md → field-schema.json`，
#: 这里**逐字保持**，否则等于偷偷改了给模型的提示词。
ROLE_ORDER: tuple[str, ...] = ("instructions", "template", "reference", "schema", "example")

#: 每份文件前的来源标题。技能包是多文件拼装，缺这行，出问题只能靠猜"这句话来自哪份文件"。
FILE_HEADER = "----- 文件：{path} -----"

#: 声明过但缺文件时可以只警告的角色：示例是锦上添花，缺了不影响生成。
OPTIONAL_ROLES = frozenset({"example"})

#: `priority` 缺省值。数字越小越靠前。
DEFAULT_PRIORITY = 100


class SkillConfigError(RuntimeError):
    """skill 目录或 `config/skills.yaml` 本身有问题 —— 配置错误，要尽早暴露。"""


class SkillArtifactNotFound(SkillConfigError, FileNotFoundError):
    """技能包里声明的某个文件不存在。

    同时继承 `FileNotFoundError`：调用方（如 `document_plan`）习惯按 `OSError` 兜底。
    """


# ---------------------------------------------------------------- 数据结构


@dataclass(frozen=True, slots=True)
class SkillArtifact:
    """一份要注入的文件（**已读出正文**）。"""

    skill_id: str
    path: str
    role: str
    content: str


@dataclass(frozen=True, slots=True)
class SkillBundle:
    """一个产物这次要注入的全部 skill 内容。

    ⚠️ 字段是 `tuple` 而不是 `list`：bundle 可能来自缓存，
    可变容器会让调用方**改到缓存里那份**（下一次生成就莫名其妙变了）。
    要改就 `list(bundle.artifacts)` 拷一份。
    """

    artifact: str
    skills: tuple[str, ...]
    artifacts: tuple[SkillArtifact, ...]


# ---------------------------------------------------------------- 路径工具


def get_repo_root() -> Path:
    """仓库根目录。"""
    return REPO_ROOT


def get_skills_root() -> Path:
    """`skills/` 根目录（各 skill 的父目录）。"""
    return REPO_ROOT / SKILLS_DIRNAME


def get_registry_path() -> Path:
    """全局注册表 `config/skills.yaml`。"""
    return REPO_ROOT / REGISTRY_RELATIVE


def get_skill_dir(skill_id: str) -> Path:
    """`skills/{skill_id}/`（**未解析**，只拼路径）。"""
    return get_skills_root() / skill_id


def _available_skills() -> str:
    root = get_skills_root()
    if not root.is_dir():
        return "（skills/ 目录不存在）"
    names = sorted(p.name for p in root.iterdir() if p.is_dir())
    return "、".join(names) if names else "（空）"


def _require_skill_dir(skill_id: str) -> Path:
    """校验 skill 目录存在，返回**解析后**的绝对路径。"""
    skill_dir = get_skill_dir(skill_id).resolve()
    if not skill_dir.is_dir():
        raise SkillConfigError(
            f"技能目录不存在：{get_skills_root() / skill_id}；现有 skill：{_available_skills()}"
        )
    return skill_dir


def _resolve_in_skill(skill_dir: Path, relative: str) -> Path:
    """把技能目录内的相对路径解析成绝对路径，**并挡住目录穿越**。

    ⚠️ 这是安全边界，不是风格问题：不检查的话
    `read_artifact_file("prd-generator", "../../backend/.env")` 会把 API Key 读进
    system prompt，而它会跟着每一次生成请求一起发给模型。
    """
    path = (skill_dir / relative).resolve()
    if path != skill_dir and skill_dir not in path.parents:
        raise SkillConfigError(
            f"技能文件路径越界：{skill_dir.name}/{relative} —— 只允许读技能目录内的文件"
        )
    return path


def _read_yaml(path: Path) -> Any:
    """读 YAML。没装 PyYAML 时给出可执行的报错（与 `model_registry` 同一套写法）。"""
    try:
        import yaml  # noqa: PLC0415 - 局部导入：缺失时报错才具体
    except ModuleNotFoundError as exc:  # pragma: no cover - 环境问题
        raise SkillConfigError(
            f"缺少 PyYAML，无法读取 {path}。请先 `pip install pyyaml`"
            "（它是 langchain 的传递依赖，后端本就在用）。"
        ) from exc
    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle)


# ---------------------------------------------------------------- skill.yaml


def _validated_manifest(skill_id: str, skill_dir: Path, raw: Mapping[str, Any]) -> dict[str, Any]:
    """校验并**归一化**一份 skill.yaml（缺省值填进去，调用方不必再判 None）。"""
    problems: list[str] = []

    declared_id = raw.get("id")
    if declared_id != skill_id:
        problems.append(f"`id` 必须与目录名一致（应为 {skill_id!r}），现在是 {declared_id!r}")

    artifacts: list[dict[str, str]] = []
    raw_artifacts = raw.get("artifacts")
    if not isinstance(raw_artifacts, list) or not raw_artifacts:
        problems.append("`artifacts` 必须是非空列表（要注入哪些文件、各是什么角色）")
    else:
        for index, item in enumerate(raw_artifacts):
            where = f"artifacts[{index}]"
            if not isinstance(item, Mapping):
                problems.append(f"`{where}` 必须是映射，形如 `{{path: ..., role: ...}}`")
                continue
            path = item.get("path")
            role = item.get("role")
            if not isinstance(path, str) or not path.strip():
                problems.append(f"`{where}.path` 必须是非空字符串")
                continue
            if not isinstance(role, str) or not role.strip():
                problems.append(f"`{where}.role` 必须是非空字符串")
                continue
            if set(item) - {"path", "role"}:
                logger.warning("skill %s 的 %s 里有本加载器不认识的键：%s", skill_id, where, sorted(set(item) - {"path", "role"}))
            # 路径越界在这里就拦下：manifest 是声明，声明不该指向技能目录之外
            try:
                _resolve_in_skill(skill_dir, path.strip())
            except SkillConfigError as exc:
                problems.append(f"`{where}.path` 不合法：{exc}")
                continue
            artifacts.append({"path": path.strip(), "role": role.strip()})

    applies_to = raw.get("applies_to")
    if not isinstance(applies_to, list) or any(
        not isinstance(item, str) or not item.strip() for item in applies_to
    ):
        problems.append(
            "`applies_to` 必须是字符串列表（可以是空列表 = 还没打算给任何产物用）；"
            "注册表绑定本 skill 时会与它交叉校验"
        )
        applies_to = []

    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        problems.append(f"`enabled` 必须是 true / false，现在是 {enabled!r}")

    priority = raw.get("priority", DEFAULT_PRIORITY)
    if isinstance(priority, bool) or not isinstance(priority, int):
        problems.append(f"`priority` 必须是整数（越小越靠前），现在是 {priority!r}")
        priority = DEFAULT_PRIORITY

    if problems:
        raise SkillConfigError(
            f"{MANIFEST_NAME} 不合法：{skill_dir}\n- " + "\n- ".join(problems)
        )

    return {
        **raw,
        "id": skill_id,
        "artifacts": artifacts,
        "applies_to": [item.strip() for item in applies_to],
        "enabled": enabled,
        "priority": priority,
    }


@lru_cache(maxsize=None)
def _manifest_cached(skill_id: str) -> dict[str, Any]:
    skill_dir = _require_skill_dir(skill_id)
    manifest_path = skill_dir / MANIFEST_NAME
    if not manifest_path.is_file():
        raise SkillConfigError(f"技能缺 {MANIFEST_NAME}：{manifest_path}")
    raw = _read_yaml(manifest_path)
    if not isinstance(raw, Mapping):
        raise SkillConfigError(f"{manifest_path} 的顶层必须是映射（mapping）")
    return _validated_manifest(skill_id, skill_dir, raw)


def load_skill_manifest(skill_id: str) -> dict[str, Any]:
    """读并校验 `skills/{skill_id}/skill.yaml`。

    Returns:
        **深拷贝**：manifest 会缓存，直接把手里的 dict 交出去等于让调用方改缓存。
    """
    return copy.deepcopy(_manifest_cached(skill_id))


# ---------------------------------------------------------------- 注册表


def validate_registry_data(data: Mapping[str, Any]) -> dict[str, tuple[str, ...]]:
    """校验一份注册表数据（不读盘，便于自检直接喂构造数据）。

    Returns:
        `{artifact: (skill_id, ...)}`，顺序即注册表里的书写顺序（**未经 priority 排序**）。

    Raises:
        SkillConfigError: 列出**全部**问题而不是只报第一个 —— 配置错误一次改完，
            比改一个跑一次快得多。
    """
    if not isinstance(data, Mapping):
        raise SkillConfigError(f"{REGISTRY_RELATIVE} 的顶层必须是映射（mapping）")

    bindings = data.get("bindings")
    if not isinstance(bindings, Mapping) or not bindings:
        raise SkillConfigError(f"{REGISTRY_RELATIVE} 缺 `bindings`（或它不是一个非空映射）")

    # 延迟导入：job_models 只提供 artifact 常量，但没必要在模块加载期就拖进来
    from services.job_models import JOB_ARTIFACTS  # noqa: PLC0415

    known = tuple(JOB_ARTIFACTS)
    problems: list[str] = []

    unknown = sorted(set(bindings) - set(known))
    if unknown:
        problems.append(
            f"`bindings` 里有不是 Job artifact 的 key：{'、'.join(unknown)}；"
            f"合法取值只有这 {len(known)} 个：{'、'.join(known)}"
        )
    missing = [artifact for artifact in known if artifact not in bindings]
    if missing:
        problems.append(
            f"`bindings` 缺这些 artifact：{'、'.join(missing)} —— "
            "没有 skill 就写空列表（`skills: []`），别省掉 key："
            "省掉的后果是「这个产物到底有没有规范」要靠翻代码回答"
        )

    resolved: dict[str, tuple[str, ...]] = {}
    for artifact, entry in bindings.items():
        if artifact not in known:
            continue
        if not isinstance(entry, Mapping):
            problems.append(f"`bindings.{artifact}` 必须是映射，形如：\n    {artifact}:\n      skills: []")
            continue
        extra = sorted(set(entry) - {"skills"})
        if extra:
            logger.warning("`bindings.%s` 里有本加载器不认识的键（已忽略）：%s", artifact, extra)
        raw_ids = entry.get("skills")
        if raw_ids is None:
            problems.append(f"`bindings.{artifact}` 缺 `skills` 键（没有 skill 就写 `skills: []`）")
            continue
        if not isinstance(raw_ids, list) or any(
            not isinstance(item, str) or not item.strip() for item in raw_ids
        ):
            problems.append(f"`bindings.{artifact}.skills` 必须是字符串列表")
            continue

        ids = [item.strip() for item in raw_ids]
        duplicated = sorted({item for item in ids if ids.count(item) > 1})
        if duplicated:
            problems.append(f"`bindings.{artifact}.skills` 里有重复的 skill id：{'、'.join(duplicated)}")

        for skill_id in ids:
            try:
                manifest = _manifest_cached(skill_id)
            except SkillConfigError as exc:
                problems.append(f"`bindings.{artifact}` 引用的 skill `{skill_id}` 加载失败：{exc}")
                continue
            if artifact not in manifest["applies_to"]:
                problems.append(
                    f"skill `{skill_id}` 的 `applies_to` 里没有 `{artifact}`"
                    f"（它声明的是 {manifest['applies_to'] or '空'}）——"
                    "要么改注册表，要么改那个 skill.yaml，两边必须一致"
                )
        resolved[artifact] = tuple(ids)

    if problems:
        raise SkillConfigError(
            f"{REGISTRY_RELATIVE} 有 {len(problems)} 处问题：\n- " + "\n- ".join(problems)
        )
    return resolved


@lru_cache(maxsize=None)
def _registry_raw_cached() -> dict[str, Any]:
    path = get_registry_path()
    if not path.is_file():
        raise SkillConfigError(
            f"找不到技能注册表：{path}（期望 {REGISTRY_RELATIVE}，相对仓库根 {REPO_ROOT}）"
        )
    data = _read_yaml(path)
    if not isinstance(data, Mapping):
        raise SkillConfigError(f"{path} 的顶层必须是映射（mapping），里面应有 `bindings`")
    return dict(data)


def load_registry() -> dict[str, Any]:
    """读注册表原文（**深拷贝**：它会缓存）。"""
    return copy.deepcopy(_registry_raw_cached())


@lru_cache(maxsize=None)
def validate_registry() -> dict[str, tuple[str, ...]]:
    """读**并校验**注册表（含 `applies_to` 交叉校验）。结果按进程缓存。

    Raises:
        SkillConfigError: 见 `validate_registry_data`。
    """
    return validate_registry_data(_registry_raw_cached())


def resolve_skill_ids(artifact: str) -> tuple[str, ...]:
    """给定 artifact，返回要生效的 skill id（已按 `priority` 排序、跳过 `enabled: false`）。

    排序键是 `(priority, 注册表里的书写顺序)`：priority 相同时保持书写顺序，
    否则"改了一下 yaml 的先后"会变成不可预期的行为。
    """
    bindings = validate_registry()
    if artifact not in bindings:
        raise SkillConfigError(
            f"artifact {artifact!r} 不在 {REGISTRY_RELATIVE} 的 bindings 里；"
            f"已知：{'、'.join(sorted(bindings))}"
        )

    ranked: list[tuple[int, int, str]] = []
    for index, skill_id in enumerate(bindings[artifact]):
        manifest = _manifest_cached(skill_id)
        if not manifest["enabled"]:
            logger.debug("skill `%s` 已停用（enabled: false），本次跳过", skill_id)
            continue
        ranked.append((manifest["priority"], index, skill_id))
    ranked.sort()
    return tuple(skill_id for _, _, skill_id in ranked)


# ---------------------------------------------------------------- 读文件 / 组装


@lru_cache(maxsize=None)
def _artifact_text_cached(skill_id: str, relative_path: str) -> str:
    skill_dir = _require_skill_dir(skill_id)
    path = _resolve_in_skill(skill_dir, relative_path)
    if not path.is_file():
        available = "、".join(
            sorted(str(p.relative_to(skill_dir)) for p in skill_dir.rglob("*") if p.is_file())
        )
        raise SkillArtifactNotFound(
            f"技能文件不存在：{skill_id}/{relative_path}；该技能包现有文件：{available or '（空）'}"
        )
    return path.read_text(encoding="utf-8").strip()


def read_artifact_file(skill_id: str, relative_path: str) -> str:
    """读技能包里的一个文件。`relative_path` 是**相对技能目录**的路径。

    给"只要一份文件"的调用方用（例如摘要回填只要 `field-schema.json`）。
    正文按 `(skill_id, relative_path)` 进程内缓存。

    Raises:
        SkillConfigError: 技能目录不存在，或路径逃出技能目录。
        SkillArtifactNotFound: 文件不存在（消息里列出该技能包现有文件）。
    """
    return _artifact_text_cached(skill_id, relative_path)


def load_skill_bundle(artifact: str) -> SkillBundle:
    """给定 artifact，把绑定的 skill 文件全部读出来。

    校验（在这一步集中做）：
    - 注册表本身合法（`validate_registry_data`，含 `applies_to` 交叉校验）
    - skill 目录存在、`skill.yaml` 可解析且 `id` 与目录名一致
    - `artifacts[].path` 在技能目录内且**文件存在**（`example` 角色缺失只 warn）

    Returns:
        `SkillBundle`；`skills` 为空元组 = 这个产物没有绑定任何 skill（合法状态，
        接口文档 / 提示词套件现在就是这样），调用方按"没有技能包"处理。
    """
    skill_ids = resolve_skill_ids(artifact)
    collected: list[SkillArtifact] = []

    for skill_id in skill_ids:
        for item in _manifest_cached(skill_id)["artifacts"]:
            path, role = item["path"], item["role"]
            try:
                content = _artifact_text_cached(skill_id, path)
            except SkillConfigError as exc:
                if role in OPTIONAL_ROLES:
                    logger.warning(
                        "技能文件缺失，按可选角色跳过：%s/%s（role=%s）：%s",
                        skill_id,
                        path,
                        role,
                        exc,
                    )
                    continue
                raise
            collected.append(
                SkillArtifact(skill_id=skill_id, path=path, role=role, content=content)
            )

    return SkillBundle(artifact=artifact, skills=skill_ids, artifacts=tuple(collected))


def bundle_to_injected_meta(
    bundle: SkillBundle, *, paths: Sequence[str] | None = None
) -> list[dict[str, Any]]:
    """把 bundle 压成 `run_summary.injected_skills` 的形状（**按 skill 分组**）。

    ```python
    [{"skill_id": "api-docs-generator", "version": "1.0.0",
      "artifacts": ["instructions.md", "references/team-api-guidelines.md"]}]
    ```

    为什么按 skill 分组而不是平铺文件：要回答的是「这次生成用了哪份规范、哪个版本」，
    文件清单只是佐证。`version` 现在没有 pin 机制，但**先记下来**才能事后定位
    "这份产物是按哪一版规范生成的"。

    Args:
        paths: 只登记这几份（相对技能目录的路径）。给"只注入了其中一份"的场景用 ——
            摘要回填只注入 `role: schema` 那一份，登记整包会让汇总夸大实际注入量。
            不传 = 全部登记。

    Returns:
        按 `bundle.skills` 的顺序排列（与提示词里的先后一致）；
        **没注入任何文件的 skill 不出现**（`enabled: false`，或文件都是可选角色且缺失）。
        空 bundle → 空列表（接口文档/提示词之外、或绑成空列表的产物就是这种状态）。
    """
    wanted = None if paths is None else set(paths)
    grouped: dict[str, list[str]] = {}
    for item in bundle.artifacts:
        if wanted is not None and item.path not in wanted:
            continue
        grouped.setdefault(item.skill_id, []).append(item.path)

    meta: list[dict[str, Any]] = []
    for skill_id in bundle.skills:
        registered = grouped.get(skill_id)
        if not registered:
            continue
        manifest = _manifest_cached(skill_id)
        meta.append(
            {
                "skill_id": skill_id,
                "version": str(manifest.get("version") or ""),
                "artifacts": registered,
            }
        )
    return meta


def compose_prompt_sections(bundle: SkillBundle) -> dict[str, str]:
    """把 bundle 按 `role` 分组，返回**可直接拼进提示词**的文本。

    ```python
    {
      "instructions": "----- 文件：instructions.md -----\\n\\n…",
      "template":     "----- 文件：references/prd-template.md -----\\n\\n…",
      "reference":    "…",
      "schema":       "…",
    }
    ```

    规则：

    - **顺序**按 `ROLE_ORDER`（instructions → template → reference → schema → example）；
      `skill.yaml` 里声明过的自造角色排在已知角色之后（按声明顺序）。
      ⚠️ 调用方**不要再排序**：这个顺序就是提示词顺序。
    - 同一个角色有多份文件时，按 `skill.yaml` 的声明顺序，用空行相接。
    - 每份文件都带 `----- 文件：<相对路径> -----` 来源标题（多文件拼装必须能溯源）。
    - 空文件 / 只有空白的文件：warn 并**跳过该段**，不往提示词里注空段落。
      读不到的**非可选**文件不会走到这里（`load_skill_bundle` 已经抛了），
      所以这里只做最后一道防线，不吞配置错误。
    """
    grouped: dict[str, list[SkillArtifact]] = {}
    for item in bundle.artifacts:
        if not item.content.strip():
            logger.warning(
                "技能文件是空的，这一段不注入：%s/%s（role=%s）",
                item.skill_id,
                item.path,
                item.role,
            )
            continue
        grouped.setdefault(item.role, []).append(item)

    remaining = [role for role in grouped if role not in ROLE_ORDER]
    if remaining:
        logger.debug("bundle 里有自造角色（排在已知角色之后）：%s", remaining)

    sections: dict[str, str] = {}
    for role in (*ROLE_ORDER, *remaining):
        items = grouped.get(role)
        if not items:
            continue
        sections[role] = "\n\n".join(
            f"{FILE_HEADER.format(path=item.path)}\n\n{item.content}" for item in items
        )
    return sections
