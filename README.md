# 菜鸟物流查询服务

基于 Python + curl_cffi (TLS 指纹模拟) + Playwright (降级绕过滑块验证) 的物流查询服务，部署于 Render Hong Kong。

## 目录

- [架构概述](#架构概述)
- [快速部署（Render）](#快速部署render)
- [防止冷启动](#防止冷启动)
- [WPS AirScript 客户端](#wps-airscript-客户端)
- [本地开发](#本地开发)
- [环境变量](#环境变量)

---

## 架构概述

```
┌──────────────┐    HTTP GET /query?mailNo=LPxxx     ┌──────────────────┐
│  WPS 表格     │ ──────────────────────────────────→ │  Cainiao Server   │
│  (AirScript)  │                                     │  (Render:58080)   │
│  客户端脚本    │ ←── JSON 结果 ───────────────────── │                   │
└──────────────┘                                     │  curl_cffi ─→ 菜鸟 API │
                                                     │  └→ Playwright (降级)  │
                                                     └──────────────────┘
```

**核心依赖：**
- `curl_cffi` — 模拟浏览器 TLS 指纹，降低被菜鸟 API 限流概率
- `Playwright (Chromium Headless Shell)` — 当 curl_cffi 被限流时自动降级，通过真实浏览器环境绕过滑块验证

---

## 快速部署（Render）

项目自带 [render.yaml](render.yaml)，支持 Render Blueprint 一键部署：

1. 将代码推送至 GitHub 仓库
2. 在 [Render Dashboard](https://dashboard.render.com) → **New +** → **Blueprint**
3. 选择仓库，Render 自动识别 `render.yaml` 并部署
4. 部署完成后获得 URL：`https://cainiao-server.onrender.com`

**验证部署：**
```bash
curl https://cainiao-server.onrender.com/health
# → {"status": "ok", "time": "2026-01-15T10:30:00"}
```

**注意：** Render 免费计划实例在 **15 分钟无请求后自动休眠**。详见下方「防止冷启动」章节。

---

## 防止冷启动

### 问题说明

Render 免费计划（Free Plan）的实例在 **闲置 15 分钟后自动休眠**，下次请求需要等待约 **14 秒冷启动**（Docker 镜像拉起 + Python 启动 + Playwright 浏览器初始化）。

### 解决方案：UptimeRobot 定时监测

推荐使用 [UptimeRobot](https://uptimerobot.com)（免费版可监控 5 个站点，每 5 分钟检测一次）保持实例活跃。

**配置步骤：**

1. 注册 UptimeRobot 并登录
2. 点击 **Monitor → Add New Monitor**
3. **Monitor Type**: 选择 `HTTP(s)`
4. **Friendly Name**: `菜鸟物流-健康检查`
5. **URL**: `https://cainiao-server.onrender.com/health`
6. **Interval**: 选择 `Every 5 minutes`（免费版最低间隔）
7. **Timeout**: 保持默认 `30 seconds`
8. 点击 **Create Monitor**

### 工作原理

`/health` 端点极其轻量（仅返回 JSON 状态，不访问菜鸟 API），每 5 分钟被调用一次，足以阻止 Render 进入休眠：

```json
GET /health → {"status": "ok", "time": "2026-01-15T10:30:00"}
```

### 备选方案

| 服务 | 免费额度 | 最低间隔 |
|------|---------|---------|
| [UptimeRobot](https://uptimerobot.com) | 5 个站点 | 5 分钟 |
| [Better Uptime](https://betteruptime.com) | 1 个站点 | 3 分钟 |
| [cron-job.org](https://cron-job.org) | 免费无限制 | 5 分钟 |
| GitHub Actions（自建） | 2000 分钟/月 | 自行控制 |

---

## WPS AirScript 客户端

项目包含两个 WPS 表格客户端脚本，用于自动读取表格中的 LP 单号并发起查询。

### wps_button_flow.js

通过 WPS 按钮触发，适用于手动启动场景：

- **路径**: `wps_button_flow.js`
- **工作模式**: 读取整张表格的 LP 单号 → 分批发送 HTTP 请求 → 写回状态
- **每批最大**: 100 单（`MAX_PER_RUN`）
- **批间延迟**: 200ms（`BETWEEN_BATCH_DELAY_MS`）
- **超时保护**: 60 分钟自动停止

### wps_airscript_direct.js

适用于 WPS 定时任务/自动触发场景：

- **路径**: `wps_airscript_direct.js`
- **工作模式**: 直接查询菜鸟 API（不经过中间服务器），包含浏览器指纹模拟
- **节流控制**: 每 20 行暂停 1 秒（`THROTTLE_EVERY=20`）
- **备用策略**: 自动从完整浏览器头 → 最小头 → 控制函数降级

### 配置方法

在两个脚本头部找到配置区，需要修改：

```javascript
var PUBLIC_URL = "https://cainiao-server.onrender.com";  // 你的服务地址
```

---

## 本地开发

### 环境要求

- Python 3.11+
- pip

### 安装与启动

```bash
# 1. 安装依赖
pip install curl_cffi

# 2. 可选 - 安装 Playwright（启用滑块验证降级）
pip install playwright
playwright install chromium

# 3. 启动服务
python cainiao_server.py --host 127.0.0.1 --port 58080

# 4. 测试
curl "http://127.0.0.1:58080/query?mailNo=LP00812637173551"
```

### 可用端点

| 路径 | 说明 |
|------|------|
| `GET /query?mailNo=LPxxx` | 查询物流（支持逗号分隔批量） |
| `GET /health` | 健康检查 |
| `GET /` | 服务信息 |

---

## 环境变量

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `PORT` | 监听端口（Cloud Run 自动注入） | `58080` |
| `PROXY_LIST` | 住宅代理列表（可选） | — |

---

## 优化记录

| 日期 | 优化项 | 效果 |
|------|--------|------|
| 2026-01 | 服务器重试退避 2s/4s → 1s 固定 | 缩短错误恢复时间 |
| 2026-01 | WPS 批间延迟 1000ms → 200ms | 大幅减少总等待时间 |
| 2026-01 | WPS 直接查询节流 5行 → 20行 | 减少无用等待 |
