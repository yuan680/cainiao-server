#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WPS 多维表 → 菜鸟物流查询 本地桥接服务

工作流程:
  1. 运行本脚本启动本地 HTTP 服务 (默认 http://127.0.0.1:8080)
  2. 在 WPS 多维表中添加「按钮」字段，动作设为「打开网页」
  3. URL 填写: http://127.0.0.1:8080/?no={物流单号}
  4. 点击按钮 → 查询结果自动显示，可直接复制到「物流状态」列

无需任何 AppKey/AppSecret，直接调用菜鸟全球公开 API。
"""

import http.server
import json
import socketserver
import sys
import time
import urllib.parse
from typing import Optional

import requests

# ── 配置 ──────────────────────────────────────────────────
HOST = "127.0.0.1"
PORT = 8080

LOCAL_API_URL = "http://127.0.0.1:58080/query"
# ─────────────────────────────────────────────────────────


def query_cainiao(mail_no: str) -> Optional[dict]:
    """通过本地 cainiao_server 中间层查询，返回其 JSON 或 None"""
    try:
        resp = requests.get(
            LOCAL_API_URL,
            params={"mailNo": mail_no, "lang": "zh-CN"},
            timeout=15,
        )
        # 即使 HTTP 200，业务 code 非 0 仍视为失败
        data = resp.json()
        if data.get("code") == 0 and data.get("data"):
            return data
        return None
    except Exception:
        return None


def extract_status(data: dict) -> dict:
    """从 cainiao_server 响应中提取摘要字段"""
    result = {
        "mail_no": "",
        "status": "查询失败",
        "status_code": "",
        "origin": "",
        "dest": "",
        "last_time": "",
        "last_event": "",
        "events": [],
    }
    package_list = data.get("data", [])
    if not package_list:
        return result

    item = package_list[0]
    result["mail_no"] = item.get("mailNo", "")
    result["status"] = item.get("status", "未知")
    # 有 events 且 status 不是"查询失败"才算有状态码
    events = item.get("events", [])
    result["status_code"] = "OK" if result["status"] not in ("查询失败", "") else ""
    result["origin"] = item.get("origin", "")
    result["dest"] = item.get("dest", "")

    if events:
        last = events[-1]
        result["last_time"] = last.get("time", "")
        result["last_event"] = last.get("status", "")
        # 若 events 中有 location 信息可附加
        loc = last.get("location", "")
        if loc:
            result["last_event"] = f"{result['last_event']} - {loc}"

    for evt in events:
        result["events"].append({
            "time": evt.get("time", ""),
            "desc": evt.get("status", ""),
            "carrier": evt.get("location", ""),
        })
    return result


def render_html(status: dict, mail_no: str) -> str:
    """生成简洁查询结果页面"""
    ok = status["status_code"] != ""
    color = "#34a853" if ok else "#ea4335"
    emoji = "✅" if ok else "❌"

    event_rows = ""
    for e in status["events"]:
        carrier_note = f'<span class="carrier">📎 {e["carrier"]}</span>' if e.get("carrier") and e["carrier"] != e["desc"] else ""
        event_rows += f"""<tr><td>{e["time"]}</td><td>{e["desc"]}</td><td>{carrier_note}</td></tr>"""

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>物流查询结果</title>
<style>
* {{ margin:0; padding:0; box-sizing:border-box; }}
body {{ font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif; background:#f5f5f5; padding:20px; }}
.card {{ max-width:680px; margin:0 auto; background:#fff; border-radius:12px; box-shadow:0 2px 12px rgba(0,0,0,.08); overflow:hidden; }}
.header {{ padding:24px 24px 16px; border-bottom:1px solid #eee; }}
.header h1 {{ font-size:20px; color:#333; }}
.header .no {{ color:#888; font-size:14px; margin-top:4px; word-break:break-all; }}
.badge {{ display:inline-block; padding:6px 16px; border-radius:20px; font-size:16px; font-weight:600; color:#fff; background:{color}; margin-top:10px; }}
.body {{ padding:24px; }}
.info-grid {{ display:grid; grid-template-columns:1fr 1fr; gap:12px; margin-bottom:20px; }}
.info-item {{ }}
.info-item label {{ display:block; font-size:12px; color:#999; margin-bottom:2px; }}
.info-item span {{ font-size:14px; color:#333; }}
.info-item .empty {{ color:#ccc; }}
table {{ width:100%; border-collapse:collapse; }}
th {{ text-align:left; padding:10px 8px; font-size:12px; color:#999; border-bottom:2px solid #eee; }}
td {{ padding:8px; font-size:13px; color:#333; border-bottom:1px solid #f0f0f0; }}
.carrier {{ display:block; color:#888; font-size:11px; margin-top:2px; }}
.footer {{ text-align:center; padding:16px; color:#bbb; font-size:12px; border-top:1px solid #eee; }}
.back {{ display:inline-block; margin-top:12px; color:#1a73e8; text-decoration:none; cursor:pointer; }}
.back:hover {{ text-decoration:underline; }}
</style>
</head>
<body>
<div class="card">
<div class="header">
<h1>{emoji} {status["status"]}</h1>
<div class="no">📦 {mail_no}</div>
</div>
<div class="body">
<div class="info-grid">
<div class="info-item"><label>发件地</label><span>{status["origin"] or "-"}</span></div>
<div class="info-item"><label>目的地</label><span>{status["dest"] or "-"}</span></div>
<div class="info-item"><label>最后更新时间</label><span>{status["last_time"] or "-"}</span></div>
<div class="info-item"><label>最后事件</label><span>{status["last_event"] or "-"}</span></div>
</div>
<table><thead><tr><th>时间</th><th>事件</th><th>承运商备注</th></tr></thead><tbody>{event_rows}</tbody></table>
</div>
<div class="footer">
<button class="back" onclick="window.close()">关闭窗口</button>
</div>
</div>
</body>
</html>"""


class Handler(http.server.BaseHTTPRequestHandler):
    """HTTP 请求处理"""

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        mail_no = (params.get("no") or [""])[0].strip()

        if parsed.path == "/":
            # 首页 + 查询表单
            if mail_no:
                result = self._do_query(mail_no)
                self._send_html(result)
            else:
                self._send_form()
        elif parsed.path == "/query":
            # JSON API
            result = self._do_query(mail_no) if mail_no else {"error": "缺少参数 no"}
            self._send_json(result)
        elif parsed.path == "/health":
            self._send_json({"status": "ok", "time": time.strftime("%Y-%m-%d %H:%M:%S")})
        else:
            self.send_response(404)
            self.send_header("Content-Type", "text/plain;charset=utf-8")
            self.end_headers()
            self.wfile.write(b"404 Not Found")

    def _do_query(self, mail_no: str) -> dict:
        """执行查询并返回结构化的结果字典"""
        raw = query_cainiao(mail_no)
        if raw is None:
            return {"error": f"运单号 {mail_no} 查询失败（网络错误）"}
        status = extract_status(raw)
        if not status["status_code"]:
            return {"error": f"未找到运单 {mail_no} 的物流信息", "raw": raw}
        return status

    def _send_form(self):
        """返回带查询框的首页"""
        html = """<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>菜鸟物流查询 - WPS 桥接服务</title>
<style>
body{font-family:-apple-system,sans-serif;background:#f5f5f5;display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0;padding:20px;}
.card{background:#fff;border-radius:12px;padding:32px;max-width:480px;width:100%;box-shadow:0 2px 12px rgba(0,0,0,.08);text-align:center;}
h1{font-size:22px;color:#333;margin-bottom:8px;}
p{color:#888;font-size:14px;margin-bottom:24px;}
input[type=text]{width:100%;padding:12px 16px;border:1px solid #ddd;border-radius:8px;font-size:16px;outline:none;transition:border .2s;}
input[type=text]:focus{border-color:#1a73e8;}
button{margin-top:12px;padding:12px 32px;border:none;border-radius:8px;background:#1a73e8;color:#fff;font-size:16px;cursor:pointer;transition:background .2s;}
button:hover{background:#1557b0;}
.footer{margin-top:24px;color:#bbb;font-size:12px;}
</style></head>
<body>
<div class="card">
<h1>📦 菜鸟物流查询</h1>
<p>输入运单号查询全球物流轨迹</p>
<form method="get" action="/">
<input type="text" name="no" placeholder="请输入运单号" autofocus>
<button type="submit">查询</button>
</form>
<div class="footer">WPS 多维表桥接服务 v1.0</div>
</div>
</body>
</html>"""
        self._send_html(html)

    def _send_html(self, content: str):
        self.send_response(200)
        self.send_header("Content-Type", "text/html;charset=utf-8")
        self.end_headers()
        if isinstance(content, dict):
            error = content.get("error", "查询失败")
            html = f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>查询结果</title><style>
body{{font-family:-apple-system,sans-serif;background:#f5f5f5;display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0;padding:20px;}}
.card{{background:#fff;border-radius:12px;padding:32px;max-width:480px;width:100%;box-shadow:0 2px 12px rgba(0,0,0,.08);text-align:center;}}
.error{{color:#ea4335;font-size:18px;margin:16px 0;}}
pre{{text-align:left;background:#f8f8f8;padding:12px;border-radius:8px;font-size:12px;overflow-x:auto;}}
button{{margin-top:16px;padding:10px 28px;border:none;border-radius:8px;background:#1a73e8;color:#fff;font-size:14px;cursor:pointer;}}
button:hover{{background:#1557b0;}}
</style></head><body>
<div class="card"><div class="error">❌ {error}</div>
<button onclick="window.close()">关闭</button></div></body></html>"""
            content = html
        self.wfile.write(content.encode("utf-8"))

    def _send_json(self, obj):
        self.send_response(200 if "error" not in obj else 404)
        self.send_header("Content-Type", "application/json;charset=utf-8")
        self.end_headers()
        self.wfile.write(json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8"))

    def log_message(self, format, *args):
        sys.stderr.write(f"[WPS Server] {args[0]} {args[1]} {args[2]}\n")


def main():
    server = socketserver.ThreadingTCPServer((HOST, PORT), Handler)
    print(f"✅ WPS 桥接服务已启动")
    print(f"   地址: http://{HOST}:{PORT}")
    print(f"   首页: http://{HOST}:{PORT}/")
    print(f"   API:  http://{HOST}:{PORT}/query?no=运单号")
    print(f"   WPS 多维表按钮 URL 填: http://{HOST}:{PORT}/?no={{物流单号}}")
    print(f"   按 Ctrl+C 停止服务")
    print("=" * 50)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n服务已停止")
        server.server_close()


if __name__ == "__main__":
    main()
