# 工单：产出物进度条 + PRD 审阅页 CTA 分层

> **用途**：把「顶栏进度改为按 entryMode 动态展示的产出物节点」+「PRD 审阅页前进按钮分层」这一篇，
> 交给**一个干净的会话**去执行。本文件是自包含的 —— 不需要回读之前那份提示词，也不需要回读对话。
>
> **为什么单独成单**：写这份单的会话已经跑了四篇改造，上下文里同时装着入口选择页、行编辑器、
> 文档版本链三摊东西，判断"这一篇能不能一次做完"已经不可靠。工单本身是可靠的，所以把它交出去。

---

## 0. 前置状态（执行前先确认）

工作区**应当是干净的、可编译的**：

```
cd frontend
node scripts/check-plain-text.mjs     # 期望 exit 0
node node_modules/typescript/bin/tsc --noEmit   # 期望 exit 0
```

后端自检（与本篇无关，但能确认基线没坏）：

```
cd backend
.venv\Scripts\python.exe scripts\smoke_check.py   # 期望 271 PASS / 0 FAIL
.venv\Scripts\python.exe scripts\job_check.py     # 期望 79 PASS / 0 FAIL
```

⚠️ **沙箱注意**（本机实测，别在这上面浪费时间）：

- **`pnpm` 不可用**（建不了自己的临时目录，报 `拒绝访问 (os error 5)`）。所以
  `pnpm build` 要拆成三步直接跑 node，见上面的 check:text + tsc，以及
  `node node_modules/vite/bin/vite.js build`。
- **`vite` 起 dev server / 跑 build 会报 `spawn EPERM`** —— 它加载配置时内部 spawn 子进程取
  Windows 真实路径，受限模式下必然失败。跑 vite 的命令需要放宽沙箱权限一次。
- **8000 / 5173 上通常已经有别的进程**（改动之前就起的），**不要杀**。要验收就自己另起端口 +
  一份临时 vite 配置（代理指向自己的后端端口），用完删掉。
- 临时目录统一放 `backend/_tmp_xxx/`、临时脚本放 `backend/scripts/_tmp_*.py`，跑完删除。
  `.gitignore` 里的 `backend/_tmp_*/` 已经能兜住漏删的目录。

---

## 1. 目标（一句话）

顶栏那条**固定五步的「流程进度」**，换成**按 `entryMode` 裁剪、按"产出物是否已有正文"点亮**的
「产出物」进度条；同时把 PRD 审阅页的两个 forward 按钮分出主次。

---

## 2. ⚠️ 三处与提示词原文不符的事实（已核实，按事实做）

| 提示词说 | 实际 |
| --- | --- |
| 删除 `stageItems` useMemo 整块、废弃 `getStageIndex`、关心 `currentStageIndex` | **这三个符号在 `App.tsx` 里都不存在**。顶栏是 `components/StepProgress.tsx`，由 `types/index.ts` 的 `stepsForMode(entryMode)` / `stepIndexOf(entryMode, viewState)` 驱动，调用点只有 [App.tsx](../../frontend/src/App.tsx) 的 `<StepProgress>` 一处 |
| 调 `retrieveRagBeforeApiDocs` | **不存在**。RAG 门控是 `ragConfirmedRef`（`App.tsx`）+ 一个 effect，真正发起生成的是 `handleGenerateDocument(kind)` |
| 调 `generatePrompts(prdContent, '')` | **不存在**。真名 `handleGenerateDocument('prompts')`，底层是 `jobGen.startPrompts(prdContent, apiDocsContent)` |
| `variant: 'link'`（原作者自己也留了后路） | **确实不支持**：`DocumentAction['variant']` 只有 `'primary' \| 'secondary' \| 'danger'`。要落"次要文字链"必须先加一档 |

**好消息**：`StepProgress` 没有任何别处依赖 —— 全仓 `git grep step-progress|StepProgress|stepsForMode|stepIndexOf`
只有「组件自己 + `App.tsx` 的 import 与那一处 JSX + `frontend/README.md` 的两行散文」。
没有 e2e 脚本点它，所以替换是安全的（README 那两行要顺手改）。

---

## 3. 改动地图

### 3.1 新建 `frontend/src/utils/artifactProgress.ts`

纯函数，**不 import 任何 hook / 组件**（入参自带回调），这样它可单测、可离线渲染。

