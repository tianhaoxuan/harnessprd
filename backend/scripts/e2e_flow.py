"""端到端全流程验收：**真实浏览器 + 真实模型**，分阶段可续跑。

这是 `validation_out/flow_{prd,api,prompts}.txt` 那三份端到端留档的生成器
（此前是一次性脚本、已删，`validation_out/README.md` 把"不可复现"列为已知缺口 —— 现在补上了）。

它做的事和用户点页面完全一样：填表 → 追问到 `stage_status=done` → 生成三份产物
→ 逐份通过 → 完成页 → 三份下载，并在每一步断言。**不 mock、不注入状态。**

## 用法

```powershell
# 前置：后端在 8000、前端 Vite 在 5173 都已经起着
cd backend
.venv\\Scripts\\python scripts\\e2e_flow.py all        # 一次跑完（约 3–5 分钟，含 5 次真实调用）
.venv\\Scripts\\python scripts\\e2e_flow.py status     # 只看当前会话状态，不调模型
.venv\\Scripts\\python scripts\\e2e_flow.py diag       # 现场诊断：DOM / 存储 / 未捕获错误
.venv\\Scripts\\python scripts\\e2e_flow.py api        # 只跑某一阶段
```

阶段：`chat` / `prd` / `api` / `prompts` / `finish` / `status` / `diag` / `trace`。

**会话状态存在浏览器 profile 的 localStorage 里**（`WORK/profile`），所以阶段之间是续跑：
关掉进程再跑下一阶段，上一阶段的产物还在。

## 为什么分阶段

真模型一次整份生成约 **25 秒**（实测），整条链条（多轮对话 + 三份产物）几分钟。
一次跑完的话，任何一步超时都会把之前所有结果一起丢掉（stdout 重定向到文件时是**块缓冲**的）。

## 环境变量

| 名字 | 默认 | 说明 |
| --- | --- | --- |
| `E2E_CHROME` | 自动探测 ms-playwright 的 chromium | 浏览器可执行文件路径 |
| `E2E_APP_URL` | `http://127.0.0.1:5173/` | 前端地址 |
| `E2E_WORK` | `%LOCALAPPDATA%\harnessprd-e2e` | profile / 截图 / 下载 / 结果 JSON 的存放目录。**必须跨进程稳定**，否则阶段之间接不上（别用 `TEMP`，原因见 `_work_dir()`） |

## ⚠️ 三个必须知道的坑（都是实测踩出来的）

1. **跑这个脚本期间不要改 `frontend/src/` 下的任何文件。** Vite 的 HMR 会整页重载，
   重载会把 `window.__h` 抹掉（脚本靠它操作页面）。脚本已经做了重载后自动重装，
   但**正在生成的那一次请求会被打断** —— 现象是"生成完了但正文是空的"，
   而真正的原因是你自己刚保存了文件。
2. **每个阶段结束时会 `Browser.close` 优雅关闭，不要改成 `taskkill`。**
   Chrome 的 localStorage 是 LevelDB、**异步落盘**，硬杀会丢掉最近的写入 ——
   实测把刚生成的产物整个丢掉，下一次启动读回旧快照，现象与应用"没保存"一模一样。
3. **`status()` 是内存派生的，`docText()` 读的是 localStorage，两者会短暂不一致**
   （落盘有 800ms 防抖 + 最长 2s 强制写）。所以别在状态刚翻转时读正文就下结论，
   要**轮询等它落盘**（`phase_doc` 就是这么做的）。

## 窗口与沙箱

Chromium 需要创建命名管道，**在受限沙箱下会以 `mojo platform_channel Check failed:
拒绝访问 (0x5)` 直接退出**。跑不通时先看这一点，而不是怀疑应用。
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import websockets

# 产物留档落在仓库里（要给人看）；profile / 截图 / 下载这些临时物落在系统临时目录，
# 免得往仓库里塞一个 Chrome profile —— 它还会把 Vite 的 watcher 搞崩（EBUSY，实测过）。
REPO = Path(__file__).resolve().parents[2]
BACKEND_OUT = REPO / "backend" / "validation_out"


def _work_dir() -> Path:
    """profile / 截图 / 下载 / 结果 JSON 放哪。

    ⚠️ **不要用 `tempfile.gettempdir()`**：在受限沙箱里（本项目的验收环境就是）
    `TEMP` 可能被指到**每次命令都不同**的目录，于是每个阶段都拿到新 profile ——
    "分阶段续跑"直接失效，表现为每一阶段都从空会话开始（本轮实测踩到，
    当时先怀疑是应用没保存）。用 `LOCALAPPDATA` 才跨进程稳定。
    """
    base = os.environ.get("E2E_WORK") or os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
    return Path(base) / "harnessprd-e2e"


WORK = _work_dir()
DOWNLOADS = WORK / "downloads"
RESULTS_FILE = WORK / "flow_results.json"

APP_URL = os.environ.get("E2E_APP_URL", "http://127.0.0.1:5173/")
CDP_PORT = 9222


def _find_chrome() -> str:
    """找 Chromium：优先 `E2E_CHROME`，否则取 ms-playwright 里最新的一个。"""
    override = os.environ.get("E2E_CHROME")
    if override:
        return override
    root = Path(os.environ.get("LOCALAPPDATA", "")) / "ms-playwright"
    candidates = sorted(root.glob("chromium-*/chrome-win64/chrome.exe"))
    if candidates:
        return str(candidates[-1])
    raise SystemExit(
        "找不到 Chromium。设 E2E_CHROME 指向 chrome.exe，"
        f"或确认 {root} 下有 chromium-*/chrome-win64/chrome.exe"
    )


CHROME = _find_chrome()

STATE: dict = {}


def log(text: str) -> None:
    print(text, flush=True)


def save_results() -> None:
    RESULTS_FILE.write_text(
        json.dumps({**STATE, "results": RESULTS}, ensure_ascii=False, indent=2), encoding="utf-8"
    )


RESULTS: list[dict] = []


def ok(name: str, cond: bool, detail: str = "") -> None:
    RESULTS.append({"name": name, "pass": bool(cond), "detail": detail})
    log(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  <-- {detail}" if detail and not cond else ""))
    save_results()


def note(text: str) -> None:
    STATE.setdefault("observations", []).append(text)
    log(f"  [观察] {text}")
    save_results()


def page_ws(timeout: float = 30.0) -> str | None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            targets = json.load(urllib.request.urlopen(f"http://127.0.0.1:{CDP_PORT}/json", timeout=2))
            for t in targets:
                if t.get("type") == "page" and "127.0.0.1:5173" in t.get("url", ""):
                    return t["webSocketDebuggerUrl"]
        except Exception:
            pass
        time.sleep(0.4)
    return None


HELPERS = r"""
window.__errs = window.__errs || [];   // 首页加载时 new-document 钩子还没生效，先补一个
window.__h = (() => {
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const $ = (s) => document.querySelector(s);
  const $$ = (s) => Array.from(document.querySelectorAll(s));
  const body = () => document.body.textContent || '';
  const setNative = (el, v) => {
    Object.getOwnPropertyDescriptor(el.constructor.prototype, 'value').set.call(el, v);
    el.dispatchEvent(new Event('input', { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
  };
  const waitFor = async (pred, ms) => {
    for (let i = 0; i < ms / 100; i++) { if (pred()) return true; await sleep(100); }
    return false;
  };
  const status = () => $('[data-testid=document-review]')?.dataset.status;
  const startBtn = () => $('[data-testid=start-prd]');
  const action = (k) => $(`[data-action=${k}]`);
  const rows = () => $$('[data-testid=message-list] li[data-role]').length;
  // ⚠️ **必须能单独数 AI 行**：用户消息是**乐观上屏**的（点发送立刻出现），
  // 所以"行数 ≥ 1"在 AI 回复到达**之前**就成立了。本轮实测踩到：以为回复到了、
  // 立刻去读输出，读到 0 字符，还量出"首轮耗时 0.1s"这种不可能的数值。
  const aiRows = () => $$('[data-testid=message-list] li[data-role=ai]').length;
  const session = () => {
    const raw = localStorage.getItem('harnessprd:development:1:session');
    return raw ? JSON.parse(raw).payload : null;
  };
  const lastAiRaw = () => {
    const s = session();
    if (!s) return null;
    for (let i = s.messages.length - 1; i >= 0; i--) {
      if (s.messages[i].role === 'ai') return s.messages[i].content;
    }
    return null;
  };
  const stageStatus = () => {
    const raw = lastAiRaw();
    if (!raw) return null;
    try { return JSON.parse(raw).stage_status ?? null; } catch { return 'PARSE_FAIL'; }
  };
  const docText = (kind) => (session()?.documents?.[kind]?.content) || '';
  const viewKey = () => {
    // viewState 不在 localStorage 里（只存 session），所以用界面特征判断当前在哪一屏
    if ($('[data-testid=done-panel]')) return 'done';
    if ($('[data-testid=document-review]')) return $('[data-testid=document-review]').dataset.status;
    if ($('[data-testid=message-list]')) return 'chatting';
    if ($('#field-product_name')) return 'form';
    return 'unknown';
  };
  // 当前审核的是**哪一份**产物。⚠️ 不要用"页面正文里有没有『提示词套件』字样"来判断 ——
  // 步骤条永远包含三个产物名，那种断言恒为真，会导致"其实还在接口文档页"被当成
  // "已推进到提示词套件页"，然后点了错的生成按钮（本轮真踩过）。
  const docTitle = () => ($('[data-testid=document-review] h2')||{}).textContent || '';
  const docKind = () => {
    const t = docTitle();
    if (t.includes('PRD')) return 'prd';
    if (t.includes('接口')) return 'api';
    if (t.includes('提示词')) return 'prompts';
    return 'unknown';
  };
  return { sleep, $, $$, body, setNative, waitFor, status, startBtn, action, rows, aiRows, session, lastAiRaw, stageStatus, docText, viewKey, docTitle, docKind };
})();
'ok'
"""


async def with_page(fn):
    """起浏览器 → 装 helpers → 执行 fn(ev, shot) → 关掉。"""
    errlog = open(WORK / "chrome.log", "a", encoding="utf-8", errors="replace")
    proc = subprocess.Popen(
        [
            CHROME, "--headless=new", "--disable-gpu", "--no-sandbox", "--no-first-run",
            "--window-size=1280,1100", f"--remote-debugging-port={CDP_PORT}",
            f"--user-data-dir={WORK}\\profile", APP_URL,
        ],
        stdout=subprocess.DEVNULL, stderr=errlog,
    )
    try:
        ws_url = page_ws()
        if not ws_url:
            log("CDP_NO_PAGE_TARGET")
            return 1
        async with websockets.connect(ws_url, max_size=64 * 1024 * 1024) as ws:
            counter = {"n": 0}

            async def call(method, params=None):
                counter["n"] += 1
                mid = counter["n"]
                await ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
                while True:
                    msg = json.loads(await ws.recv())
                    if msg.get("id") == mid:
                        return msg

            await call("Runtime.enable")
            await call("Page.enable")
            # ⚠️ 把 helpers **一起**塞进 new-document 脚本：Vite HMR 会因为改前端源码
            # 自动整页重载，而 `window.__h` 是普通全局 —— 重载就没了，之后每个 evaluate
            # 都抛 `window.__h is undefined`，表现为"探针莫名其妙崩在取值那一步"。
            # 所以：**跑探针期间不要改前端源码**；同时这里让它重载后自动重装。
            await call("Page.addScriptToEvaluateOnNewDocument", {"source":
                "window.__errs=[];"
                "window.addEventListener('error',e=>window.__errs.push('error: '+(e.message||e.type)));"
                "window.addEventListener('unhandledrejection',e=>window.__errs.push('rejection: '+String(e.reason)));"
                + HELPERS})

            async def ev(expr):
                res = await call("Runtime.evaluate",
                                 {"expression": expr, "awaitPromise": True, "returnByValue": True,
                                  "timeout": 1500000})
                if "error" in res:
                    return {"__cdp_error__": res["error"]}
                r = res.get("result", {})
                if "exceptionDetails" in r:
                    return {"__error__": r["exceptionDetails"].get("exception", {}).get("description")}
                return r.get("result", {}).get("value")

            async def shot(name: str):
                png = await call("Page.captureScreenshot", {"format": "png"})
                if "result" in png and "data" in png["result"]:
                    (WORK / f"{name}.png").write_bytes(base64.b64decode(png["result"]["data"]))

            await call("Browser.setDownloadBehavior",
                       {"behavior": "allow", "downloadPath": str(DOWNLOADS)})
            log(f"helpers: {await ev(HELPERS)}")
            try:
                return await fn(ev, shot)
            finally:
                # ⚠️ **优雅关闭**，不能用 taskkill 了事：Chrome 的 localStorage 是
                # LevelDB、**异步落盘**，硬杀会丢掉最近的写入 —— 实测把刚生成的产物整个丢了，
                # 下一次启动读回旧快照，现象与应用"没保存"一模一样（白查半天）。
                try:
                    await call("Browser.close")
                    await asyncio.sleep(2.0)
                except Exception as exc:  # noqa: BLE001 - 关闭失败不影响结论，下面还有兜底
                    log(f"Browser.close 失败（改用 taskkill 兜底）：{type(exc).__name__}")
    finally:
        errlog.close()
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(1)


async def boot(ev) -> None:
    """等 App 恢复完（会话在 localStorage 里，重开就是续跑）。"""
    for _ in range(200):
        if await ev("window.__h.viewKey() !== 'unknown'") is True:
            break
        await asyncio.sleep(0.25)
    await asyncio.sleep(1.0)
    view = await ev("window.__h.viewKey()")
    s = await ev("window.__h.session()")
    if isinstance(s, dict):
        docs = list((s.get("documents") or {}).keys())
        msgs = len(s.get("messages") or [])
        log(f"当前屏：{view}｜本地会话：{msgs} 条消息，产物 {docs}")
    else:
        log(f"当前屏：{view}｜本地会话：无")


async def wait_stage(ev, timeout: float = 25.0) -> str | None:
    """等这一轮的结构化输出**落盘**，再读 `stage_status`。

    为什么要等：AI 消息上屏是即时的，但**写进 localStorage 是防抖的**（800ms，最长 2s），
    而 `stageStatus()` 读的正是存储里的原始 JSON。刚落屏就读会读到**上一轮**（或 `None`），
    于是把"还没写进去"误判成"模型没按契约输出"。这个坑本轮踩了两次。
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = await ev("window.__h.stageStatus()")
        if status is not None:
            return status
        await asyncio.sleep(0.3)
    return None


async def phase_chat(ev, shot) -> int:
    """对话阶段：填表（若还没填）→ 追问到模型报 done。"""
    await boot(ev)
    if (await ev("window.__h.viewKey()")) == "form":
        log("\n【填表提交（真实首轮调用）】")
        t0 = time.monotonic()
        await ev(r"""
          (() => {
            const h = window.__h; const d = {
              product_name: '群聊周报助手',
              problem: '团队群聊里信息散落，每周要人工翻聊天记录拼周报，一次两小时，还容易漏掉重要进展。',
              target_users: '20 人以下创业团队的技术负责人，兼做项目管理',
              core_features: '1. 自动汇总指定群聊的消息\n2. 按人 / 项目生成周报草稿\n3. 支持人工修改后导出',
            };
            for (const [id, v] of Object.entries(d)) h.setNative(h.$('#field-' + id), v);
            const sel = h.$('#field-platform');
            h.setNative(sel, Array.from(sel.options).find((o) => o.value).value);
            for (const id of ['need_auth', 'need_database']) {
              h.$('#field-' + id).querySelector('input[type=radio]').click();
            }
            h.$('form button[type=submit]').click();
            return 'ok';
          })()
        """)
        arrived = await ev("window.__h.waitFor(() => window.__h.aiRows() >= 1, 600000)")
        ok("首轮 AI 回复到达", arrived is True, f"AI 行数={await ev('window.__h.aiRows()')}")
        note(f"首轮耗时 {time.monotonic() - t0:.1f}s")

    first = await wait_stage(ev)
    raw1 = await ev("window.__h.lastAiRaw()")
    note(f"当前 stage_status={first}｜原始输出 {len(raw1 or '')} 字符")
    ok("AI 输出是结构化 JSON 且带 stage_status",
       isinstance(raw1, str) and raw1.startswith("{") and first in ("asking", "done"),
       f"stage_status={first}")

    turns = 0
    enabled = await ev("!!window.__h.startBtn() && !window.__h.startBtn().disabled")
    while not enabled and turns < 6:
        turns += 1
        answer = (
            f"按你的建议来就行，这是第 {turns} 轮补充：目标用户就是技术负责人，"
            "最重要的成功指标是「每周省下两小时」，规模 20 人以下团队，硬约束是必须能导出到飞书。"
        )
        t1 = time.monotonic()
        # 数 **AI 行**而不是总行数：用户消息是乐观上屏的，总行数会立刻 +1，
        # 等它等不到"这一轮回复真的到了"（坑见 HELPERS 里 `aiRows` 的注释）。
        before_ai = await ev("window.__h.aiRows()")
        sent = await ev(
            "(() => {"
            " const h = window.__h;"
            " const ta = h.$('textarea[aria-label=对话输入]');"
            " if (!ta) return 'no-input';"
            " ta.focus();"
            f" h.setNative(ta, {json.dumps(answer, ensure_ascii=False)});"
            " ta.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true }));"
            " return 'ok';"
            "})()"
        )
        if sent != "ok":
            note(f"第 {turns} 轮发送失败：{sent}")
            break
        await ev(f"window.__h.waitFor(() => window.__h.aiRows() > {before_ai}, 600000)")
        st = await wait_stage(ev)
        enabled = await ev("!!window.__h.startBtn() && !window.__h.startBtn().disabled")
        note(f"第 {turns} 轮：耗时 {time.monotonic() - t1:.1f}s｜stage_status={st}｜生成 PRD 可用={enabled}")
        save_results()
        if st == "done":
            break
    ok("对话收敛到 stage_status=done（生成 PRD 可用）", enabled is True,
       f"追问 {turns} 轮后仍不可用（stage_status={await ev('window.__h.stageStatus()')}）")
    note(f"共追问 {turns} 轮")
    return 0


async def phase_doc(
    ev, shot, kind: str, label: str, *, expect_truncated: bool | None = None
) -> int:
    """生成一份产物并检查。"""
    await boot(ev)
    # ⚠️ **先确认自己在哪一页**：`action('generate')` 是"当前页那个生成按钮"，
    # 停错页就会去生成另一份产物，而断言照样"通过"（本轮踩过：在接口文档页跑了
    # "提示词套件"阶段，然后读 prompts 正文得到 0 字符）。
    on_page = await ev("window.__h.docKind()")
    ok(f"[{label}] 点生成前确实在该产物的审核页（当前：{on_page}）", on_page == kind, f"期望 {kind}")
    if on_page != kind:
        return 1
    t0 = time.monotonic()
    clicked = await ev(f"(() => {{ const b = window.__h.action('generate'); if (!b) return 'no-button'; b.click(); return 'ok'; }})()")
    ok(f"[{label}] 找到并点击了生成按钮", clicked == "ok", str(clicked))
    if clicked != "ok":
        return 1
    ok(f"[{label}] 切到生成中",
       await ev("window.__h.waitFor(() => window.__h.status() === 'generating', 10000)") is True,
       str(await ev("window.__h.status()")))
    done = await ev("window.__h.waitFor(() => window.__h.status() === 'pending_review', 1500000)")
    elapsed = time.monotonic() - t0

    # ⚠️ 正文是**防抖**写进 localStorage 的（800ms，最长 2000ms），而状态是内存派生的 ——
    # 状态一变就读存储必然读到旧值。早先这里读到 0 字符，被我误判成"生成结果为空的 bug"，
    # 其实是我自己读得太早。改成**轮询等它落盘**。
    text: object = ""
    for _ in range(60):
        text = await ev(f"window.__h.docText('{kind}')")
        if isinstance(text, str) and text:
            break
        await asyncio.sleep(0.25)
    # `ev()` 在 JS 抛异常时返回的是 `{"__error__": ...}`，不是字符串 —— 直接切片会炸成
    # 一个跟真实问题无关的 `KeyError: slice(None, None, None)`，把现场毁掉。先判类型。
    if not isinstance(text, str):
        note(f"[{label}] ⚠️ 取正文失败（evaluate 未返回字符串）：{text!r}")
        text = ""
    alert = await ev("(document.querySelector('[data-testid=document-review] [role=alert]')||{}).textContent||''")
    if not isinstance(alert, str):
        alert = ""
    banner = await ev(
        "(document.querySelector('[data-testid=document-truncated]')||{}).textContent||''"
    )
    if not isinstance(banner, str):
        banner = ""
    note(f"[{label}] 耗时 {elapsed:.1f}s｜{len(text)} 字符｜末尾 70 字：{text[-70:]!r}")
    if alert:
        note(f"[{label}] ⚠️ 页面失败提示：{alert[:220]}")
    if banner:
        note(f"[{label}] 截断横幅：{banner[:120]}")
    ok(f"[{label}] 生成结束（进入待审核）", done is True, f"状态={await ev('window.__h.status()')}")
    ok(f"[{label}] 产出了正文", bool(text) and len(text) > 300, f"{len(text)} 字符")
    ok(f"[{label}] 没有中途报错", not alert, alert[:160])
    if expect_truncated is not None:
        ok(
            f"[{label}] 截断提示{'出现' if expect_truncated else '不出现'}（预期：{'会被截断' if expect_truncated else '能生成完'}）",
            bool(banner) is expect_truncated,
            f"横幅={banner[:100]!r}",
        )
    if text:
        # ⚠️ **会原地覆盖 `validation_out/flow_{kind}.txt`。** 现状是：
        # `flow_api.txt` / `flow_prompts.txt` 是**分片之前**留下的"整份被截断"证据，
        # 而现在的代码走分片、重跑产出的会是完整文档 —— 一跑就把那两份证据换掉了。
        # 想留住它们，先把文件改名（见 `HANDOFF.md` §6 第 11 项），别指望重跑能复原。
        (BACKEND_OUT / f"flow_{kind}.txt").write_text(text, encoding="utf-8")
    STATE.setdefault("timings", {})[kind] = round(elapsed, 1)
    save_results()
    await shot(f"flow_{kind}")
    return 0


async def phase_prd(ev, shot) -> int:
    # PRD 实测能一次生成完（技能包 6 章，几千字符），所以**不该**出现截断横幅 ——
    # 这条断言就是防"截断判定误报"的：一报假，用户就再也不信这个提示了。
    return await phase_doc(ev, shot, "prd", "PRD", expect_truncated=False)


async def phase_api(ev, shot) -> int:
    await boot(ev)
    # 先通过 PRD（若还在审核页且未通过）
    if (await ev("(() => { const b = window.__h.action('approve'); return !!b && !b.disabled; })()")) is True:
        await ev("window.__h.action('approve').click(); 'ok'")
        await asyncio.sleep(600)
    ok("推进到接口文档页", (await ev("window.__h.docKind()")) == "api",
       "当前=" + str(await ev("window.__h.docTitle()")))
    # ⚠️ **分片落地后，接口文档不再被截断**：前端会按 `GET /conversation/document-plan`
    # 的 3 片逐片生成再拼接（`HANDOFF.md` §4 坑 #20），所以这里**要求截断横幅不出现**。
    # 早先这里写的是 `expect_truncated=True`（"整份必然撞上限"），那是分片之前的结论 ——
    # 留着它会让重跑**必然失败**，而失败原因看起来像功能坏了。
    # 被截断的那份留档是分片之前的 `flow_api.txt`（见 `validation_out/README.md`）。
    rc = await phase_doc(ev, shot, "api", "接口文档", expect_truncated=False)
    fr = await ev("Array.from(new Set(window.__h.docText('api').match(/FR-\\d{2}/g) || [])).sort()")
    note(f"[接口文档] FR 命中 {len(fr or [])} 个：{fr}")
    ok("[接口文档] 能追溯 PRD 的 FR 编号（链条约束）", len(fr or []) >= 1, str(fr))
    return rc


async def phase_prompts(ev, shot) -> int:
    await boot(ev)
    if (await ev("(() => { const b = window.__h.action('approve'); return !!b && !b.disabled; })()")) is True:
        await ev("window.__h.action('approve').click(); 'ok'")
        await asyncio.sleep(600)
    ok("推进到提示词套件页", (await ev("window.__h.docKind()")) == "prompts",
       "当前=" + str(await ev("window.__h.docTitle()")))
    # 套件是多文件、篇幅最长的一份，预期同样会撞上限（不预设，只看实际：
    # 传 None 表示"不判断"——这一份的截断与否本轮才第一次观测到）
    rc = await phase_doc(ev, shot, "prompts", "提示词套件")
    files = await ev("(window.__h.docText('prompts').match(/^=== FILE: .+ ===$/gm) || [])")
    note(f"[提示词套件] 文件块 {len(files or [])}：{files}")
    ok("[提示词套件] 保留 === FILE: === 多文件契约", len(files or []) >= 1, str(files))
    return rc


async def phase_finish(ev, shot) -> int:
    await boot(ev)
    # 通过提示词套件 → 完成页
    if (await ev("(() => { const b = window.__h.action('approve'); return !!b && !b.disabled; })()")) is True:
        await ev("window.__h.action('approve').click(); 'ok'")
    ok("进入完成页",
       await ev("window.__h.waitFor(() => !!window.__h.$('[data-testid=done-panel]'), 10000)") is True,
       "页面=" + (await ev("window.__h.body()"))[:90])
    ok("完成页三张卡片齐全", (await ev("window.__h.$$('[data-doc-card]').length")) == 3,
       str(await ev("window.__h.$$('[data-doc-card]').length")))
    await shot("flow_done")

    for kind, filename in (
        ("prd", "群聊周报助手-PRD.md"),
        ("api", "群聊周报助手-接口文档.md"),
        ("prompts", "群聊周报助手-提示词套件.md"),
    ):
        await ev(f"document.querySelector('[data-download={kind}]').click(); 'ok'")
        target = DOWNLOADS / filename
        for _ in range(120):
            if target.exists() and target.stat().st_size > 0:
                break
            await asyncio.sleep(0.25)
        ok(f"{kind} 文件落盘（{filename}）", target.exists(),
           f"目录：{sorted(p.name for p in DOWNLOADS.iterdir())}")
        if target.exists():
            disk = target.read_text(encoding="utf-8")
            onpage = await ev(f"window.__h.docText('{kind}')")
            ok(f"{kind} 下载内容与页面正文逐字一致", disk == onpage, f"{len(disk)} vs {len(onpage or '')}")
            ok(f"{kind} 无 BOM", not target.read_bytes().startswith(b"\xef\xbb\xbf"), "")

    errs = await ev("window.__errs")
    ok("全流程无未捕获报错", not errs, json.dumps(errs, ensure_ascii=False)[:400])
    return 0


async def phase_status(ev, shot) -> int:
    await boot(ev)
    s = await ev("window.__h.session()")
    log(json.dumps(
        {
            "view": await ev("window.__h.viewKey()"),
            "stage_status": await wait_stage(ev, timeout=8.0),
            "startPrdEnabled": await ev("!!window.__h.startBtn() && !window.__h.startBtn().disabled"),
            "messages": len((s or {}).get("messages", [])),
            "documents": {
                k: {"chars": len(v.get("content", "")), "approved": v.get("approved")}
                for k, v in ((s or {}).get("documents") or {}).items()
            },
            "notice": await ev("(document.querySelector('[data-testid=app-notice]')||{}).textContent||''"),
        },
        ensure_ascii=False, indent=2,
    ))
    # 把已有产物落盘，便于直接读
    for kind in ("prd", "api", "prompts"):
        text = await ev(f"window.__h.docText('{kind}')")
        if text:
            (BACKEND_OUT / f"flow_{kind}.txt").write_text(text, encoding="utf-8")
            log(f"  已落盘 flow_{kind}.txt（{len(text)} 字符）")
    return 0


async def phase_diag(ev, shot) -> int:
    """当前屏的详细现场：内存（DOM）、存储、未捕获错误，三处一起看。

    为什么要这个：`status()` 是**派生**的（内存），而 `docText()` 读的是**存储**，
    两者不一致时单看哪个都会得出相反结论（本轮就吃过一次：状态说"待审核"、
    存储说 0 字符，于是分不清是"后端没给内容"还是"没落盘"）。
    """
    await boot(ev)
    diag = await ev(r"""
      (() => {
        const h = window.__h;
        const raw = localStorage.getItem('harnessprd:development:1:session');
        let payload = null;
        try { payload = raw ? JSON.parse(raw).payload : null; } catch (e) { payload = 'PARSE_FAIL'; }
        const docs = (payload && payload.documents) || {};
        const editor = document.querySelector('[data-testid=document-editor]');
        const viewer = document.querySelector('[data-testid=document-viewer]');
        return {
          viewKey: h.viewKey(),
          statusAttr: h.status(),
          statusText: (document.querySelector('[data-testid=document-status]')||{}).textContent || '',
          editorChars: editor ? editor.value.length : null,
          viewerChars: viewer ? (viewer.textContent || '').length : null,
          inMemoryApiChars: h.docText('api').length,
          storedKeys: Object.keys(docs),
          storedChars: Object.fromEntries(Object.entries(docs).map(([k, v]) => [k, (v.content||'').length])),
          storedTruncated: Object.fromEntries(Object.entries(docs).map(([k, v]) => [k, v.truncated])),
          viewStateOnDisk: payload && payload.viewState,
          notice: (document.querySelector('[data-testid=app-notice]')||{}).textContent || '',
          // 对话侧的失败/截断横幅（`role=alert`，在对话页）。截断在结构化 JSON 上
          // 表现为"解析不出下一轮要问什么"，所以必须能在现场看到这句话有没有出现。
          chatAlert: (document.querySelector('[role=alert]')||{}).textContent || '',
          errs: window.__errs,
          reviewing: (document.querySelector('[data-testid=document-review]')||{}).dataset?.status,
        };
      })()
    """)
    log(json.dumps(diag, ensure_ascii=False, indent=2))
    await shot("diag")
    return 0


async def phase_trace(ev, shot) -> int:
    """点一次生成，之后每 1.5 秒同时采 DOM 与存储 —— 看内容在哪一步丢掉。

    为什么要时间线：状态（内存派生）与正文（防抖落盘）是两条独立的路径，
    只在最后看一眼分不清"没生成出来"和"生成出来了但没落盘"。
    """
    await boot(ev)
    clicked = await ev("(() => { const b = window.__h.action('generate'); if (!b) return 'no-button'; b.click(); return 'ok'; })()")
    log(f"点击生成：{clicked}")
    for i in range(30):
        await asyncio.sleep(1.5)
        sample = await ev(r"""
          (() => {
            const h = window.__h;
            const payload = (() => {
              try { return JSON.parse(localStorage.getItem('harnessprd:development:1:session')).payload; }
              catch (e) { return null; }
            })();
            const docs = (payload && payload.documents) || {};
            const editor = document.querySelector('[data-testid=document-editor]');
            const viewer = document.querySelector('[data-testid=document-viewer]');
            return {
              s: h.status(),
              ed: editor ? editor.value.length : null,
              vw: viewer ? (viewer.textContent || '').length : null,
              api: docs.api ? (docs.api.content || '').length : 0,
              trunc: docs.api ? docs.api.truncated : null,
              vs: payload && payload.viewState,
              notice: ((document.querySelector('[data-testid=app-notice]')||{}).textContent||'').slice(0, 40),
            };
          })()
        """)
        log(f"  t={1.5*(i+1):5.1f}s  {sample}")
        if isinstance(sample, dict) and sample.get("trunc") is not None:
            break
    await shot("trace")
    return 0


PHASES = {
    "chat": phase_chat,
    "prd": phase_prd,
    "api": phase_api,
    "prompts": phase_prompts,
    "finish": phase_finish,
    "status": phase_status,
    "diag": phase_diag,
    "trace": phase_trace,
}

# 一次跑完整条链条时的顺序。**顺序不能变**：每一步都依赖上一步的产物
# （接口文档要 PRD、套件要 PRD + 接口文档、完成页要三份都通过）。
FULL_CHAIN: tuple[str, ...] = ("chat", "prd", "api", "prompts", "finish")


async def run_phase(phase: str) -> int:
    """跑一个阶段（自带浏览器进程）。"""
    log(f"\n=== 阶段 {phase} ===")
    return await with_page(PHASES[phase])


async def main() -> int:
    phase = sys.argv[1] if len(sys.argv) > 1 else "status"
    if phase != "all" and phase not in PHASES:
        log(f"未知阶段：{phase}；可选 all 或 {sorted(PHASES)}")
        return 2

    if RESULTS_FILE.exists():
        try:
            prev = json.loads(RESULTS_FILE.read_text(encoding="utf-8")).get("results", [])
            # 就地改而不是重新绑定：`RESULTS` 是模块级列表，重绑定对其它函数无效
            RESULTS[:] = [r for r in prev if not any(r["name"] == n["name"] for n in RESULTS)]
        except Exception:
            pass

    DOWNLOADS.mkdir(parents=True, exist_ok=True)
    WORK.mkdir(parents=True, exist_ok=True)

    rc = 0
    for step in FULL_CHAIN if phase == "all" else (phase,):
        rc = await run_phase(step) or rc

    # 同名断言只保留**最后一次**：早期跑失败、后来修好的条目不该继续挂在报告里
    # （否则一眼看去像是"还有一堆失败"，而其实全是历史回声）。
    latest: dict[str, dict] = {}
    for item in RESULTS:
        latest[item["name"]] = item
    RESULTS[:] = list(latest.values())
    passed = sum(1 for r in RESULTS if r["pass"])
    log(f"\n累计 {passed}/{len(RESULTS)} passed")
    for r in RESULTS:
        if not r["pass"]:
            log(f"  FAIL - {r['name']} | {r['detail'][:180]}")
    save_results()
    return rc


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
