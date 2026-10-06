"""交付包导出（05 篇）：把三份产物打成**可直接转手**的一个 ZIP。

## 交付包里有什么、为什么

    {产品名}-delivery.zip
    ├── README.md          阅读顺序、每份文档的版本与生成时间、未通过项摘要、过期提示
    ├── manifest.json      机读汇总（版本号、derived_from、quality_gate、review、基线时间）
    ├── PRD.md
    ├── API-Docs.md        （没有就省略，README 里说明）
    ├── Prompts.md
    └── prompts/…          提示词套件按 `=== FILE: ===` 拆成的单个文件

**为什么必须有 README 与 manifest**：这个 ZIP 是要发给别人的（开发、甲方、下游团队）。
对方打开只看得到三份 `.md`，不知道哪份是最终版、哪几项质检没过、PRD 改过之后接口文档
有没有跟上。`manifest.json` 给机器（以后接入流水线），`README.md` 给人。

## 为什么在后端组包

05 篇之前是**前端手写 ZIP**（`services/zip.ts` 自己压 deflate、算 CRC）。改到后端的三个理由：

1. **manifest 要的信息在后端**：`derived_from`、`quality_gate`、`review` 都在版本 metadata 里，
   前端要么多发几个请求、要么只拿到界面内存态里那一份（刷新后就没了）；
2. **版本快照必须来自权威源**：用户可能打开的是某一版**预览**，而交付包里该放 **current**；
3. **少一份实现**：ZIP 是标准格式，Python 的 `zipfile` 是标准库；自己写压缩是纯负债
   （前端那份 230 行随之删除）。

## 只读，不写库

本模块**不产生副作用**：读版本 + 读会话快照 → 组字节。所以自检脚本能直接喂一个临时库断言
（`smoke_check` 就是这么验的）。
"""

from __future__ import annotations

import io
import json
import re
import zipfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from services.quality_gate import split_prompt_files

__all__ = [
    "DOC_SPECS",
    "DeliveryDoc",
    "build_delivery_zip",
    "build_lineage",
    "collect_delivery_docs",
    "delivery_file_name",
    "stale_entries",
    "stale_warnings",
]

_CST = timezone(timedelta(hours=8))

#: ZIP 里文件名不允许出现的字符（Windows 解压会失败）+ 路径分隔符。
_UNSAFE_NAME = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


@dataclass(frozen=True, slots=True)
class DocSpec:
    """一份产物在交付包里的身份。

    `snapshot_key` 与 `doc_type` **不一样**（`api-docs` 在会话快照里叫 `api`）——
    这层翻译只写在这里一次，别在调用点手写映射。
    """

    doc_type: str
    role: str
    title: str
    snapshot_key: str
    file_name: str


#: 交付包里的三份产物，**顺序就是阅读顺序**（README 照这个写）。
DOC_SPECS: tuple[DocSpec, ...] = (
    DocSpec("prd", "prd", "PRD（产品需求文档）", "prd", "PRD.md"),
    DocSpec("api-docs", "api_docs", "接口文档", "api", "API-Docs.md"),
    DocSpec("prompts", "prompts", "提示词套件", "prompts", "Prompts.md"),
)


@dataclass(frozen=True, slots=True)
class DeliveryDoc:
    """一份产物的导出态。"""

    spec: DocSpec
    content: str
    version_no: int | None
    version_id: str | None
    created_at: str | None
    metadata: Mapping[str, Any]
    #: 正文是从哪来的：`version`（版本层）/ `snapshot`（只有会话字符串）/ `none`（没有）
    source: str

    @property
    def present(self) -> bool:
        return bool(self.content.strip())


def delivery_file_name(product_name: str) -> str:
    """ZIP 的文件名：`{产品名}-delivery.zip`（产品名为空或全是不安全字符时退回 `harnessprd`）。"""
    cleaned = _UNSAFE_NAME.sub("", (product_name or "").strip()).strip(" .")
    return f"{cleaned or 'harnessprd'}-delivery.zip"


