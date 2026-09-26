# harnessprd 前端镜像：**多阶段** —— 构建产物留在第一阶段，最终镜像只装静态文件。
#
# 构建（在仓库根目录）：
#   docker build -t harnessprd-web ./frontend
#
# 为什么最终镜像是 nginx 而不是 node：产物是纯静态文件（`vite build` 的 `dist/`），
# 生产不该为了发几个文件而背一个 Node 运行时。
#
# ⚠️ **nginx 的 server 配置不在这个镜像里**，而是由 compose 挂 `deploy/nginx.conf` 进去 ——
# 这样改反代规则（超时、鉴权、HTTPS）不用重建镜像。
# 直接 `docker run` 这个镜像的话，它只有默认站点，`/api` 不会转发到后端。

# ---------- 阶段 1：构建 ----------
FROM node:22-slim AS build

# 用 corepack 按 package.json 的 `packageManager` 字段取 pnpm（已固定 12.4.2）。
# 不固定的话：本地 pnpm 与镜像里的 pnpm 大版本不一致时，`--frozen-lockfile` 会因为
# lockfile 格式差异直接失败 —— 那是"在我机器上是好的"的典型来源。
RUN corepack enable

WORKDIR /app

# 依赖清单先拷 → 依赖没变时这层走缓存
COPY package.json pnpm-lock.yaml ./

# npm 源。默认官方 registry，国内机器通常要换镜像（同 pip 的道理）：
#   docker compose build --build-arg NPM_REGISTRY=https://registry.npmmirror.com web
ARG NPM_REGISTRY=https://registry.npmjs.org/
RUN pnpm config set registry "${NPM_REGISTRY}" && pnpm install --frozen-lockfile

COPY . .

# `pnpm build` = `check:text && tsc --noEmit && vite build`（见 package.json）——
# 也就是说**镜像构建会顺带跑类型检查与纯文本守卫**，构建不过就出不来镜像。
# 这是刻意的：绝不让一个类型错误的东西上线。
RUN pnpm build

# ---------- 阶段 2：运行 ----------
FROM nginx:1.27-alpine

COPY --from=build /app/dist /usr/share/nginx/html

EXPOSE 80
