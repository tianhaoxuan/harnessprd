import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

/** 后端地址。改后端启动端口时同步改这里。 */
const BACKEND_ORIGIN = 'http://127.0.0.1:8000'

export default defineConfig({
  plugins: [react()],
  server: {
    // 端口固定 5173 —— 与后端 CORS 白名单（backend/.env 的 CORS_ORIGINS）保持一致。
    port: 5173,
    // 5173 被占用时**直接失败**，而不是静默改用 5174。
    // 否则端口一变，后端 CORS 白名单就对不上了：请求被拒，但报错出现在浏览器控制台，
    // 很容易被当成"后端挂了"去查错方向。
    strictPort: true,

    // 开发期代理：前端只写相对路径（axios.get('/api/v1/...')），
    // 浏览器看到的是**同源请求**，既不用硬编码后端地址，也不涉及跨域。
    // 生产部署时由反向代理（Nginx / Caddy）承担同一角色。
    proxy: {
      '/api': {
        target: BACKEND_ORIGIN,
        // 把 Host 头改写成目标地址。不设的话后端收到的是 `Host: localhost:5173`，
        // 按 Host 做校验或路由的中间件会拒绝。
        changeOrigin: true,
      },
    },

    // ⚠️ **忽略工具链的临时文件**。有些写入方式（编辑器、脚本、AI 工具）不是原地改文件，
    // 而是"写一个临时文件 → 改名覆盖"，临时目录形如 `.App.tsx.1234.abc.tmpdir/`。
    // Vite 会去 watch 这些路径，而它们**转瞬即逝且可能仍被占用** —— Windows 上会抛
    // `EBUSY: resource busy or locked, watch ...`，**整个 dev server 直接退出**
    // （实测：一次正常的文件写入就把 5173 打死了，看起来像"改代码把服务改崩了"）。
    watch: {
      ignored: ['**/.*.tmpdir/**', '**/.*.tmp', '**/*.tmp'],
    },
  },
})
