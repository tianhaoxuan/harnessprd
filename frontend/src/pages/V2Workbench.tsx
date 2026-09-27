/**
 * V2 工作台外壳。
 *
 * ## 它自己不实现工作台
 *
 * 工作台本体（表单 → 对话 → 产物 → 审核）就是 V1 那套 `App` 组件。V2 这一层只做三件事：
 *
 * 1. **读路由参数**：`/v2` 没有 `:id`（新建），`/v2/:id` 有（编辑）—— 两种情况渲染
 *    **同一个** `App`，靠 `sessionId` 区分。这就是需求里"复用同一个组件"的落点：
 *    再写一个工作台 = 两套状态机并行维护，迟早长歪。
 * 2. **加一条 V2 自己的顶栏**：左上「工作台」、右上「新建」「结束当前任务」「我的方案」。
 *    放在这一层而不是塞进 `App` 内部，是为了**让 V1 的 `/` 完全不被动到** ——
 *    V1 页面里不该凭空多出 V2 的入口。
 * 3. **离开页面前把防抖队列落库**（`flushPendingSave()`）：不 flush 的话，
 *    用户最后 1 秒的编辑会随页面一起消失，而他以为已经保存过了。
 *
 * ## `key` 为什么不是简单的 `id ?? 'new'`
 *
 * 需求里有一条硬规则：**从 `/v2` 保存成功、路由替换到 `/v2/:id` 之后，不许再拉一次接口**。
 * 而 `key` 一变 React 就会**重建** `App`（重建 = 重新挂载 = 会去拉数据）。
 * 所以这里记住"哪些 id 是**本页自己刚创建的**"：自建的 id 不改 key，`App` 保持原实例、
 * 沿用内存里的状态；只有**直接访问/刷新** `/v2/:id` 时才是新实例 —— 那时才拉数据。
 * 这条规则的开关在 `App` 的 `onSessionSaved` 回调上（见下面的注释）。
 */
import { useRef } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'

import App from '../App'
import { flushPendingSave } from '../services/sessionService'

export default function V2Workbench() {
  const { id } = useParams<{ id: string }>()
  const navigate = useNavigate()
  /** 本页自己刚创建出来的 id：这些 id 不该触发 `App` 重建（否则会重拉数据）。 */
  const selfCreatedId = useRef<string | null>(null)

  const fromSelf = Boolean(id) && id === selfCreatedId.current
  const workbenchKey = id && !fromSelf ? id : 'new'
  const workbenchSessionId = id && !fromSelf ? id : null

  /** 离开页面前：先把防抖队列落库，再跳。跳转失败/没网也不该把用户卡住。 */
  const goWithFlush = async (target: string) => {
    await flushPendingSave()
    navigate(target)
  }

  return (
    <div className="flex min-h-full flex-col">
      {/* V2 顶栏。视觉与工作台一致：白底、细边框、圆角按钮 */}
      <header className="sticky top-0 z-10 border-b border-slate-200 bg-white/90 backdrop-blur">
        <div className="mx-auto flex max-w-3xl items-center gap-3 px-8 py-3">
          <Link to="/v2" className="text-sm font-semibold text-slate-800">
            harnessprd <span className="text-primary-600">V2</span>
          </Link>
          <span className="rounded bg-slate-100 px-1.5 py-0.5 text-xs text-slate-500">
            {id ? '编辑方案' : '新建方案'}
          </span>
          <span className="flex-1" />
          <Link
            to="/v2"
            data-testid="v2-new"
            className="rounded-lg border border-slate-300 px-3 py-1.5 text-xs text-slate-600 transition hover:bg-slate-50"
          >
            新建
          </Link>
          <button
            type="button"
            data-testid="v2-end-task"
            onClick={() => void goWithFlush('/v2')}
            className="rounded-lg border border-slate-300 px-3 py-1.5 text-xs text-slate-600 transition hover:bg-slate-50"
          >
            结束当前任务
          </button>
          <button
            type="button"
            data-testid="v2-my-plans"
            onClick={() => void goWithFlush('/v2/list')}
            className="rounded-lg bg-primary-600 px-3 py-1.5 text-xs font-medium text-white transition hover:bg-primary-700"
          >
            我的方案
          </button>
        </div>
      </header>

      {/* 工作台本体。`key` 的取值规则见文件头：「自建 id 不重建」。 */}
      <div className="flex-1">
        <App
          key={workbenchKey}
          sessionId={workbenchSessionId}
          onSessionSaved={(savedId) => {
            // 首次保存成功 → 记下这是「本页自建」的 id，再把地址**替换**成 `/v2/{id}`。
            // 地址变了但 `key` 不变（`fromSelf` 为真），所以 `App` 不会被重建，
            // 也就不会去重拉一次刚存好的数据 —— 这是需求测试 2/5 的关键。
            selfCreatedId.current = savedId
            navigate(`/v2/${savedId}`, { replace: true })
          }}
        />
      </div>
    </div>
  )
}
