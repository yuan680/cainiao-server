# ════════════════════════════════════════════════════════════
# 菜鸟物流查询 — Google Cloud Run 部署
# 使用 Playwright 官方 Python 基础镜像（含 Chromium）
# ════════════════════════════════════════════════════════════
FROM mcr.microsoft.com/playwright/python:v1.40.0

ENV PYTHONUNBUFFERED=1

WORKDIR /app

# ─── 安装 Python 依赖 ───
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ─── 复制应用代码 ───
COPY cainiao_server.py .

# ─── Cloud Run 会自动注入 PORT 环境变量 ───
# cainiao_server.py 已支持通过 PORT 动态监听端口
EXPOSE 58080

HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; import os; p=os.environ.get('PORT','58080'); urllib.request.urlopen(f'http://localhost:{p}/health')" || exit 1

CMD ["python", "cainiao_server.py", "--host", "0.0.0.0"]
