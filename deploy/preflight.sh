#!/usr/bin/env sh
# harnessprd 上线前自查 —— **在服务器上跑**（不是本机），逐项检查部署前置条件。
#
#   sh deploy/preflight.sh
#
# 为什么要这个脚本：部署文档里最容易出的三类问题（忘了填 Key、端口被占、容器出不了网）
# 都不会在 `docker compose up` 时报错，而是等到用户点"生成"时才以"生成失败"的形式暴露 ——
# 那时你已经以为部署成功了。这个脚本把这三件事提前问一遍。
#
# 退出码：0 = 没发现硬问题（WARN 项自己判断）；1 = 有 FAIL，先处理再 up。

set -u

fail=0
ok() { printf '  [OK]   %s\n' "$*"; }
bad() { printf '  [FAIL] %s\n' "$*"; fail=1; }
warn() { printf '  [WARN] %s\n' "$*"; }

# 允许在仓库根目录之外调用
if [ -f "$(dirname "$0")/../docker-compose.yml" ]; then
    cd "$(dirname "$0")/.." || exit 1
fi
if [ ! -f docker-compose.yml ]; then
    printf '请在仓库根目录运行（或让脚本与 docker-compose.yml 的相对位置保持不变）\n'
    exit 1
fi

printf '\n== 1. 容器运行时 ==\n'
if command -v docker >/dev/null 2>&1; then
    ok "docker 存在：$(docker --version)"
else
    bad "没有 docker"
fi
if docker info >/dev/null 2>&1; then
    ok "docker 守护进程可达"
else
    bad "docker 守护进程不可达（引擎没起；或当前用户不在 docker 组）"
fi
if docker compose version >/dev/null 2>&1; then
    ok "docker compose v2 可用"
else
    bad "没有 docker compose v2（只有 v1 的话命令形态不同）"
fi

printf '\n== 2. 配置与密钥 ==\n'
if [ -f backend/.env ]; then
    ok "backend/.env 存在"
    if grep -qE '^DEEPSEEK_API_KEY=.+' backend/.env; then
        ok "DEEPSEEK_API_KEY 已填"
    else
        bad "DEEPSEEK_API_KEY 是空的 —— 生成接口会返回 503"
    fi
    if grep -qE '^DEFAULT_LLM_PROVIDER=deepseek' backend/.env; then
        ok "DEFAULT_LLM_PROVIDER=deepseek"
    else
        warn "DEFAULT_LLM_PROVIDER 不是 deepseek —— 确认你确实配了那家厂商的 Key"
    fi
    # 下限是 8192：整份接口文档要 >10000 输出 token，分片预算也是按这个上限算的。
    # 调小会让每一片也开始被截断（docs/部署.md §5.1）。
    if grep -qE '^LLM_MAX_TOKENS=8192' backend/.env; then
        ok "LLM_MAX_TOKENS=8192"
    else
        warn "LLM_MAX_TOKENS 不是 8192 —— 调小会让分片也被截断，调大没有意义（模型上限就是 8192）"
    fi
    if grep -qE '^LLM_TIMEOUT=([3-9][0-9]{2,}|[0-9]{4,})' backend/.env; then
        ok "LLM_TIMEOUT 足够长"
    else
        warn "LLM_TIMEOUT 偏小 —— 单次生成实测 3–30 秒，网络慢时会更久"
    fi
else
    bad "缺 backend/.env（cp backend/.env.example backend/.env 后填 Key）"
fi
if [ -f deploy/nginx.conf ]; then
    ok "deploy/nginx.conf 存在"
else
    bad "缺 deploy/nginx.conf —— web 容器会挂不上配置"
fi

printf '\n== 3. 端口占用 ==\n'
check_port() {
    port="$1"
    if command -v ss >/dev/null 2>&1; then
        if ss -ltn 2>/dev/null | grep -q ":${port} "; then
            warn "端口 ${port} 已被占用"
        else
            ok "端口 ${port} 空闲"
        fi
    elif command -v netstat >/dev/null 2>&1; then
        if netstat -ltn 2>/dev/null | grep -q ":${port} "; then
            warn "端口 ${port} 已被占用"
        else
            ok "端口 ${port} 空闲"
        fi
    else
        warn "没有 ss/netstat，跳过端口 ${port} 检查"
    fi
}
check_port "${WEB_PORT:-8080}"
check_port 8000

printf '\n== 4. 出网（容器能不能到 LLM 供应商）==\n'
# 后端容器要能直连 api.deepseek.com。连不上的话：生成全失败，而日志里只有一句连接错误。
if command -v curl >/dev/null 2>&1; then
    code=$(curl -sS -m 8 -o /dev/null -w '%{http_code}' https://api.deepseek.com/v1/models 2>/dev/null || echo 000)
    case "$code" in
        200 | 401) ok "能连到 api.deepseek.com（HTTP ${code}；401 也算通：网络可达、只是没带 Key）" ;;
        *) bad "连不上 api.deepseek.com（HTTP ${code}）—— 容器同样连不上，生成会全部失败" ;;
    esac
else
    warn "没有 curl，跳过出网检查（可手工试：wget -qO- https://api.deepseek.com/v1/models）"
fi

printf '\n== 5. 访问控制（**上公网前必须做**）==\n'
if grep -qE '^[[:space:]]*auth_basic' deploy/nginx.conf; then
    ok "nginx.conf 里 Basic Auth 已启用"
elif grep -qE '^[[:space:]]*allow ' deploy/nginx.conf; then
    ok "nginx.conf 里配了 IP 白名单"
else
    warn "没有任何访问控制 —— 本服务无用户体系，谁拿到地址谁就能烧你的 LLM 额度（docs/部署.md §5.3）"
fi

printf '\n'
if [ "$fail" -eq 0 ]; then
    printf '结论：前置条件基本就绪（WARN 项自己判断要不要处理），可以 docker compose up -d --build api web\n'
else
    printf '结论：有 FAIL 项，先处理再起服务\n'
fi
exit "$fail"
