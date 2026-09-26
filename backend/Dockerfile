# harnessprd 后端镜像。
#
# 构建（在仓库根目录）：
#   docker build -t harnessprd-api ./backend
#
# 运行：**密钥只能从环境变量进容器，不能进镜像**（`.env` 已在 .gitignore 里，
# 但构建上下文仍然会把它打进层里 —— 所以 `.dockerignore` 必须挡住它）。
#
#   docker run --rm -p 8000:8000 --env-file backend/.env harnessprd-api
#
# ⚠️ 这个镜像里**没有前端**。生产形状是「nginx 出静态 + /api 反代到本容器」，
# 见 docs/部署.md。单容器形态（FastAPI 连静态一起出）需要给 main.py 加静态挂载，
# 本期没做。

FROM python:3.12-slim

# 依赖来源**只有 requirements.txt**（后端依赖的唯一来源，见文件头注释）。
# 不装 gcc 之类的编译链：requirements 里全是纯 wheel 或自带 wheel 的包，实测不需要。
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# pip 源。默认官方 PyPI，**国内机器常常需要换成镜像**：同一条命令里往往"几个包下成了、
# 某个包却报 `Could not find a version ... (from versions: none)`" —— 那通常不是包不存在
# （官方索引里明明有），而是那个项目页的请求被拦或超时。换源即可：
#   docker compose build --build-arg PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/ api
# 也可在 compose 的 .env 里写 PIP_INDEX_URL=... 一次性生效。
ARG PIP_INDEX_URL=https://pypi.org/simple

# 先只拷贝依赖清单 → 依赖没变时这一层走缓存，改代码不用重装依赖
COPY requirements.txt ./
RUN pip install --no-cache-dir --index-url "${PIP_INDEX_URL}" -r requirements.txt

# 再拷代码。⚠️ `core/prompts/*.md`、`core/questions_config.json` 等**都是运行时读的**，
# 漏掉任何一个都会在生成时炸（提示词是外置文件，不是写死在代码里的）。
COPY . .

EXPOSE 8000

# `--timeout-keep-alive 75`：一次生成要挂几十秒（接口文档分 3 片 ≈ 90 秒），
# nginx 那边也要相应放大 proxy_read_timeout（见 deploy/nginx.conf 的注释）。
#
# **worker 数**：默认 1。生成请求是 SSE、单请求持续几十秒到几分钟，而真正的瓶颈是
# 上游模型 —— 加 worker 能把并发摊开（LLM 调用是 IO 等待，多进程确实有效），
# 代价是内存与"同一会话并发写"的风险（会话现在根本没有服务端状态，所以暂时无所谓）。
# 要调就加 `--workers N`。
CMD ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--timeout-keep-alive", "75"]
