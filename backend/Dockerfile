# syntax=docker/dockerfile:1

# ─────────────────────────────────────────────────────────────
# 后端镜像：FastAPI + uvicorn
#
# 构建（在 backend/ 目录下执行）：
#     docker build -t text-to-sql-agent-backend .
# 运行：
#     docker run --rm -p 8000:8000 text-to-sql-agent-backend
#
# 镜像默认以「离线模拟模式」启动，不需要任何密钥就能打开 /docs 与完整界面。
# 要连真实模型，运行时用环境变量覆盖：
#     docker run --rm -p 8000:8000 \
#       -e LLM_MOCK=false \
#       -e LLM_API_KEY=sk-xxx \
#       -e LLM_BASE_URL=https://api.deepseek.com/v1 \
#       text-to-sql-agent-backend
# ─────────────────────────────────────────────────────────────

FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# 先只拷依赖清单、装完依赖再拷源码。
# 这样改业务代码时这一层能命中缓存，不必每次重新装一遍依赖
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# 离线模拟总开关。
# 代码里 LLM_MOCK 的默认值是 false，而 .env 被 .gitignore 排除、镜像里没有，
# 不显式打开的话容器一启动就会拿假 Key 去调真实模型接口。
# 运行时 -e LLM_MOCK=false 即可覆盖
ENV LLM_MOCK=true

# 构建期建好示例数据库。
# 不放在启动期是因为 init_database 的 drop_first 默认为 true，
# 每次重启都重建一遍纯属浪费时间；示例数据是固定种子生成的，只读用途，
# 打进镜像里正合适
RUN python -m app.agent.database.init_database

EXPOSE 8000

# 打应用自己的 /api/health，而不是只探端口：
# 端口通不代表服务装配完成（比如状态图预热失败）
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
