"""分片生成验收：证明"按计划分片"真的消除了截断。**会花钱**（6 次真实调用）。

整份接口文档（实测 17257 字符）与提示词套件（18354 字符）必然撞上单次输出上限被截断
（`HANDOFF.md` §4 坑 #18）。本脚本按 `GET /conversation/document-plan` 给出的计划逐片生成、
按前端的同一套逻辑拼接，然后核对三件事：

1. **每一片都没被截断**（`done.truncated == false`）
2. **该有的章节 / 文件块一个不少**（接口文档 1–9 章 + 附录 A/B；套件 00–05 各类齐全）
3. 没有重复的文档标题

为什么必须真跑：分片的**全部意义**就是"不再被截断"，而这只能由真实模型回答。
`smoke_check.py` 只管计划的形状（覆盖、不重叠、不卡死数量），管不了输出长度。

运行（在 `backend/` 下，需要后端已在 8000 起着）：

    .venv\\Scripts\\python scripts\\split_check.py
    .venv\\Scripts\\python scripts\\split_check.py --recheck   # 免费：只复查已归档的 split_*.txt

端口不是 8000 时用环境变量覆盖：`$env:HARNESS_API_BASE = 'http://127.0.0.1:8124/api/v1/conversation'`

留档写到 `validation_out/split_{api,prompts}.txt`（完整分片结果，可与被截断的
`flow_{api,prompts}.txt` 对照）。

⚠️ **上游 PRD 必须是当前结构（技能包 6 章）**：`gen_api.md` / `gen_prompts.md` 的取数来源
已按 6 章改写（"第 4 章技术规格的认证与权限子块"、"第 2 章 MVP 表 + `FR-xx`"）。
喂一份 15 章 PRD 进来，模型看到的"第 4 章"与提示词说的不是同一章 —— 那验的是
**输入过期**，不是链条本身。所以这里默认取 `prd_skill.txt`（由 `skill_prd_check.py` 生成），
找不到才退回 `flow_prd.txt` 并**打印警告**。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path

import httpx

# `services.*` 的导入要先有 backend/ 在 sys.path 上（脚本是从 backend/scripts/ 跑的）。
# 与本目录其它脚本（smoke_check / validate_prompts / verify_run_agg）同一套做法。
BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from services.stitch import stitch_parts  # noqa: E402 - 必须在 sys.path 之后导入

# 后端地址。默认 8000，可用 `HARNESS_API_BASE` 覆盖 —— 需要的原因很实际：
# 机器上常有一个**改动之前**就起着的后端占着 8000（本轮实测撞到过），
# 而那段旧代码验不了新提示词。硬编码端口只能让人去停别人的服务。
BASE = os.environ.get("HARNESS_API_BASE", "http://127.0.0.1:8000/api/v1/conversation")
BACKEND = BACKEND_DIR  # 同一个值，只是本文件下游一直用这个名字（留档路径都基于它）
# 上游 PRD：优先用**当前结构**（技能包 6 章）那一份，理由见文件头。
PRD_FILE = BACKEND / "validation_out" / "prd_skill.txt"
# 老结构（15 章 + 2 附录）那一份：只在当前结构留档缺失时兜底，并会打印警告。
LEGACY_PRD_FILE = BACKEND / "validation_out" / "flow_prd.txt"

FORM = {
    "product_name": "群聊周报助手",
    "problem": "团队群聊里信息散落，每周要人工翻聊天记录拼周报，一次两小时。",
    "target_users": "20 人以下创业团队的技术负责人，兼做项目管理",
    "core_features": "1. 自动汇总群聊消息\n2. 按人生成周报草稿\n3. 人工修改后导出",
    "platform": "web",
}
KNOWN = "目标用户：技术负责人；成功指标：每周省两小时；规模：20 人以下；约束：导出到飞书"


async def fetch_plan(client: httpx.AsyncClient, kind: str) -> dict:
    response = await client.get(f"{BASE}/document-plan", params={"kind": kind})
    response.raise_for_status()
    return response.json()


async def generate_part(
    client: httpx.AsyncClient, kind: str, part: dict, prd: str, api_content: str
) -> tuple[str, dict]:
    body: dict = {
        "form": FORM,
        "known_info": KNOWN,
        "scope": {"outline": part["outline"], "scope": part["scope"], "spec": part["spec"]},
    }
    if kind in ("api", "prompts"):
        body["prd_content"] = prd
    if kind == "prompts" and api_content:
        body["api_content"] = api_content

    path = {
        "api": "generate-api-docs-stream",
        "prompts": "generate-prompts-stream",
    }[kind]

    started = time.monotonic()
    text = ""
    done: dict = {}
    async with client.stream("POST", f"{BASE}/{path}", json=body) as response:
        response.raise_for_status()
        event = ""
        async for line in response.aiter_lines():
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                data = json.loads(line[5:].strip())
                if event == "chunk":
                    text += data.get("text", "")
                elif event == "done":
                    done = data
                elif event == "error":
                    raise RuntimeError(f"流里报错：{data}")
    elapsed = time.monotonic() - started
    print(
        f"    第 {part['index']} 片（{part['label']}）：{elapsed:.1f}s｜{len(text)} 字符｜"
        f"truncated={done.get('truncated')}｜finish_reason={done.get('finish_reason')}"
    )
    return text, done


def stitch(pieces: list[str]) -> str:
    """与前端 `stitchParts()` 同逻辑。

    ⚠️ **实现已移到 `services/stitch.py`**（这里的 `stitch` 只是别名）：后端现在有两个
    拼接调用方（本脚本与 `services/job_runner.py` 的后台任务），各写一份必然分叉，
    而症状会是"验收脚本说完整、任务里的产物却缺一段"。逻辑一个字符都没改。
    """
    return stitch_parts(pieces)


async def run_kind(client: httpx.AsyncClient, kind: str, prd: str, api_content: str = "") -> dict:
    print(f"\n=== {kind} ===")
    plan = await fetch_plan(client, kind)
    print(f"  来源：{plan['source']}｜分片数：{len(plan['parts'])}｜multi_part={plan['multi_part']}")
    pieces: list[str] = []
    truncated_any = False
    for part in plan["parts"]:
        text, done = await generate_part(client, kind, part, prd, api_content)
        pieces.append(text)
        truncated_any = truncated_any or bool(done.get("truncated"))

    full = stitch(pieces)
    (BACKEND / "validation_out" / f"split_{kind}.txt").write_text(full, encoding="utf-8")
    return {"kind": kind, "parts": len(plan["parts"]), "truncated_any": truncated_any, "text": full}


def verdict(kind: str, text: str, *, parts: int | None, truncated_any: bool | None) -> bool:
    """核对一份**拼好的**产物，返回是否通过。抽出来是为了能离线复查已有留档。

    `parts` / `truncated_any` 是**分片级**的事实：留档里只有拼好的全文，
    离线复查时传 `None`，这两项就不判（会打印说明），只判结构。
    """
    ok = True
    head = f"共 {parts} 片｜" if parts is not None else "（离线复查，分片数未知）｜"
    print(f"\n[{kind}] {head}拼接后 {len(text)} 字符｜任一片截断={truncated_any}")
    if truncated_any:
        ok = False
        print("  [X] 仍有分片被截断 —— 分片预算（_PART_BUDGET_CHARS）需要调小")
    elif truncated_any is None:
        print("  [注] 截断与否是**每一片**的事实，离线复查判不了（要看 `done.truncated`）")
    if kind == "api":
        chapters = sorted({int(m) for m in re.findall(r"^#{1,3}\s*第\s*(\d+)\s*章", text, re.M)})
        # ⚠️ 附录的判据踩过两次坑，最终定成"**标题**里出现 A / B，两种写法都认"：
        # 先是写成子串 `"附 A" in text`，把模型写的 `## 附录 A` 误判成缺附录；
        # 改成只认 `"附录 A"` 之后，本轮模型又写成了 `## 附 A`（`gen_api.md` 的章节清单
        # 里本来写的也是"附 A"），于是**又把一份完整文档报成缺附录**。
        # 教训：断言要盯"这一节在不在"（标题级），不要盯"用哪个同义词"。
        # 不用 `\b`：中文侧的词边界在 `re` 里不可靠。用 `(?![A-Za-z])` 挡住 `附 ABC`。
        found = set(re.findall(r"^#{1,3}\s*附(?:录)?\s*([AB])(?![A-Za-z])", text, re.M))
        has_appendix = {"A", "B"} <= found
        print(f"  章节命中：{chapters} | 附录命中：{sorted(found)}")
        if not has_appendix:
            ok = False
            print("  [X] 缺附录 A/B（按标题找 `## 附 A` 或 `## 附录 A`）")
        missing = [n for n in range(1, 10) if n not in chapters]
        if missing:
            ok = False
            print(f"  [X] 缺章：{missing}")
    else:
        files = re.findall(r"^=== FILE: (.+?) ===$", text, re.M)
        print(f"  文件块 {len(files)} 个：{files}")
        if len(files) < 6:
            ok = False
            print("  [X] 文件块太少（模板的目录结构至少 00–05 六类）")
    heads = re.findall(r"^# .+$", text, re.M)
    # 只报**重复**的 H1：套件是多文件产物，每个文件自带一个 H1 是正常的
    # （它们由 `=== FILE: ===` 分隔）。真正要防的是同一行标题被拼了两遍。
    dupes = sorted({h for h in heads if heads.count(h) > 1})
    if dupes:
        # 不用 ⚠️/✗ 这类符号：Windows 控制台默认 GBK，打印会抛 UnicodeEncodeError
        # 把脚本自己搞崩（真崩过一次），结论反而丢了。
        print(f"  [注意] 出现重复的 H1：{dupes[:3]}")
    # 链条：下游产物必须能追溯到 PRD 的编号（这是换结构之后最该盯的一项）
    fr = sorted({int(x) for x in re.findall(r"FR-(\d+)", text)})
    ac = sorted({int(x) for x in re.findall(r"AC-(\d+)", text)})
    print(f"  {kind}: FR 命中 {fr} | AC 命中 {ac}")
    if "api" in kind and not fr:
        ok = False
        print("  [X] 接口文档里一个 FR 编号都没有 —— 追溯链断了")
    return ok


def recheck() -> int:
    """离线复查已归档的 `split_*.txt`（**免费**，不调模型）。

    用途很实际：判据本身也会写错（本轮就把"附 A"误判成缺附录两次），
    而重跑一次要花 6 次调用 —— 有了这个入口，改完判据可以先拿旧产物验证判据，
    不用为了验证"断言写得对不对"再烧一遍钱。
    """
    ok = True
    for kind in ("api", "prompts"):
        path = BACKEND / "validation_out" / f"split_{kind}.txt"
        if not path.exists():
            print(f"[{kind}] 没有留档：{path}")
            ok = False
            continue
        ok = verdict(kind, path.read_text(encoding="utf-8"), parts=None, truncated_any=None) and ok
    print("\n结果：", "已有留档结构完整" if ok else "留档有问题，见上面 [X]")
    return 0 if ok else 1


async def main() -> int:
    print(f"后端地址：{BASE}（改端口用环境变量 HARNESS_API_BASE）")
    if PRD_FILE.exists():
        prd = PRD_FILE.read_text(encoding="utf-8")
        print(f"上游 PRD：{PRD_FILE.name}（{len(prd)} 字符，当前结构）")
    else:
        prd = LEGACY_PRD_FILE.read_text(encoding="utf-8")
        print(
            f"⚠️ 找不到 {PRD_FILE.name}，退回 {LEGACY_PRD_FILE.name}"
            f"（{len(prd)} 字符）—— 那是**老结构**的 PRD，与现在的提示词取数来源不一致，"
            "本次结果只能说明'分片本身没截断'，不能用来评判链条取数是否正确。"
            f"先跑 scripts/skill_prd_check.py 生成 {PRD_FILE.name}。"
        )
    async with httpx.AsyncClient(timeout=httpx.Timeout(900.0)) as client:
        # 接口文档先跑，再把它**拼好的全文**当作套件的上游 —— 真实流程就是这样
        # （套件消费 PRD + 接口文档）。早先这里每片只传"上一片"，上游是残的，
        # 那样验出来的套件质量不代表线上会拿到的东西。
        api_result = await run_kind(client, "api", prd)
        prompts_result = await run_kind(client, "prompts", prd, api_content=api_result["text"])
        results = [api_result, prompts_result]

    print("\n================ 结论 ================")
    ok = True
    for item in results:
        ok = (
            verdict(
                item["kind"],
                item["text"],
                parts=item["parts"],
                truncated_any=item["truncated_any"],
            )
            and ok
        )

    print("\n结果：", "分片生成完整，没有被截断" if ok else "仍有问题，见上面 [X]")
    return 0 if ok else 1


if __name__ == "__main__":
    if "--recheck" in sys.argv:
        raise SystemExit(recheck())
    raise SystemExit(asyncio.run(main()))