```ts
export type ArtifactId = 'requirements' | 'clarification' | 'prd' | 'api-docs' | 'prompts'
export type ArtifactStatus = 'pending' | 'active' | 'done'

export interface ArtifactNode {
  id: ArtifactId
  label: string
  status: ArtifactStatus
  clickable: boolean
  onClick?: () => void
  title?: string
}

/** 判断"这一份产出物有没有正文"所需的全部输入（其余状态一概不进这个函数）。 */
export interface ArtifactContents {
  /** 表单里有没有有效的产品名（structured 的 requirements 判据之一）。 */
  hasProductName: boolean
  /** 澄清对话的内容（`messages`）。 */
  hasClarification: boolean
  prdContent: string
  apiDocsContent: string
  promptsContent: string
}

export function buildArtifactNodes(args: {
  entryMode: EntryMode
  viewState: ViewState
  contents: ArtifactContents
  /** 每个节点该跳去哪；返回 undefined = 这一屏没有对应去处。 */
  onNavigate: (id: ArtifactId) => (() => void) | undefined
}): ArtifactNode[]
```

**节点集合（按 entryMode 裁剪）**

| entryMode | 节点 |
| --- | --- |
| `structured` | 需求 → 澄清 → PRD → 接口文档 → 提示词（5） |
| `prd-shortcut` | PRD → 接口文档 → 提示词（3） |
| `prompts-debug` | PRD → 提示词（2）；**`apiDocsContent` 非空时**在中间插一个接口文档 |

**active（`viewState` → 节点）**

| viewState | active |
| --- | --- |
| `form` | `requirements`（`structured`）/ 该入口的第一份产出物 |
| `chatting` | `clarification` |
| `generating-prd` / `review-prd` | `prd` |
| `generating-api-docs` / `review-api-docs` | `api-docs` |
| `generating-prompts` / `review-prompts` | `prompts` |

**done 判据**

- `requirements`：`hasProductName` 且（**见 §4 待拍板**）
- `clarification`：`hasClarification`
- `prd` / `api-docs` / `prompts`：对应 `content.trim()` 非空

**clickable**：`status === 'done'` 才可点（`active` 是当前页、`pending` 没东西可看，都不可点）。
**`pending` 必须给 `title`**（例如「尚未生成」），否则用户点不动又不知道为什么。

> ⚠️ **明确禁止**：让 `pending` 的接口文档 / 提示词节点"跳一个空页"。这是提示词 §四 点名的坑。

### 3.2 新建 `frontend/src/components/ArtifactProgressBar.tsx`

照 `StepProgress.tsx` 的 DOM 形状写（`<nav>` + 一组 `<button>` + `Check` 图标），
差异只有：`aria-label` 从「流程进度」改成**「产出物」**，左侧文案同样改成「产出物」，
并按 `node.status` 上三档样式（done 绿底 + ✓ / active 深色底白字 / pending 灰且 `disabled`）。
保留一个 `data-testid`（建议 `artifact-progress`）供将来断言。

### 3.3 改 `frontend/src/App.tsx`

| 位置 | 改动 |
| --- | --- |
| import（~34 行） | `StepProgress` → `ArtifactProgressBar` + `buildArtifactNodes` |
| 顶栏 JSX（~2618 行） | `<StepProgress viewState={viewState} entryMode={entryMode} onSelect={handleStepSelect} />` → `<ArtifactProgressBar nodes={artifactNodes} />`；`artifactNodes` 用 `useMemo` 在 `docActionsFor` 附近算，`onNavigate` 里复用现有的 `handleStepSelect` / `setViewState` |
| `docActionsFor`（~2514 行） | 见 §3.5 |

### 3.5 PRD 审阅页 CTA 分层（改 `docActionsFor`，~2514 行）

`entryMode === 'structured'`（主路径）：

- 主：`{ label: '生成接口文档 →', variant: 'primary' }`（走现有 RAG 门控那条路：设置
  `ragConfirmedRef` 之前的状态 → 弹 `RagHitsPanel` → 确认后生成。**不要绕过它**）
- 次：`{ label: '跳过接口文档，直接生成提示词', variant: 'link', onClick: confirmSkipApiDocs }`
- `confirmSkipApiDocs`：`window.confirm('跳过接口文档时，提示词可能缺少 API 细节。是否继续？')`
  → 确认后 `void handleGenerateDocument('prompts')`（**注意不是** `generatePrompts(prd, '')`，那个函数不存在）

`entryMode === 'prd-shortcut' | 'prompts-debug'`：用户已经选了捷径，**保留两枚并列**，
把「直接生成提示词」降成 `secondary`（可加一句轻量 confirm，也可不加）。

