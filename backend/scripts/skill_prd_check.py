"""PRD 两条路径的对照：用**同一份输入**各跑一次，产物留档、便于并排比。

**会花钱**：每次 1 次真实调用（技能路径实测约 8–10 秒，15 章路径约 15–25 秒）。

| 模式 | 命令 | 走的提示词 | 产物 |
| --- | --- | --- | --- |
| 技能包（产品默认） | `python scripts/skill_prd_check.py` | `skills/prd-generator/`（6 章 + FR/AC） | `validation_out/prd_skill.txt` |
| 回退路径 | `python scripts/skill_prd_check.py --legacy` | `gen_common + gen_prd`（15 章 + 2 附录） | `validation_out/prd_15ch.txt` |

不带参数时**不写死 use_skill，而是跟着 `settings.prd_use_skill` 走** ——
这样它验的就是产品真实默认，而不是脚本自己的假设。

两条路径的产物结构互不兼容，**同一时刻只该有一套在跑**；这个脚本的作用是留一份
"另一套长什么样"的证据，以及回退时能立刻复现。切换结构要同步 `core/config.py` 里
`prd_use_skill` 注释列出的那几处口径。

用法（Key 走环境变量，**不落盘**）：

    cd backend
    $env:DEEPSEEK_API_KEY='sk-...'
    .venv\\Scripts\\python scripts\\skill_prd_check.py            # 产品默认（技能包）
    .venv\\Scripts\\python scripts\\skill_prd_check.py --legacy   # 回退路径（15 章）
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from core.config import get_settings  # noqa: E402
from services.document_service import DocumentService  # noqa: E402
from services.llm import StreamOutcome  # noqa: E402

# 与之前那次 15 章 PRD 用同一份输入，便于对照。
# ⚠️ 这里用的是项目 20 题表单的字段名，而技能包的输入契约是另外 8 个字段
# （`skills/prd-generator/references/field-schema.json`）。运行时两者都只是
# `{form_summary}` 里的一段文本，没有显式映射 —— 这是有意保持的现状（见 HANDOFF §10 决策 7）：
# 先把结构切过去，字段映射单独做。所以两条路径喂的是同一份文本，对照才有意义。
FORM = {
    "product_name": "群聊周报助手",
    "problem": "团队群聊里进展、决策、阻塞信息散落，每周要人工翻聊天记录拼周报，一次约两小时，还容易漏。",
    "target_users": "20 人以下创业团队的技术负责人，兼做项目管理",
    "core_features": "1. 汇总指定群聊在指定时间范围内的消息\n2. 按人 / 项目生成周报草稿\n3. 人工修改后导出到飞书",
    "success_metrics": "上线首个完整使用周，负责人撰写周报耗时从约 2 小时降到 10 分钟以内；导出成功率 100%",
    "platform": "Web 网站",
    "need_auth": "需要：账号密码注册登录",
    "need_database": "需要：要长期保存用户数据",
    "tech_stack": "前端 React + TypeScript，后端 Python FastAPI，PostgreSQL",
    "existing_system": "全新项目，没有历史包袱",
    "page_count": "4–8 个",
    "style_preference": "简洁克制（类似 Linear、Notion）",
    "integrations": "飞书开放平台（导出周报文档）",
    "core_entities": "用户：id、姓名、邮箱、密码哈希；群聊配置：id、名称、归属人；汇总结果：id、时间范围、消息数；周报草稿：id、汇总 id、正文、状态",
    "api_convention": "RESTful + JWT",
    "scale_expectation": "单团队 20 人以内，消息量每天数百条",
    "hard_constraints": "必须能导出到飞书；部署在内网服务器，不上公有云",
    "compliance": "手机号必须加密存储；需要操作审计日志；数据不能出境",
    "engineering_conventions": "前后端分离，接口先定契约再实现，关键路径补测试",
}

KNOWN_INFO = """主场景动线（S1）：技术负责人登录 → 绑定一个群聊并选定时间范围 → 触发汇总 →
在草稿页看到按人 / 项目组织的周报草稿 → 手工调整措辞 → 导出到飞书 → 在飞书里收到文档链接。