def collect_delivery_docs(
    *,
    documents: Any,
    session_id: str,
    snapshot: Mapping[str, Any],
) -> list[DeliveryDoc]:
    """按 `DOC_SPECS` 收集三份产物。

    两级来源，**版本层优先**：

    | 情况 | 用谁 |
    | --- | --- |
    | 有 current 版本行 | 版本层的正文 + metadata（权威，含 quality_gate / derived_from） |
    | 只有会话里的字符串（老会话 / 迁移前） | 会话快照 `documents.<key>.content`，`version_no=None` |
    | 都没有 | 空内容（`present=False`，ZIP 里省略，README 说明） |

    ⚠️ 第二档**照样要写进 ZIP**：正文是用户的东西，"没有版本行"是我们这边的事实，
    不该让他在交付包里少一份文档（工单 §八点名了这条）。
    """
    snapshot_documents = snapshot.get("documents")
    snapshot_documents = snapshot_documents if isinstance(snapshot_documents, Mapping) else {}

    collected: list[DeliveryDoc] = []
    for spec in DOC_SPECS:
        version = None
        try:
            version = documents.get_current_version(session_id, spec.doc_type)
        except Exception:  # noqa: BLE001 - 版本层读不到不该让整包失败（会话里可能还有正文）
            version = None
        if version is not None and version.content.strip():
            collected.append(
                DeliveryDoc(
                    spec=spec,
                    content=version.content,
                    version_no=version.version_no,
                    version_id=version.id,
                    created_at=version.created_at,
                    metadata=dict(version.metadata()),
                    source="version",
                )
            )
            continue

        raw = snapshot_documents.get(spec.snapshot_key)
        content = ""
        if isinstance(raw, Mapping):
            value = raw.get("content")
            content = value if isinstance(value, str) else ""
        collected.append(
            DeliveryDoc(
                spec=spec,
                content=content,
                version_no=None,
                version_id=None,
                created_at=None,
                metadata={},
                source="snapshot" if content.strip() else "none",
            )
        )
    return collected


def stale_entries(docs: Sequence[DeliveryDoc]) -> list[tuple[str, str]]:
    """算"下游产物是否落后于上游"：`[(受影响的 doc_type, 给人看的整句)]`。

    规则只有一条：`derived_from.prd_version_no < PRD 的 current version_no` → 过期。
    比较用的是**版本号**（`version_no` 单调递增），不是内容哈希 —— 用户看得懂 v2/v3，
    也才能在界面上说清"当前接口文档基于 v2"。

    返回 `(doc_type, 文案)` 而不是只回文案：辅助 API 要按产物给出 `stale.api_docs`，
    靠**匹配句子里的中文标签**去反推是脆的（改一个字就配不上）。
    """
    by_type = {doc.spec.doc_type: doc for doc in docs}
    prd = by_type.get("prd")
    prd_no = prd.version_no if prd else None

    entries: list[tuple[str, str]] = []
    for doc_type, label in (("api-docs", "接口文档"), ("prompts", "提示词套件")):
        doc = by_type.get(doc_type)
        if doc is None or not doc.present:
            continue
        derived = doc.metadata.get("derived_from")
        derived = derived if isinstance(derived, Mapping) else {}
        base_no = derived.get("prd_version_no")
        if isinstance(base_no, int) and isinstance(prd_no, int) and prd_no > base_no:
            entries.append(
                (
                    doc_type,
                    f"PRD 已更新至 v{prd_no}，当前{label}基于 v{base_no}。建议重新生成或手动核对。",
                )
            )

    # 提示词还依赖接口文档（生成时可选带上它）—— 接口文档升版了同样要提示
    prompts = by_type.get("prompts")
    api = by_type.get("api-docs")
    if prompts is not None and prompts.present and api is not None and api.present:
        derived = prompts.metadata.get("derived_from")
        derived = derived if isinstance(derived, Mapping) else {}
        api_base = derived.get("api_docs_version_no")
        if isinstance(api_base, int) and isinstance(api.version_no, int) and api.version_no > api_base:
            entries.append(
                (
                    "prompts",
                    f"接口文档已更新至 v{api.version_no}，当前提示词套件基于 v{api_base}。"
                    "建议重新生成或手动核对。",
                )
            )
    return entries


