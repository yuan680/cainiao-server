# ═════════════════════════════════════════════════════════════
# 菜鸟物流查询 — Render 优化部署
# 目标镜像大小 ≈ 260MB（远 < 512MB）
# 采用 Playwright Chromium Headless Shell（比 apt chromium 小 60%）
# 防限流：curl_cffi TLS 指纹模拟 + Playwright 降级绕过滑块
# ═════════════════════════════════════════════════════════════
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1
# Playwright 浏览器存储路径
ENV PLAYWRIGHT_BROWSERS_PATH=/app/pw-browsers
# Chromium 启动参数（--single-process + 限制 JS 堆 256MB 避免 OOM）
ENV CHROMIUM_FLAGS="--no-sandbox --disable-dev-shm-usage --single-process --disable-gpu --no-zygote --js-flags=--max_old_space_size=256"

# ── 1. 系统依赖（Chromium Headless Shell 运行时最小集） ──
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    libnss3 \
    libnspr4 \
    libatk1.0-0t64 \
    libatk-bridge2.0-0t64 \
    libcups2t64 \
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
    libasound2t64 \
    && rm -rf /var/lib/apt/lists/*

# ── 2. 安装 Playwright + 下载最小化 Chromium Headless Shell ──
#     Headless Shell ≈70MB（仅为 apt chromium 的 1/3）
RUN pip install --no-cache-dir 'playwright==1.52.0' \
    && python -m playwright install --only-shell chromium

WORKDIR /app

# ── 3. 应用 Python 依赖 ──
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── 4. 复制应用代码（仅部署必需文件） ──
COPY cainiao_server.py gsheet_writer.py ./

# Render / Cloud Run 自动注入 PORT 环境变量
EXPOSE 58080

HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD python -c "import os; import urllib.request; p=os.environ.get('PORT','58080'); urllib.request.urlopen(f'http://localhost:{p}/health', timeout=5)" || exit 1

CMD ["python", "cainiao_server.py", "--host", "0.0.0.0"]