角色与权限（S2）：两种角色。owner（群聊归属人）可绑定 / 解绑群聊、查看与自己有关的周报；
member 只读自己被汇总到的条目，不能改他人草稿。未登录用户只能看到登录页。

状态流转（S3）：周报草稿 draft → editing（用户开始手改）→ exported（导出成功）；
导出失败回落到 editing。汇总任务 running → succeeded / failed，failed 可重试，重试次数不限但需人工触发。

优先级分级（S3）：P0 是账号注册登录、绑定群聊并汇总、生成周报草稿、导出到飞书；
P1 是人工编辑草稿、导出历史记录；P2 是按项目维度组织草稿、多群对比。

不做清单（S3）：自动把周报发到群里、多人协同编辑同一份草稿、除飞书外的其它导出目标、
群聊消息的自动分类打标。

边界与异常（S2）：时间跨度超过 31 天时拒绝并提示；群聊在选定范围内没有任何消息时给出空态而不是空草稿；
导出时飞书侧限流（每分钟 5 次）需退避重试；同一群聊并发触发两次汇总时第二次返回既有任务不新建。

页面清单（S4）：登录页 /login、表单页 /setup、汇总进度页 /aggregations/:id、
周报草稿页 /reports/:id、导出历史页 /exports。

非功能（S4）：汇总 500 条消息 30 秒内返回；导出成功率 100%（含重试）；
服务可用性按工作时段 99% 要求；支持 Chrome / Edge 最近两个大版本，屏幕宽度 1280 起。

验收口径（S4）：每条 P0 功能都要能用一句话判定做完没有，例如「绑定群聊后 30 秒内能看到按人分组的草稿」。

技术约束（S4）：内网部署，容器化；必须用 PostgreSQL 存用户与草稿；导出走飞书开放平台，
需要应用凭证由管理员配置。"""


async def main() -> int:
    # 不带参数：跟产品默认（`settings.prd_use_skill`，现在是技能包）。
    # `--legacy`（旧写法 `--default` 仍认）走 15 章那条回退路径。
    # 两者共用同一份输入，便于并排比。
    legacy = "--legacy" in sys.argv or "--default" in sys.argv
    use_skill = False if legacy else None
    mode = "15ch" if legacy else "skill"
    out_name = f"prd_{mode}.txt"

    settings = get_settings()
    print(f"mode={mode} use_skill={use_skill} "
          f"(settings.prd_use_skill={settings.prd_use_skill}) "
          f"provider={settings.llm_provider} model={settings.active_llm_model} "
          f"key_configured={settings.llm_configured}")

    service = DocumentService()
    outcome = StreamOutcome()
    started = time.monotonic()
    parts: list[str] = []
    async for chunk in service.generate_prd_stream(
        form=FORM,
        known_info=KNOWN_INFO,
        open_questions="是否支持多群对比（用户跳过）",
        conflicts="表单说 Web 网站，对话里提到可能要小程序 —— 以对话为准，本期先做 Web",
        outcome=outcome,
        use_skill=use_skill,
    ):
        parts.append(chunk)
    elapsed = time.monotonic() - started

    text = "".join(parts)
    out = BACKEND / "validation_out" / out_name
    out.write_text(text, encoding="utf-8")

    chapters = re.findall(r"^## (.+)$", text, re.M)
    summary = {
        "mode": mode,
        "elapsed_s": round(elapsed, 1),
        "chars": len(text),
        "lines": len(text.splitlines()),
        "headings": len(chapters),
        "first_level_headings": chapters,
        "finish_reason": outcome.finish_reason,
        "truncated": outcome.truncated,
        "pending_marks": text.count("[待确认]"),
        "fr_ids": len(set(re.findall(r"FR-\d+", text))),
        "ac_ids": len(set(re.findall(r"AC-\d+", text))),
        "tables": sum(1 for line in text.splitlines() if line.startswith("|")),
        "tail_80": text[-80:],
        "saved_to": str(out),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
