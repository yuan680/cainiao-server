# WPS 多维表集成物流轨迹查询指南

**核心发现：菜鸟全球物流公开 API 无需鉴权**。可直接调用 `global.cainiao.com` 接口，不需要 AppKey/AppSecret。

---

## ⚠️ 关键限制：AirScript 无法调 localhost（详见独立文档）

WPS 多维表的「执行 AirScript 脚本」运行在**云端沙箱**，不能访问 `localhost`/`127.0.0.1`。
- **按钮「执行 AirScript 脚本」** → 必须用公网 URL（已预置 `PUBLIC_URL`）
- **按钮「发送 HTTP 请求」** ✅ 桌面端可访问本地服务，不受此限制

> ❗ **JS 宏（AirScript）中写 localhost 永远无效**，不存在"失败后自动降级"的可能。
> 详细分析见 [WPS_AirScript关键限制.md](WPS_AirScript关键限制.md)

公网服务地址（已部署到 Render）：
```
https://cainiao-server.onrender.com/query?mailNo=LP00812637173551
```

---

## 两大核心方案

### 方案 A：按钮「发送 HTTP 请求」（单行 · 零配置）

最简单方式，每行一个按钮，点击即查单行。

**准备工作**：右键 `install.bat` → **以管理员身份运行**（一次即可，开机自启）。

**表格字段配置**：

| 字段名 | 类型 | 说明 |
|--------|------|------|
| `物流单号` | **文本** | 输入运单号（如 LP00812637173551） |
| `物流状态` | **文本** | 自动接收查询结果 |
| `更新` | **按钮** | 配置为「发送 HTTP 请求」 |

**按钮配置**：
1. 点击「更新」列头 → 动作选 **「发送 HTTP 请求」**
2. 请求方式：`GET`
3. 请求 URL（推荐方式 A）：
   ```
   http://localhost/query?mailNo={物流单号}
   ```
   > `{物流单号}` 是跨列引用占位符，WPS 自动替换
4. 返回格式：`JSON`
5. 数据写入：
   - 目标列：`物流状态`
   - 映射值：`$data[0].status`

**验证**：浏览器打开 `http://localhost/query?mailNo=LP00812637173551` 应返回 JSON。

---

### 方案 B：JS 宏全表查询（批量 · 无 500 行限制）

一键遍历全部行（1597+ 行），支持状态判断、多列写入、自动限速。

**脚本文件**：`wps_button_flow_rr.js`（RecordRange 方案）

**优势**：

| 特性 | 按钮「HTTP 请求」 | JS 宏 ✅ |
|------|-------------------|----------|
| 批量查询所有行 | ❌ 一次一行 | ✅ 一键全表 |
| 写入多列 | ❌ 只能写一列 | ✅ 状态+时间等分列 |
| 跳过已完结 | ❌ | ✅ 已签收/退回自动跳过 |
| 不受 500 行限制 | ✅ 单行查询 | ✅ RecordRange 逐行读写 |
| 批间限流 | — | ✅ 每批 100 条，间隔 1.5s |

#### 第一步：创建脚本

1. 打开 WPS 多维表 → **「脚本」→「脚本编辑器」**
2. **「新建脚本」** → 粘贴 `wps_button_flow_rr.js` 全部内容
3. 保存为 `菜鸟物流查询`，关闭编辑器

#### 第二步：配置列索引（脚本顶部配置区）

根据你当前表结构（14 列），默认值如下：

```javascript
var SHEET_NAME = "";              // 留空自动检测当前活动表格
var COL_MAIL_NO  = 4;   // 物流单号（第 4 列）
var COL_STATUS   = 5;   // 物流状态（第 5 列，纯文本字段）
var COL_TIME     = 6;   // 更新时间（第 6 列，日期字段）
```

> **日期格式注意**：WPS 日期字段接受 `YYYY/MM/DD HH:mm`（`/` 分隔），不接受 `-` 分隔。

#### 第三步：运行

- **一键全表**：菜单 **「脚本」→「菜鸟物流查询」**
- **单行按钮**：配置按钮动作 →「执行 AirScript 脚本」→ 选 `菜鸟物流查询.查询物流`

---

## 关于 500 行限制

`Application.Record.GetRecords()` **最多返回前 500 条**（PageSize 上限 500，Page/offset 无效）。

**突破方案**：使用 `Application.Sheets("数据表").RecordRange` 直接按行列索引操作，不受 500 行限制。`wps_button_flow_rr.js` 即采用此方案。

```javascript
var sh = Application.Sheets("数据表");
var rr = sh.RecordRange;

// 读第 4 行第 4 列（物流单号）
var mailNo = rr(4, 4).Value;

// 写第 4 行第 5 列（物流状态）
rr(4, 5).Value = "已签收 | 快件已签收";
```

---

## API 返回值参考

```
GET http://localhost/query?mailNo=LP00812637173551
```

```json
{
  "code": 0,
  "data": [{
    "mailNo": "LP00812637173551",
    "status": "已签收",
    "statusCode": "signed",
    "origin": "中国",
    "dest": "美国",
    "latestTime": "2024-01-15 14:30:00",
    "latestEvent": "快件已签收",
    "eventCount": 12
  }]
}
```

| 路径 | 说明 |
|------|------|
| `$data[0].status` | 物流状态 |
| `$data[0].latestEvent` | 最新事件 |
| `$data[0].latestTime` | 最新时间 |
| `$data[0].dest` | 目的地 |

---

## 文件清单

| 文件 | 用途 |
|------|------|
| `cainiao_server.py` | **本地 HTTP 服务**（WPS 按钮调用端） |
| `cainiao_track.py` | 命令行查询 + CSV 导出 |
| `wps_button_flow_rr.js` | ⭐ **全表查询脚本**（RecordRange，推荐） |
| `wps_button_flow.js` | 按钮脚本（GetRecords，限前 500 行） |
| `install.bat` | 一键安装（端口转发 80→58080 + 开机自启） |
| `start_server.bat` | 手动启动本地服务 |

---

## 常见问题

| 问题 | 原因 | 解决 |
|------|------|------|
| 按钮点后无反应 | 本地服务未启动 | 运行 `install.bat` 或 `start_server.bat` |
| 返回空数据 | 运单号不存在 | 到菜鸟官网验证 |
| 提示"运行流程失败" | 脚本/函数名错误 | 重新选「菜鸟物流查询.查询物流」 |
| 端口被占用 | 58080 已被使用 | 关闭占用程序，或改 `--port`
