# ⚠️ WPS AirScript 关键限制：无法访问本地代理

## 核心结论

**WPS 多维表的 AirScript 脚本（包括「执行 AirScript 脚本」按钮和脚本编辑器运行）运行在云端沙箱，绝对不能访问 localhost/127.0.0.1 或本机任何端口。**

| 执行方式 | 能否访问 localhost | 说明 |
|----------|:---:|------|
| 按钮「执行 AirScript 脚本」 | ❌ 不能 | 云端沙箱，无本地网络 |
| 脚本编辑器「运行」 | ❌ 不能 | 同上 |
| 按钮「发送 HTTP 请求」 | ✅ 桌面端可以 | 客户端本机发出，不受此限制 |

---

## 为什么不能用本地代理

```javascript
// ❌ 在 AirScript 中永远失败（云端沙箱无法连接）
var API_URL = "http://localhost:58080/query";

// ✅ 必须用公网地址
var API_URL = "https://cainiao-server.onrender.com/query";
```

WPS 多维表的 AirScript 环境特征：
- 脚本在 WPS 云端服务器执行，不是在用户本地
- 没有网络访问到用户局域网或本机
- `localhost` 指向的是 WPS 云容器自身，不是你的电脑
- `HTTP.get()` / `UrlFetchApp.fetch()` / `XMLHttpRequest` 全部受此限制

---

## 哪些场景受影响

| 场景 | 方案 | 是否可行 |
|------|------|:--------:|
| 按钮「发送 HTTP 请求」→ `localhost` | 本地端口转发 80→58080 | ✅ 桌面端 |
| JS 宏批量查 → 公网 URL | `cainiao-server.onrender.com` | ✅ |
| JS 宏批量查 → `localhost:58080` | 本地 Python 代理 | ❌ **永远失败** |
| JS 宏批量查 → `127.0.0.1:58080` | 同上 | ❌ **永远失败** |
| 按钮「执行 AirScript」→ 任何 localhost | 同上 | ❌ **永远失败** |

---

## 正确架构

```
┌─────────────────────────────────────────────────┐
│ 用户电脑 (桌面端 WPS)                            │
│                                                  │
│  按钮「发送 HTTP 请求」──→ localhost:58080       │
│                            (cainiao_server.py)   │
│                                    ↓             │
│                           菜鸟 API (global.cainiao.com)│
└─────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────┐
│ WPS 云端沙箱 (AirScript)                         │
│                                                  │
│  HTTP.get() ──→ Render 公网服务                  │
│                  (cainiao-server.onrender.com)    │
│                          ↓                       │
│                  菜鸟 API (global.cainiao.com)    │
└─────────────────────────────────────────────────┘
```

---

## 历史教训

1. 曾尝试在 AirScript 中直接调用菜鸟 API → 被 WAF 拦截（TLS 指纹）
2. 为了解决 WAF 拦截，启动本地 Python 代理 → AirScript 连不上 localhost
3. 最终方案：**本地代理只给「发送 HTTP 请求」按钮用，AirScript 一律走 Render 公网服务**
4. 如果 Render 免费版休眠（15分钟无请求冷启动），第一次查询需要等 30-60 秒

---

## 相关文件

| 文件 | 用途 | 访问方式 |
|------|------|----------|
| `cainiao_server.py` | 本地代理服务器（端口 58080） | 仅按钮「发送 HTTP 请求」 |
| `wps_macro_tracking.js` | AirScript 批量查询脚本 | **必须用公网 URL** |
| `wps_button_flow_rr.js` | AirScript 全表查询脚本 | **必须用公网 URL** |
| `cainiao-server.onrender.com` | Render 公网代理服务 | AirScript + 按钮共用 |