def stale_warnings(docs: Sequence[DeliveryDoc]) -> list[str]:
    """过期提示的**文案列表**（manifest / README 用；界面走 `build_lineage`）。"""
    return [message for _, message in stale_entries(docs)]


def _doc_summary(doc: DeliveryDoc) -> dict[str, Any]:
    """manifest 里单份产物的条目（字段名与工单 §五一致）。"""
    gate = doc.metadata.get("quality_gate")
    gate = gate if isinstance(gate, Mapping) else None
    review = doc.metadata.get("review")
    review = review if isinstance(review, Mapping) else None
    derived = doc.metadata.get("derived_from")

    entry: dict[str, Any] = {
        "title": doc.spec.title,
        "present": doc.present,
        "version_no": doc.version_no,
        "version_id": doc.version_id,
        "generated_at": doc.created_at,
        "source": doc.source,
    }
    if isinstance(derived, Mapping):
        entry["derived_from"] = dict(derived)
    if gate is not None:
        # 只留摘要：整份 checks 有十几条，manifest 是**索引**不是报告（报告在 README 里）
        entry["quality_gate"] = {
            "passed": gate.get("passed"),
            "score": gate.get("score"),
            "failed": [
                {"id": item.get("id"), "label": item.get("label"), "severity": item.get("severity")}
                for item in (gate.get("checks") or [])
                if isinstance(item, Mapping) and not item.get("passed")
            ],
        }
    if review is not None:
        entry["review"] = {
            "passed": review.get("passed"),
            "summary": review.get("summary"),
            "skipped": bool(review.get("review_skipped", False)),
            "issue_count": len(review.get("issues") or []),
        }
    return entry


def build_manifest(
    *,
    docs: Sequence[DeliveryDoc],
    snapshot: Mapping[str, Any],
    session_id: str,
    exported_at: str | None = None,
) -> dict[str, Any]:
    """`manifest.json` 的内容（字段名与工单 §五的示例逐字对齐）。"""
    form = snapshot.get("form")
    form = form if isinstance(form, Mapping) else {}
    product_name = form.get("product_name")
    if not isinstance(product_name, str) or not product_name.strip():
        product_name = "未命名产品"

    documents: dict[str, Any] = {}
    for doc in docs:
        documents[doc.spec.role] = _doc_summary(doc)

    baseline_at = snapshot.get("prdBaselineConfirmedAt")
    return {
        "product_name": product_name,
        "session_id": session_id,
        "exported_at": exported_at or datetime.now(_CST).isoformat(timespec="seconds"),
        "entry_mode": snapshot.get("entryMode") or "structured",
        "exporter": "harnessprd",
        "baseline": {
            "confirmed": snapshot.get("prdBaselineConfirmed") is True,
            "confirmed_at": baseline_at if isinstance(baseline_at, str) else None,
            "prd_version_id": snapshot.get("prdBaselineVersionId"),
            "prd_version_no": snapshot.get("prdBaselineVersionNo"),
        },
        "documents": documents,
        "stale_warnings": stale_warnings(docs),
    }


