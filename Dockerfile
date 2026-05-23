# ════════════════════════════════════════════════════════════
# 菜鸟物流查询 — 云端 Docker 部署
# 兼容 Railway / Render / fly.io 等平台
# ════════════════════════════════════════════════════════════
FROM python:3.11-slim

# ─── 系统依赖（Playwright 自动安装所需库） ───
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    # Playwright 自动检测并安装 Chromium 系统依赖
    && rm -rf /var/lib/apt/lists/*

# ─── Python 依赖 ───
ENV PYTHONUNBUFFERED=1
ENV PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 安装 Playwright 浏览器 + 系统库（install-deps 会自动 apt-get install）
RUN python -m playwright install chromium && \
    python -m playwright install-deps chromium

# ─── 应用代码 ───
COPY cainiao_server.py .

# ─── 运行时 ───
# Railway / Render 会自动注入 PORT 环境变量
# cainiao_server.py 检测到 PORT 后自动监听 0.0.0.0:$PORT
EXPOSE 58080

HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; import os; p=os.environ.get('PORT','58080'); urllib.request.urlopen(f'http://localhost:{p}/health')" || exit 1

CMD ["python", "cainiao_server.py", "--host", "0.0.0.0"]
