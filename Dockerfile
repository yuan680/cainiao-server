# ════════════════════════════════════════════════════════════
# 菜鸟物流查询 — Render / Cloud Run 部署
# 使用 python:3.11-slim + 系统 Chromium（镜像 ~400MB）
# ════════════════════════════════════════════════════════════
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1
ENV PLAYWRIGHT_CHROMIUM_PATH=/usr/bin/chromium
# 降低 Chromium 内存占用（Render 免费实例 512MB RAM）
ENV CHROMIUM_FLAGS="--no-sandbox --disable-dev-shm-usage --single-process --disable-gpu --no-zygote"

# 安装 Chromium 及系统依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    chromium \
    chromium-driver \
    libglib2.0-0 \
    libnss3 \
    libnspr4 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libdbus-1-3 \
    libxkbcommon0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libpango-1.0-0 \
    libcairo2 \
    libasound2 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 安装 Python 依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制应用代码
COPY cainiao_server.py .

# Render / Cloud Run 会自动注入 PORT 环境变量
EXPOSE 58080

HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; import os; p=os.environ.get('PORT','58080'); urllib.request.urlopen(f'http://localhost:{p}/health')" || exit 1

CMD ["python", "cainiao_server.py", "--host", "0.0.0.0"]
