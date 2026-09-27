import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter, Route, Routes } from 'react-router-dom'

import App from './App'
import SessionListView from './pages/SessionListView'
import V2Workbench from './pages/V2Workbench'
import './index.css'

const container = document.getElementById('root')
if (!container) {
  throw new Error('找不到 #root 挂载点 —— index.html 被改坏了？')
}

/**
 * 路由入口（react-router-dom v7）。
 *
 * | 路径 | 页面 | 说明 |
 * | --- | --- | --- |
 * | `/` | `App`（V1） | **一行没动**：还是原来那套"表单 → 对话 → 产物"向导 |
 * | `/v2` | 工作台（新建） | 没有 `:id` → 全新一份 |
 * | `/v2/list` | 方案列表 | |
 * | `/v2/:id` | 工作台（编辑） | **与新建复用同一个组件**，靠 `useParams()` 区分 |
 *
 * ⚠️ `/v2/list` 与 `/v2/:id` 的**声明顺序无关紧要**：react-router v6+ 用打分匹配，
 * 静态段（`list`）永远优先于动态段（`:id`）。靠"把静态路由写在前面"是不必要的迷信，
 * 但反过来写也不会出错 —— 别为了这个去调整顺序，改坏了反而更难查。
 *
 * ⚠️ 深链接（直接访问 `/v2/list`）依赖 dev server 的 SPA 回退：Vite 对未命中的路径
 * 返回 `index.html`（默认 `appType: 'spa'`）。部署到静态服务器时要配同样的回退，
 * 否则刷新 `/v2/list` 会 404 —— 这不是路由库的问题。
 */
createRoot(container).render(
  <StrictMode>
    <BrowserRouter>
      <Routes>
        <Route path="/" element={<App />} />
        <Route path="/v2" element={<V2Workbench />} />
        <Route path="/v2/list" element={<SessionListView />} />
        <Route path="/v2/:id" element={<V2Workbench />} />
      </Routes>
    </BrowserRouter>
  </StrictMode>,
)