def build_readme(
    *,
    docs: Sequence[DeliveryDoc],
    manifest: Mapping[str, Any],
) -> str:
    """`README.md`：一个**不熟悉本项目**的人打开 ZIP 后需要知道的全部信息。"""
    lines: list[str] = [f"# {manifest.get('product_name')} · 交付包", ""]
    lines.append(
        "本包由 harnessprd 从「需求 → PRD → 接口文档 → 提示词套件」这条链路导出，"
        "包含最终定稿的三份文档与它们的质检结论。"
    )
    lines.append("")

    lines.append("## 阅读顺序")
    lines.append("")
    lines.append("1. **PRD.md** —— 需求基线，功能编号（`FR-xx`）与验收标准（`AC-xx`）都在这里；")
    lines.append("2. **API-Docs.md** —— 从 PRD 推导的接口契约（后端 / 前端联调按它来）；")
    lines.append("3. **Prompts.md** —— 交给 AI 编码助手的实现提示词（`prompts/` 目录是同一份内容按文件拆开的版本）。")
    lines.append("")

    lines.append("## 每一份的版本与生成时间")
    lines.append("")
    lines.append("| 文档 | 版本 | 生成时间 | 结构校验 | 机器审查 |")
    lines.append("| --- | --- | --- | --- | --- |")
    for doc in docs:
        entry = (manifest.get("documents") or {}).get(doc.spec.role) or {}
        if not doc.present:
            lines.append(f"| {doc.spec.title} | — | — | — | — |")
            continue
        version = entry.get("version_no")
        gate = entry.get("quality_gate") or {}
        review = entry.get("review") or {}
        gate_text = (
            "未跑" if not gate else ("通过" if gate.get("passed") else f"{gate.get('score')} 分（有未过项）")
        )
        if not review:
            review_text = "未跑"
        elif review.get("skipped"):
            review_text = "未审核（审查器没跑成）"
        else:
            count = review.get("issue_count") or 0
            review_text = "通过" if count == 0 else f"{count} 条意见"
        lines.append(
            f"| {doc.spec.title} | {('v' + str(version)) if version else '未登记版本'} | "
            f"{doc.created_at or '—'} | {gate_text} | {review_text} |"
        )
    lines.append("")

    # 未通过项摘要：这是用户**最需要看到**的一段（比"通过/不通过"四个字有用得多）
    failed_lines: list[str] = []
    for doc in docs:
        entry = (manifest.get("documents") or {}).get(doc.spec.role) or {}
        gate = entry.get("quality_gate") or {}
        for item in gate.get("failed") or []:
            failed_lines.append(f"- **{doc.spec.title}** · {item.get('label')}（{item.get('id')}）")
        review = entry.get("review") or {}
        if review and not review.get("skipped") and (review.get("issue_count") or 0) > 0:
            summary = review.get("summary") or "审查有问题"
            failed_lines.append(f"- **{doc.spec.title}** · 机器审查 {review.get('issue_count')} 条：{summary}")
    if failed_lines:
        lines.append("## 未通过项摘要")
        lines.append("")
        lines.extend(failed_lines)
        lines.append("")
        lines.append("这些**不阻断交付**：结构校验与机器审查都只是提示，定稿与否由人决定。")
        lines.append("")
    else:
        lines.append("## 未通过项摘要")
        lines.append("")
        lines.append("结构校验与机器审查都没有未通过项。")
        lines.append("")

    warnings = list(manifest.get("stale_warnings") or [])
    lines.append("## 过期提示")
    lines.append("")
    if warnings:
        lines.extend(f"- {item}" for item in warnings)
    else:
        lines.append("下游文档与它依据的上游版本一致，没有过期提示。")
    lines.append("")

    missing = [doc.spec.title for doc in docs if not doc.present]
    if missing:
        lines.append("## 本包中缺失的文档")
        lines.append("")
        lines.append("、".join(missing) + " —— 导出时还没有生成过，本包里没有对应文件。")
        lines.append("")

    lines.append("## 一份技术说明")
    lines.append("")
    lines.append(
        "- 结构化信息见 `manifest.json`（版本号、上游版本、质检摘要、过期提示，字段名稳定，可被脚本消费）；"
    )
    lines.append("- PRD 里 `FR-xx` / `AC-xx` 是功能与验收的唯一编号，接口文档与提示词都引用它们做追溯。")
    lines.append("")
    lines.append(f"（由 harnessprd 导出 · {manifest.get('exported_at')}）")
    lines.append("")
    return "\n".join(lines)


