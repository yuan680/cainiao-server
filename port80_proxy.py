#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
port80_proxy.py — 本地 80 端口代理服务
=======================================
功能: 监听 80 端口，将请求转发到 58080 端口上的 cainiao_server

为什么需要此脚本:
  WPS AirScript 的 HTTP 模块仅支持 80 端口，而 cainiao_server.py 运行在 58080 端口。
  Windows netsh portproxy 在 localhost→localhost 回环上不可靠，此脚本更稳定。

启动:
  python port80_proxy.py
  (需要管理员权限，因为 80 端口是特权端口)

与 cainiao_server.py 配合:
  1. 启动 cainiao_server.py --port 58080  (主服务)
  2. 启动 port80_proxy.py                 (转发代理)
  WPS 请求 http://localhost/query?mailNo=...  →  转发到 58080
"""

import json
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
import urllib.request
import urllib.error

UPSTREAM = "http://127.0.0.1:58080"


class ProxyHandler(BaseHTTPRequestHandler):
    """将收到的请求转发到上游 58080 服务，并返回结果"""

    def do_GET(self):
        self._proxy_request()

    def do_OPTIONS(self):
        """CORS preflight"""
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def _proxy_request(self):
        upstream_url = UPSTREAM + self.path
        try:
            req = urllib.request.Request(upstream_url)
            req.add_header("User-Agent", "WPS-AirScript-Proxy/1.0")
            with urllib.request.urlopen(req, timeout=15) as resp:
                body = resp.read()
                self.send_response(resp.status)
                # 透传上游 Content-Type
                ct = resp.headers.get("Content-Type", "application/json")
                self.send_header("Content-Type", ct)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        except urllib.error.HTTPError as e:
            body = e.read()
            self.send_response(e.code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except urllib.error.URLError as e:
            msg = json.dumps({
                "code": -1,
                "error": f"无法连接上游服务(58080): {e.reason}",
            }, ensure_ascii=False).encode("utf-8")
            self.send_response(502)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(msg)))
            self.end_headers()
            self.wfile.write(msg)
        except Exception as e:
            msg = json.dumps({
                "code": -1,
                "error": f"代理错误: {str(e)}",
            }, ensure_ascii=False).encode("utf-8")
            self.send_response(500)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(msg)))
            self.end_headers()
            self.wfile.write(msg)

    def log_message(self, format, *args):
        print(f"[port80] {self.client_address[0]} - {format % args}")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="80端口转发代理 → 58080")
    parser.add_argument("--host", default="0.0.0.0", help="监听地址（默认 0.0.0.0）")
    parser.add_argument("--port", type=int, default=80, help="监听端口（默认 80）")
    args = parser.parse_args()

    server = HTTPServer((args.host, args.port), ProxyHandler)
    print(f"🚀 80端口代理已启动  →  {UPSTREAM}")
    print(f"   📍 http://localhost:{args.port}")
    print(f"   📖 转发示例: http://localhost/query?mailNo=LP00812637173551")
    print(f"   ℹ  确保 cainiao_server.py 已在 58080 端口运行")
    print(f"   ❌ 按 Ctrl+C 停止")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n🛑 代理已停止")
        server.server_close()


if __name__ == "__main__":
    main()