其它审阅页保持：`review-api-docs` 只有「生成提示词 →」一枚 forward（**不要**给它加"跳过提示词"）；
`review-prompts` 无 forward。

### 3.6 改 `frontend/src/components/DocumentReview.tsx`

- `DocumentAction['variant']` 联合类型加 `'link'`
- `ACTION_VARIANTS` 加一档：小字号 + `underline` + `text-slate-500`，**视觉权重明显低于 primary**
  （提示词 §六 也允许用 ghost 小按钮替代，二选一）

### 3.7 顺手改 `frontend/README.md`

第 27 行与第 658 行提到 `StepProgress.tsx`「顶部步骤条，5 格」。顶栏换了之后这两句就不成立了。

---

## 4. ⚠️ 需要拍板的一条判据

`requirements` 节点的 done 条件，提示词写的是
**「formData 有有效 product_name **且曾进入过澄清**或已有 PRD」**。

问题是「**曾经**进入过澄清」这个信息**没有现成状态**：`viewState` 只有"现在在哪一屏"，
一旦用户从 `chatting` 走到 `review-prd`，`viewState` 就变了，而"他确实聊过"这件事没有单独记。

三个可选判据（**选一个再动手**，否则会出现"进过澄清页但一个字没说、节点却打勾"）：

1. `hasProductName && (hasClarification || prdContent.trim() || roundIndex > 1)` —— 无需新状态，最省事
2. `hasProductName && (hasClarification || prdContent.trim())` —— 更严：没聊过且没 PRD 就不算完成
3. 真引入一个"提交过表单/进过澄清"的标记（写进快照）—— 最准，但要动 `SessionData` 与快照读写

**倾向 1**，但这条决定了"填完表单还没开始聊"时需求节点是 ✓ 还是 ●，属于产品口径，得你定。

---

## 5. 验收清单（提示词 §十 + 本篇补充）

- [ ] 顶栏左侧文案是「产出物」，**不再是**「流程进度」
- [ ] 切换 `entryMode`（从路径选择页选三种路径），节点个数随之变化：5 / 3 / 2（有 API 内容时 3）
- [ ] 未生成的节点**点不动**，且 hover 有「尚未生成」之类的说明
- [ ] 已完成的节点可点，且跳到对应 `review-*`
- [ ] 主路径 PRD 审阅页：**只有一个深色主按钮**「生成接口文档 →」；「跳过接口文档…」是次要样式且**弹 confirm**
- [ ] 跳过之后：进入 `generating-prompts`，而顶栏的**接口文档节点仍是 pending**（提示词 §九）
- [ ] 导入 PRD 路径的 PRD 页：两个 forward 按钮**都还能用**
- [ ] 接口文档页：只有「生成提示词 →」一枚 forward
- [ ] 澄清页按钮、三种文档生成 Job、刷新重连、版本侧栏**都没被改坏**
- [ ] 仅有 PRD、无 API 时看提示词页点「返回」→ 回到 `review-prd`（与改造前一致）
- [ ] 刷新后节点 done 状态与内容字段一致（这条是**纯函数**的好处：状态由内容推导，不另存）
- [ ] `check:text` / `tsc --noEmit` / `vite build` 三步全过

---

## 6. 不要动的东西（提示词 §八 点名 + 本篇补充）

- `hooks/useGenerationJob.ts` —— 一行都不要动
- RAG 检索函数本体与 `RagHitsPanel`（**门控逻辑要复用**，不是绕过）
- 澄清页按钮、表单字段
- `components/GenerationStepper.tsx` —— 它吃的是 `steps` / `elapsedSeconds`，与顶栏无关
- **建议先别删 `StepProgress.tsx` 与 `stepsForMode` / `stepIndexOf`**：它们导出着，留着不报错，
  这次改动可一键回退。真要清理，单独一次提交（并把 README 一起收尾）

---

## 7. 本次会话已经做完、但**还没提交**的东西

`git status` 里这些是前两篇的成果，与本工单无关，但会影响"基线是否干净"的判断：

- 入口选择页（`components/project-entry/`）+ `types/index.ts` 的 `FormSubView`
- 行编辑器（`components/structured-form/`）+ `utils/structuredFormTransform.ts` + `types/structuredForm.ts`
- `App.tsx` / `StructuredForm.tsx` / `types/index.ts` 的相应改动

**先决定要不要把这两篇提交掉再开工单**（两件事的 diff 混在一起会让验收变难）。