def build_lineage(*, documents: Any, session_id: str, snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """三份产物的版本与上游关系摘要（05 篇的辅助 API 载荷）。

    ## 为什么需要它

    界面上要显示"PRD 已更新至 v3，当前接口文档基于 v2"，而**前端拿不到这个信息**：
    版本列表接口是按 `doc_type` 一次查一份的，站在接口文档页时它手上没有 PRD 的版本号。

    工单把这条 API 列为"可选"，并给了另一条路（session load 时后端算好 stale_flags）。
    这里选了独立接口：`GET /api/session/{id}` 是**每次自动保存后都会拉**的热路径，
    往里塞三个版本号的查询不划算；而这条只在审核页需要时拉一次。

    ## 文案只有一处

    `stale` 里的 `message` 与 ZIP 里 README/manifest 的过期提示**取自同一个
    `stale_warnings()`** —— 界面上看到的话与交付包里写的话必须一字不差，否则
    用户拿两份东西对照时会以为哪个算错了。
    """
    docs = collect_delivery_docs(documents=documents, session_id=session_id, snapshot=snapshot)
    documents_payload: dict[str, Any] = {}
    for doc in docs:
        derived = doc.metadata.get("derived_from")
        documents_payload[doc.spec.role] = {
            "doc_type": doc.spec.doc_type,
            "title": doc.spec.title,
            "present": doc.present,
            "version_no": doc.version_no,
            "version_id": doc.version_id,
            "generated_at": doc.created_at,
            "derived_from": dict(derived) if isinstance(derived, Mapping) else None,
        }

    # 按产物给出"这一份是否过期"（前端拿 key 取用，不必自己解析整句文案）
    stale: dict[str, Any] = {
        "api_docs": {"stale": False, "message": None},
        "prompts": {"stale": False, "message": None},
    }
    for doc_type, message in stale_entries(docs):
        role = "api_docs" if doc_type == "api-docs" else "prompts"
        stale[role] = {"stale": True, "message": message}
    return {"session_id": session_id, "documents": documents_payload, "stale": stale}


def build_delivery_zip(
    *,
    documents: Any,
    session_id: str,
    snapshot: Mapping[str, Any],
) -> tuple[bytes, str]:
    """组交付包。

    Returns:
        `(zip 字节, 文件名)` —— 路由层直接拿去当响应体与 `Content-Disposition`。
    """
    docs = collect_delivery_docs(documents=documents, session_id=session_id, snapshot=snapshot)
    manifest = build_manifest(docs=docs, snapshot=snapshot, session_id=session_id)
    readme = build_readme(docs=docs, manifest=manifest)

    buffer = io.BytesIO()
    # ⚠️ `ZIP_DEFLATED` 要 zlib（标准库自带）。中文正文压得动，别用 ZIP_STORED。
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("README.md", readme)
        archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        for doc in docs:
            if not doc.present:
                continue
            if doc.spec.doc_type == "prompts":
                files = split_prompt_files(doc.content)
                if files:
                    # 拆分版**额外**给一份：`Prompts.md` 仍然是拼好的整份（工具链两种用法都要）
                    for name, body in files:
                        safe = _UNSAFE_NAME.sub("_", name).strip("/ ") or "untitled.md"
                        archive.writestr(f"prompts/{safe}", body)
                else:
                    archive.writestr(
                        "prompts/README.txt",
                        "这份提示词套件没有 `=== FILE: ===` 分隔行，整份内容见 ../Prompts.md。",
                    )
            archive.writestr(doc.spec.file_name, doc.content)
    return buffer.getvalue(), delivery_file_name(
        str((snapshot.get("form") or {}).get("product_name") or "")
        if isinstance(snapshot.get("form"), Mapping)
        else ""
    )
