#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
菜鸟物流查询 — 本地 HTTP 服务
=============================
供 WPS 多维表按钮调用（发送 HTTP 请求），无需鉴权。

启动:
  python cainiao_server.py

  # 指定端口（默认 58080）
  python cainiao_server.py --port 58080

WPS 多维表按钮配置:
  触发动作: 发送 HTTP 请求
  请求方式: GET
  请求 URL: http://localhost:58080/query?mailNo=LP00812637173551
  返回格式: JSON
  结果写入: {物流状态} 列 = $data.status+":"+$data.latest_event

启动后在浏览器测试:
  http://localhost:58080/query?mailNo=LP00812637173551

开机自启（注册为计划任务）:
  schtasks /Create /SC ONLOGON /TN "CainiaoTrackServer" /TR "python F:\\cainiao_track\\cainiao_server.py" /RL HIGHEST
"""

import json
import sys
import os
import time
import random
import threading
import queue
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from datetime import datetime
from typing import Optional

# 每个 HTTP 请求的上下文（线程本地）
_req_ctx = threading.local()

# ============================================================
# Google Sheets 写入（可选懒加载）
# ============================================================
_GSHEET_WRITER = None
_GSHEET_READY = False


def _get_gsheet_writer():
    """懒加载 Google Sheets 写入器。"""
    global _GSHEET_WRITER, _GSHEET_READY
    if _GSHEET_READY:
        return _GSHEET_WRITER
    if not GOOGLE_SHEET_ID:
        _GSHEET_READY = True
        return None
    try:
        from gsheet_writer import GoogleSheetsWriter
        _GSHEET_WRITER = GoogleSheetsWriter(creds_path=GOOGLE_SHEET_CREDENTIALS)
        _GSHEET_WRITER.set_sheet(GOOGLE_SHEET_ID, GOOGLE_SHEET_WORKSHEET)
        _GSHEET_READY = True
        log(f"[GSHEET] Google Sheets 写入已启用 (Sheet: {GOOGLE_SHEET_ID})")
    except Exception as e:
        log(f"[GSHEET] 初始化失败: {e}（不影响查询功能）")
        _GSHEET_READY = True
    return _GSHEET_WRITER


def _write_results_to_sheet(results: list[dict]):
    """将查询结果异步写入 Google Sheets。"""
    import threading
    threading.Thread(target=_do_write_sheet, args=(results,), daemon=True).start()


def _do_write_sheet(results: list[dict]):
    """后台线程：实际写入。"""
    writer = _get_gsheet_writer()
    if writer is None:
        return
    header = [
        "mailNo", "status", "statusCode", "origin", "dest",
        "latestTime", "latestEvent", "eventCount",
    ]
    rows = []
    for r in results:
        rows.append([
            r.get("mailNo", ""),
            r.get("status", ""),
            r.get("statusCode", ""),
            r.get("origin", ""),
            r.get("dest", ""),
            r.get("latestTime", ""),
            r.get("latestEvent", ""),
            str(r.get("eventCount", 0)),
        ])
    writer.ensure_header(header)
    writer.write_rows(rows)

from curl_cffi import requests

# curl_cffi 自带 TLS 指纹模拟，无需额外 SSL 警告压制

# ============================================================
# 配置开关
# ============================================================
# 直接使用 curl_cffi（TLS 指纹模拟浏览器），速度最快
# Playwright 作为 curl_cffi 限流时的降级方案（页面导航模式）
# 云部署（Cloud Run / Docker）内置 Playwright 基镜像，可用浏览器兜底
# curl_cffi 优先，限流时降级到 Playwright（1GB RAM 足够跑 Chromium）
USE_PLAYWRIGHT = True
# 每次查询前的随机延时范围（秒），加入更大抖动模拟人类操作
# 批量场景下：0.8-1.5s 太规律，放大到 1.0-3.0s 更不容易触发限流
# 2026-05-23 优化：配合 WPS 批量请求模式（单 HTTP 含多单号），
# 延迟减半（0.5-1.5s），每批只等一次，整体速度翻倍。
QUERY_DELAY_MIN = 0.5
QUERY_DELAY_MAX = 1.5

# 全局冷却：当短时间内失败太多时，强制所有查询等待
_GLOBAL_COOLDOWN = 0.0          # 下次查询前需等待的时间戳（time.time）
_GLOBAL_COOLDOWN_LOCK = threading.Lock()
_GLOBAL_FAIL_WINDOW = []        # 最近失败时间戳（用于滑动窗口计数）
_GLOBAL_FAIL_WINDOW_LOCK = threading.Lock()
_GLOBAL_FAIL_THRESHOLD = 3      # 10 秒内超过 3 次失败 → 触发全局冷却
_GLOBAL_COOLDOWN_SECONDS = 15   # 触发后冷却 15 秒

def _check_global_cooldown():
    """检查是否需要全局冷却；若需要则等待。"""
    global _GLOBAL_COOLDOWN
    wait = 0.0
    with _GLOBAL_COOLDOWN_LOCK:
        now = time.time()
        if now < _GLOBAL_COOLDOWN:
            wait = _GLOBAL_COOLDOWN - now
    if wait > 0:
        capped = min(wait, 30)
        log(f"[GLOBAL] 全局冷却中，等待 {capped:.0f}s…")
        time.sleep(capped)

def _record_failure():
    """记录一次失败；若滑动窗口内失败数超阈值，触发全局冷却。"""
    global _GLOBAL_COOLDOWN
    now = time.time()
    with _GLOBAL_FAIL_WINDOW_LOCK:
        _GLOBAL_FAIL_WINDOW.append(now)
        # 只保留最近 10 秒
        _GLOBAL_FAIL_WINDOW[:] = [t for t in _GLOBAL_FAIL_WINDOW if now - t < 10]
        fail_count = len(_GLOBAL_FAIL_WINDOW)
    if fail_count >= _GLOBAL_FAIL_THRESHOLD:
        with _GLOBAL_COOLDOWN_LOCK:
            _GLOBAL_COOLDOWN = time.time() + _GLOBAL_COOLDOWN_SECONDS
        log(f"[GLOBAL] ⚠ 最近 {fail_count} 次失败，触发 {_GLOBAL_COOLDOWN_SECONDS}s 全局冷却")

def _record_success():
    """成功时清空失败窗口。"""
    with _GLOBAL_FAIL_WINDOW_LOCK:
        _GLOBAL_FAIL_WINDOW.clear()

# 菜鸟公开 API
PUBLIC_API_URL = "https://global.cainiao.com/global/detail.json"

# 日志输出（WPS 请求时需要静默运行，日志写入文件可选）
LOG_FILE = "cainiao_query.log"  # 每次请求的详细信息会写入此文件

# ============================================================
# Google Sheets 配置（留空则不启用）
# ============================================================
# Sheet ID: 从 Google Sheets URL 获取
#   https://docs.google.com/spreadsheets/d/【HERE】/edit
GOOGLE_SHEET_ID = os.environ.get("GOOGLE_SHEET_ID", "")

# 证书文件路径（环境变量优先，支持服务账号 JSON / OAuth token JSON）
GOOGLE_SHEET_CREDENTIALS = os.environ.get("GOOGLE_SHEET_CREDENTIALS", "")

# 工作表名称（默认第一个 tab）
GOOGLE_SHEET_WORKSHEET = os.environ.get("GOOGLE_SHEET_WORKSHEET", "Sheet1")

# ============================================================
# 方案 A：Playwright（真实 Chromium 浏览器）— 专用工作线程
# ============================================================
# Playwright sync API 对象绑定在创建线程上，因此用一个专用
# 后台线程运行所有操作，通过任务队列 + Event 通信，避免跨
# 线程崩溃。同时复用浏览器上下文（跳过每次创建 context / 访
# 问首页的耗时），单个查询可降至 1-2 秒。
_PLAYWRIGHT_LOCK = threading.Lock()
_PLAYWRIGHT_TASK_QUEUE = queue.Queue()   # (task_id, mail_no, lang)
_PLAYWRIGHT_RESULTS = {}                 # task_id -> dict | Exception
_PLAYWRIGHT_EVENTS = {}                  # task_id -> threading.Event
_PLAYWRIGHT_COUNTER = 0
_PLAYWRIGHT_THREAD: Optional[threading.Thread] = None
_PLAYWRIGHT_READY_EVENT = threading.Event()

# Playwright → curl_cffi Cookie 注入
_PLAYWRIGHT_COOKIES = []
_PLAYWRIGHT_COOKIES_LOCK = threading.Lock()
# Playwright 是否可用（只检测一次，避免重复启动失败）
_PLAYWRIGHT_AVAILABLE: Optional[bool] = None

def _check_playwright_available() -> bool:
    """快速检测 Playwright 是否可用（只检测一次并缓存结果）。"""
    global _PLAYWRIGHT_AVAILABLE
    if _PLAYWRIGHT_AVAILABLE is not None:
        return _PLAYWRIGHT_AVAILABLE
    try:
        import playwright.sync_api  # noqa
        # 还要检查浏览器是否真的安装
        import subprocess
        import shutil
        # 查找 chromium 可执行路径
        pw_browsers = os.path.expanduser("~/.cache/ms-playwright")
        if not os.path.isdir(pw_browsers):
            # 也可能在容器标准路径
            if shutil.which("chromium") or shutil.which("chromium-browser") or shutil.which("google-chrome"):
                _PLAYWRIGHT_AVAILABLE = True
                return True
            _PLAYWRIGHT_AVAILABLE = False
            log("[WARN] Playwright 浏览器未安装，跳过 Playwright 降级路径")
            return False
        _PLAYWRIGHT_AVAILABLE = True
        return True
    except ImportError:
        _PLAYWRIGHT_AVAILABLE = False
        log("[WARN] Playwright 未安装，跳过 Playwright 降级路径")
        return False

def _inject_playwright_cookies():
    """将 Playwright 浏览器中提取的 cookies 注入到 curl_cffi Session，降低触发滑块概率。"""
    global _PLAYWRIGHT_COOKIES
    with _PLAYWRIGHT_COOKIES_LOCK:
        cookies = list(_PLAYWRIGHT_COOKIES)
    if not cookies:
        return
    count = 0
    for c in cookies:
        name = c.get("name", "")
        value = c.get("value", "")
        domain = c.get("domain", "")
        path = c.get("path", "/")
        if name and value:
            try:
                _CAINIAO_SESSION.cookies.set(name, value, domain=domain, path=path)
                count += 1
            except Exception:
                pass
    if count > 0:
        log(f"[COOKIE] 已将 {count} 个 Playwright Cookie 注入 curl_cffi Session")


def _playwright_worker():
    """Playwright 工作线程：页面导航→监听 API 响应，自动降级解决滑块。"""
    try:
        import platform as _platform
        from playwright.sync_api import sync_playwright

        os.environ.pop("PLAYWRIGHT_CHROMIUM_USE_HEADLESS_SHELL", None)

        # --- 根据操作系统调整浏览器参数 ---
        system = _platform.system()  # Windows / Darwin / Linux
        launch_args = [
            "--disable-blink-features=AutomationControlled",
        ]
        if system == "Linux":
            launch_args.extend(["--no-sandbox", "--disable-dev-shm-usage"])

        # 从环境变量读取额外 Chromium 标志（Dockerfile 中设置，用于限制内存等）
        _extra_flags = os.environ.get("CHROMIUM_FLAGS", "").strip()
        if _extra_flags:
            launch_args.extend(_extra_flags.split())

        # --- 选择对应的 User-Agent（让网站看到"真实"的浏览器环境）---
        if system == "Darwin":
            ua = (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            )
        elif system == "Windows":
            ua = (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            )
        else:
            ua = (
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            )

        pw = sync_playwright().start()
        browser = pw.chromium.launch(
            headless=True,
            args=launch_args,
        )
        context = browser.new_context(
            user_agent=ua,
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
            viewport={"width": 1920, "height": 1080},
            device_scale_factor=1,
            java_script_enabled=True,
        )
        page = context.new_page()

        # 监听 API 响应（页面 JS 自动调用 detail.json，携带真实浏览器上下文）
        _captured = {}

        def _on_response(response):
            if response.status == 200 and 'detail.json' in response.url:
                try:
                    j = response.json()
                    if isinstance(j, dict):
                        _captured['data'] = j
                except Exception:
                    pass

        page.on('response', _on_response)

        log("[OK] Playwright 浏览器已就绪（页面导航模式，自动绕过 CAPTCHA）")

        _PLAYWRIGHT_READY_EVENT.set()

        def _check_captcha() -> bool:
            """检查当前页是否有滑块验证（NC 组件）"""
            try:
                return page.evaluate("""() => {
                    const el = document.querySelector('.nc-container, #nocaptcha, [class*="nc-container"]');
                    if (!el) return false;
                    const style = window.getComputedStyle(el);
                    return style.display !== 'none' && style.visibility !== 'hidden' && el.offsetHeight > 0;
                }""")
            except Exception:
                return False

        def _try_navigate(mail_no: str, lang: str, max_attempts: int = 2) -> Optional[dict]:
            """带滑块检测和自动重试的页面导航。超出尝试次数返回 None。"""
            for attempt in range(1, max_attempts + 1):
                _captured.clear()
                try:
                    page.goto(
                        f'https://global.cainiao.com/newDetail.htm?mailNoList={mail_no}&lang={lang}',
                        wait_until='load',
                        timeout=25_000,
                    )
                except Exception as nav_e:
                    log(f"[PW-NAV] 导航失败(第{attempt}次): {nav_e}")
                    continue

                # 等待轨迹元素渲染
                try:
                    page.wait_for_function(
                        "() => document.querySelector('[class*=\"detail\"] li, .track-item, .timeline-item, [class*=\"timeline\"]') !== null",
                        timeout=8_000,
                    )
                except Exception:
                    pass

                data = _captured.get('data')
                if data is not None:
                    return data

                # 无 API 数据 → 检查滑块
                if _check_captcha():
                    log(f"[PW-CAPTCHA] 第{attempt}次触发了滑块，尝试绕过（首页→详情页）…")
                    try:
                        page.goto("https://global.cainiao.com/", wait_until="load", timeout=15_000)
                        log("[PW-CAPTCHA] 首页已加载，等待 2s 建立会话…")
                        time.sleep(2)
                    except Exception:
                        pass
                    # 继续循环 → 下一次导航
                    continue

                if attempt < max_attempts:
                    log(f"[PW-RETRY] 页面无数据(第{attempt}次)，重试…")
                    time.sleep(1)
            return None

        while True:
            task_id, mail_no, lang = _PLAYWRIGHT_TASK_QUEUE.get()
            if mail_no is None:   # shutdown
                break
            try:
                data = _try_navigate(mail_no, lang, max_attempts=2)

                if data is None:
                    # 最后手段：创建全新页面再试一次
                    log("[PW-FALLBACK] 常规重试耗尽，尝试全新 Page…")
                    try:
                        page2 = context.new_page()
                        _captured.clear()
                        page2.on('response', _on_response)
                        page2.goto(
                            f'https://global.cainiao.com/newDetail.htm?mailNoList={mail_no}&lang={lang}',
                            wait_until='load',
                            timeout=25_000,
                        )
                        try:
                            page2.wait_for_function(
                                "() => document.querySelector('[class*=\"detail\"] li, .track-item, .timeline-item, [class*=\"timeline\"]') !== null",
                                timeout=8_000,
                            )
                        except Exception:
                            pass
                        data = _captured.get('data')
                        page2.close()
                    except Exception as p2_e:
                        log(f"[PW-FALLBACK] 全新 Page 也失败: {p2_e}")

                if data is None:
                    has_captcha = _check_captcha()
                    if has_captcha:
                        raise RuntimeError("触发了滑块验证，自动重试策略均未通过")
                    raise RuntimeError("页面加载完成但未捕获到 API 响应")
                if not isinstance(data, dict) or data.get('success') is False:
                    raise RuntimeError(f"API 返回异常: {json.dumps(data, ensure_ascii=False)[:200]}")

                # ✅ 成功获取数据后提取 Cookies 注入 curl_cffi Session
                try:
                    cookies = context.cookies()
                    with _PLAYWRIGHT_COOKIES_LOCK:
                        _PLAYWRIGHT_COOKIES.clear()
                        _PLAYWRIGHT_COOKIES.extend(cookies)
                    _inject_playwright_cookies()
                except Exception as ce:
                    log(f"[PW-COOKIE] Cookie 注入失败: {ce}")

            except Exception as e:
                data = RuntimeError(f"Playwright 查询失败: {e}")

            with _PLAYWRIGHT_LOCK:
                _PLAYWRIGHT_RESULTS[task_id] = data
                evt = _PLAYWRIGHT_EVENTS.pop(task_id, None)
            if evt:
                evt.set()

        context.close()
        browser.close()
        pw.stop()
    except Exception as e:
        log(f"[WARN] Playwright 工作线程初始化失败: {e}")
        _PLAYWRIGHT_READY_EVENT.set()   # 防止死等


def _ensure_playwright() -> bool:
    """确保 Playwright 工作线程已启动（线程安全）。"""
    global _PLAYWRIGHT_THREAD
    if _PLAYWRIGHT_READY_EVENT.is_set() and (
        _PLAYWRIGHT_THREAD and _PLAYWRIGHT_THREAD.is_alive()
    ):
        return True

    with _PLAYWRIGHT_LOCK:
        if _PLAYWRIGHT_READY_EVENT.is_set() and (
            _PLAYWRIGHT_THREAD and _PLAYWRIGHT_THREAD.is_alive()
        ):
            return True
        if _PLAYWRIGHT_THREAD and _PLAYWRIGHT_THREAD.is_alive():
            # 线程已启动，等待就绪
            return _PLAYWRIGHT_READY_EVENT.wait(timeout=15)

        try:
            from playwright.sync_api import sync_playwright  # noqa: check import
        except ImportError:
            log("[WARN] Playwright 未安装，降级为纯 requests 模式")
            log("[HINT] 如需降级绕过滑块，安装: pip install playwright && playwright install chromium")
            return False

        _PLAYWRIGHT_READY_EVENT.clear()
        _PLAYWRIGHT_THREAD = threading.Thread(target=_playwright_worker, daemon=True)
        _PLAYWRIGHT_THREAD.start()

    # 等待 worker 就绪（最长 15 秒）
    ready = _PLAYWRIGHT_READY_EVENT.wait(timeout=15)
    if not ready:
        log("[WARN] Playwright 工作线程启动超时，降级为 requests")
        return False
    return True


def _playwright_query(mail_no: str, lang: str = "zh-CN") -> dict:
    """通过 Playwright 工作线程查询菜鸟 API（线程安全）。"""
    if not _ensure_playwright():
        raise RuntimeError("Playwright 不可用")

    global _PLAYWRIGHT_COUNTER
    with _PLAYWRIGHT_LOCK:
        task_id = _PLAYWRIGHT_COUNTER
        _PLAYWRIGHT_COUNTER += 1
        evt = threading.Event()
        _PLAYWRIGHT_EVENTS[task_id] = evt

    _PLAYWRIGHT_TASK_QUEUE.put((task_id, mail_no, lang))

    # 等待结果（最长 30 秒）
    if not evt.wait(timeout=30):
        with _PLAYWRIGHT_LOCK:
            _PLAYWRIGHT_EVENTS.pop(task_id, None)
            _PLAYWRIGHT_RESULTS.pop(task_id, None)
        raise RuntimeError("Playwright 查询超时")

    with _PLAYWRIGHT_LOCK:
        result = _PLAYWRIGHT_RESULTS.pop(task_id, None)

    if result is None:
        raise RuntimeError("Playwright 查询无返回")
    if isinstance(result, Exception):
        raise result
    if not isinstance(result, dict) or result.get("success") is False:
        raise RuntimeError(f"Playwright 查询结果异常: {str(result)[:200]}")
    return result


# ============================================================
# 方案 B：curl_cffi Session（带自动轮换）
# ============================================================
# 使用全局 Session，但当检测到持续限流时自动创建新 Session 并轮换 User-Agent。
# 避免单个 Session 被上游标记后所有请求持续失败。

_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
]

# 连续限流计数器（达到阈值后触发 Session 轮换）
_session_rl_count = 0
_SESSION_RL_THRESHOLD = 2        # 连续 2 次限流就换 Session
_session_lock = threading.Lock()

def _build_session() -> requests.Session:
    """创建全新的 curl_cffi Session，带随机 User-Agent 并预热首页。"""
    s = requests.Session()
    ua = random.choice(_USER_AGENTS)
    s.headers.update({
        "User-Agent": ua,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Referer": "https://global.cainiao.com/",
        "Origin": "https://global.cainiao.com",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
        "DNT": "1",
        "Connection": "keep-alive",
    })
    # 预热：访问首页获取 Cookie
    try:
        s.get(
            "https://global.cainiao.com/",
            proxies={"http": "", "https": ""},
            verify=False,
            timeout=15,
        )
    except Exception:
        pass
    return s


def _maybe_rotate_session():
    """当连续限流超过阈值时，创建一个全新的 Session 替换全局变量。"""
    global _CAINIAO_SESSION, _session_rl_count
    with _session_lock:
        if _session_rl_count >= _SESSION_RL_THRESHOLD:
            old = _CAINIAO_SESSION
            _CAINIAO_SESSION = _build_session()
            _session_rl_count = 0
            log(f"[SESSION] 限流累计{_SESSION_RL_THRESHOLD}次，已轮换 Session（User-Agent: {_CAINIAO_SESSION.headers.get('User-Agent','')[:50]}…）")
            try:
                old.close()
            except Exception:
                pass
            return True
    return False


_CAINIAO_SESSION = _build_session()


def log(msg: str):
    """统一日志：控制台 + 可选文件，自动处理编码错误"""
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    # 安全输出到控制台（避免 GBK 编码问题导致崩溃）
    try:
        print(line)
    except UnicodeEncodeError:
        # 若 stdout 编码不支持部分字符，用 replace 避免崩溃
        try:
            out = sys.stdout.buffer
            out.write(line.encode(sys.stdout.encoding or "utf-8", errors="replace") + b"\n")
            out.flush()
        except Exception:
            pass  # 仍失败则静默丢弃
    if LOG_FILE:
        try:
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass


# ============================================================
# 结果缓存（LRU + TTL，避免短时间重复查询触发限流）
# ============================================================
_CACHE_MAX_SIZE = 200          # 最多缓存 200 条
_CACHE_TTL = 300               # 缓存有效期（秒），仅终态单号缓存 5 分钟
_QUERY_CACHE = {}              # key -> (expiry_timestamp, result)

# 终态状态码——到达这些状态后物流信息不会再变化，可安全缓存较长时间
# 非终态（运输中、清关中、派送中）一律不缓存，确保 WPS 每次查询都拿到最新状态
_TERMINAL_STATUS_CODES = frozenset({
    "DELIVERED",        # 妥投 / 用户已签收
    "FAILED",           # 投递失败 / 异常
    "RETURNED",         # 退件
    "CANCELLED",        # 已取消
    "LOST",             # 遗失
})
_QUERY_CACHE_ORDER = []        # 用于 LRU 淘汰的 key 列表


def _cache_get(key: str) -> Optional[dict]:
    """从缓存读取，命中则刷新 LRU 顺序。"""
    entry = _QUERY_CACHE.get(key)
    if entry is None:
        return None
    expiry, result = entry
    if time.time() > expiry:
        # 过期
        del _QUERY_CACHE[key]
        try:
            _QUERY_CACHE_ORDER.remove(key)
        except ValueError:
            pass
        return None
    # 刷新 LRU
    try:
        _QUERY_CACHE_ORDER.remove(key)
    except ValueError:
        pass
    _QUERY_CACHE_ORDER.append(key)
    return result


def _cache_set(key: str, result: dict, ttl: Optional[int] = None):
    """写入缓存，超出最大容量时淘汰最久未命中的条目。

    Args:
        key: 缓存键
        result: 缓存值
        ttl: 此条目的有效时长（秒）。None 表示使用全局 _CACHE_TTL。
             传入 0 或负数则跳过缓存。
    """
    if ttl is None:
        ttl = _CACHE_TTL
    if ttl <= 0:
        return  # 不缓存非终态数据，确保 WPS 每次查询拿到最新状态
    expiry = time.time() + ttl
    # 若已存在，先清理旧位置
    if key in _QUERY_CACHE:
        try:
            _QUERY_CACHE_ORDER.remove(key)
        except ValueError:
            pass
    # 淘汰
    while len(_QUERY_CACHE) >= _CACHE_MAX_SIZE:
        oldest = _QUERY_CACHE_ORDER.pop(0) if _QUERY_CACHE_ORDER else None
        if oldest and oldest in _QUERY_CACHE:
            del _QUERY_CACHE[oldest]
        else:
            break
    _QUERY_CACHE[key] = (expiry, result)
    _QUERY_CACHE_ORDER.append(key)


def _resolve_cache_ttl(api_response: dict) -> int:
    """根据 API 响应中所有包裹的物流状态决定缓存时长。

    终态（DELIVERED / FAILED / RETURNED / CANCELLED / LOST）不会再变化，
    可安全缓存满 _CACHE_TTL；非终态（运输中、清关中等）不缓存，
    确保 WPS 每次查询都拿到最新状态。

    Returns:
        _CACHE_TTL（全量缓存时长）或 0（不缓存）。
    """
    module = api_response.get("module")
    if not isinstance(module, list):
        return 0                     # 无有效数据，不缓存
    # 仅在所有包裹均为终态时缓存
    if all(
        isinstance(item, dict) and item.get("status", "") in _TERMINAL_STATUS_CODES
        for item in module
    ):
        return _CACHE_TTL
    return 0                         # 有任何一单仍在运输中 → 不缓存


def query_cainiao(mail_no: str, lang: str = "zh-CN") -> dict:
    """查询菜鸟物流轨迹，使用 curl_cffi 直连（TLS 指纹模拟浏览器）。"""
    # --- 缓存检查：同一运单号短时间内不重复查上游 ---
    cache_key = f"{mail_no}:{lang}"
    cached = _cache_get(cache_key)
    if cached is not None:
        log(f"[CACHE] {mail_no} 命中缓存，直接返回")
        _req_ctx.cache_hit = True
        return cached
    _req_ctx.cache_hit = False

    # 每次查询前随机延时，模仿人类的操作间隔
    delay = random.uniform(QUERY_DELAY_MIN, QUERY_DELAY_MAX)
    log(f"[SLEEP] {mail_no} 等待 {delay:.1f}s…")
    time.sleep(delay)

    # 注入之前 Playwright 提取的 cookies（如果有），降低滑块触发概率
    _inject_playwright_cookies()

    # --- 方案 A：curl_cffi（TLS 指纹模拟浏览器，速度快） ---
    proxies = {"http": "", "https": ""}
    max_retries = 3
    max_rate_limit = 5          # 批量场景给更多限流重试机会
    normal_attempt = 0
    rate_limit_attempt = 0
    last_exc = None

    try:
        while normal_attempt < max_retries:
            try:
                # 在每次重试前检查是否需要轮换 Session（避免持续限流）
                _maybe_rotate_session()

                resp = _CAINIAO_SESSION.get(
                    PUBLIC_API_URL,
                    params={"mailNos": mail_no, "lang": lang},
                    proxies=proxies,
                    verify=False,
                    timeout=20,
                )

                # 检查 Content-Type —— 如果不是 JSON 则大概率被拦截了
                ct = (resp.headers.get("Content-Type", "") or "").lower()
                if "json" not in ct and "javascript" not in ct:
                    normal_attempt += 1
                    if normal_attempt < max_retries:
                        delay = 2 ** normal_attempt  # 2s, 4s
                        log(f"[WARN] {mail_no} 返回 {ct or '非JSON'} (第{normal_attempt}次)，{delay}s 后重试…")
                        time.sleep(delay)
                        continue
                    else:
                        raise RuntimeError(f"上游返回 {ct or '非JSON'}：{resp.text[:300]}")

                body = resp.json()

                # 检查 API 业务状态
                if body.get("success") is False or body.get("module") is None:
                    err_text = json.dumps(body, ensure_ascii=False)
                    # 检测上游限流特征码（"被挤爆啦" / "RGV587_ERROR"）
                    is_rate_limited = "RGV587_ERROR" in err_text or "被挤爆" in err_text
                    if is_rate_limited:
                        rate_limit_attempt += 1
                        # 累计连续限流，准备触发 Session 轮换
                        with _session_lock:
                            _session_rl_count += 1
                        if rate_limit_attempt > max_rate_limit:
                            raise RuntimeError(f"上游持续限流，已重试{max_rate_limit}次仍失败")
                        log(f"[RLIMIT] {mail_no} 上游限流 {_rl_count_str(rate_limit_attempt)}，尝试解除封锁…")
                        # 提取 punish URL 并访问以解除封锁
                        punish_url = None
                        if isinstance(body.get("data"), dict):
                            punish_url = body["data"].get("url")
                        if punish_url:
                            try:
                                _CAINIAO_SESSION.get(
                                    punish_url,
                                    proxies=proxies,
                                    verify=False,
                                    timeout=10,
                                )
                                log(f"[UNLOCK] {mail_no} 已访问 punish URL 解除限流")
                            except Exception:
                                pass
                        # 再访问一次首页刷新 cookie
                        try:
                            _CAINIAO_SESSION.get(
                                "https://global.cainiao.com/",
                                proxies=proxies,
                                verify=False,
                                timeout=10,
                            )
                        except Exception:
                            pass
                        log(f"[REFRESH] {mail_no} Session 已刷新，再试一次")
                        # 限流后等待递增，随次数大幅增加退避时间
                        # 3s, 5s, 15s, 30s, 45s, 60s — 给上游充分冷却时间
                        rl_delays = [3, 5, 15, 30, 45, 60]
                        idx = min(rate_limit_attempt - 1, len(rl_delays) - 1)
                        rl_delay = rl_delays[idx]
                        # 加入随机抖动 ±25%，避免所有客户端同时重试
                        rl_delay *= random.uniform(0.75, 1.25)
                        log(f"[COOLDOWN] {mail_no} 等待 {rl_delay:.0f}s 冷却…")
                        time.sleep(rl_delay)
                        # 冷却后轮换 Session（如果已累计到阈值）
                        _maybe_rotate_session()
                        continue  # 不计入普通重试

                    normal_attempt += 1
                    if normal_attempt < max_retries:
                        delay = 2 ** normal_attempt
                        log(f"[WARN] {mail_no} API返回异常 (第{normal_attempt}次)，{delay}s 后重试…")
                        time.sleep(delay)
                        continue
                    raise RuntimeError(f"API返回异常: {err_text[:200]}")

                # 判断是否所有包裹都已达到终态，决定缓存时长
                _cache_set(cache_key, body, ttl=_resolve_cache_ttl(body))
                # 查询成功 → 重置连续限流计数器
                with _session_lock:
                    _session_rl_count = 0
                return body

            except json.JSONDecodeError as e:
                raw_preview = resp.text[:300].strip().replace("\r", "\\r").replace("\n", "\\n")
                normal_attempt += 1
                if normal_attempt < max_retries:
                    delay = 2 ** normal_attempt
                    log(f"[WARN] {mail_no} JSON解析失败 (第{normal_attempt}次)，{delay}s 后重试…")
                    time.sleep(delay)
                    continue
                raise RuntimeError(f"JSON解析失败，响应前300字符: {raw_preview}") from e

            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
                last_exc = e
                normal_attempt += 1
                if normal_attempt < max_retries:
                    delay = 2 ** normal_attempt
                    log(f"[WARN] {mail_no} 网络错误: {type(e).__name__} (第{normal_attempt}次)，{delay}s 后重试…")
                    time.sleep(delay)
                    continue
                raise RuntimeError(f"网络连接失败(重试{max_retries}次后): {last_exc}") from last_exc

            except requests.exceptions.HTTPError as e:
                # HTTP 4xx/5xx — 抛出，不重试（服务器明确拒绝）
                raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}") from e

            # 不应走到这里
            raise RuntimeError("curl_cffi 重试全部耗尽")
    except Exception as e:
        log(f"[WARN] curl_cffi 所有重试均失败: {e}")

    # --- 方案 B：Playwright（真实浏览器，绕过滑块验证） ---
    if USE_PLAYWRIGHT and _check_playwright_available():
        log(f"[FALLBACK] 尝试 Playwright 绕过 CAPTCHA 查询 {mail_no}…")
        try:
            raw = _playwright_query(mail_no, lang)
            if raw and raw.get("success") is not False:
                log(f"[OK] Playwright 成功查询 {mail_no}")
                _cache_set(cache_key, raw, ttl=_resolve_cache_ttl(raw))
                return raw
            log(f"[WARN] Playwright 返回异常: {json.dumps(raw, ensure_ascii=False)[:200]}")
        except Exception as pw_e:
            log(f"[FALLBACK] Playwright 也失败: {pw_e}")

    raise RuntimeError(f"所有查询方案均失败: {mail_no}")


def _rl_count_str(n: int) -> str:
    """返回限流次数的友好描述"""
    if n <= 2:
        return f"第{n}次"
    else:
        return f"第{n}次（持续中）"


def parse_simplified(raw: dict) -> list:
    """将原始 API 响应简化为 WPS 多维表可用的格式"""
    results = []
    for item in raw.get("module", []):
        mail_no = item.get("mailNo", "")
        status = item.get("statusDesc", "")
        status_code = item.get("status", "")
        origin = item.get("originCountry", "")
        dest = item.get("destCountry", "")
        latest = item.get("latestTrace", {}) or {}
        latest_time = latest.get("timeStr", "")
        latest_event = latest.get("standerdDesc", latest.get("desc", ""))
        # 轨迹事件列表
        detail_list = item.get("detailList", [])
        event_count = len(detail_list)
        # 提取事件摘要（最多20条，避免响应过大）
        events = []
        for evt in detail_list:
            events.append({
                "time": evt.get("timeStr", ""),
                "status": evt.get("standerdDesc", evt.get("desc", "")),
                "location": evt.get("location", evt.get("facilityName", "")),
            })

        results.append({
            "mailNo": mail_no,
            "status": status,
            "statusCode": status_code,
            "origin": origin,
            "dest": dest,
            "latestTime": latest_time,
            "latestEvent": latest_event,
            "eventCount": event_count,
            "events": events,
        })
    return results


class QueryHandler(BaseHTTPRequestHandler):
    """HTTP 请求处理"""

    def do_GET(self):
        try:
            self._handle_get()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass  # 客户端断开
        except Exception:
            # 兜底：避免未捕获异常导致整个服务器退出
            log(f"[FATAL] do_GET 未捕获异常: {sys.exc_info()[1]}")
            try:
                self._write_json(500, {"error": "内部错误"})
            except Exception:
                pass

    def _handle_get(self):
        parsed = urlparse(self.path)
        req_start = time.time()

        if parsed.path == "/query":
            params = parse_qs(parsed.query)
            mail_nos = params.get("mailNo", [])
            lang = params.get("lang", ["zh-CN"])[0]

            if not mail_nos:
                self._write_json(400, {"error": "缺少参数 ?mailNo=xxx"})
                log(f"[REQ] 400 缺少参数")
                return

            # 支持批量，用逗号分隔
            mail_no_list = [m.strip() for m in mail_nos[0].split(",") if m.strip()]
            mail_no_str = ",".join(mail_no_list)
            log(f"[REQ] → {', '.join(mail_no_list)} (共{len(mail_no_list)}单)")

            try:
                raw = query_cainiao(mail_no_str, lang)
                simplified = parse_simplified(raw)
                elapsed = time.time() - req_start
                log(f"[OK] ✓ {len(mail_no_list)}单 耗时{elapsed:.1f}s → {len(simplified)}结果")
                # 补齐缺失的单号（API 不会为无效单号返回 module 项）
                returned_mail_nos = {r["mailNo"] for r in simplified}
                for mn in mail_no_list:
                    if mn not in returned_mail_nos:
                        simplified.append({
                            "mailNo": mn,
                            "status": "无数据",
                            "statusCode": "",
                            "origin": "",
                            "dest": "",
                            "latestTime": "",
                            "latestEvent": "",
                            "eventCount": 0,
                            "error": "",
                        })
                # 异步写入 Google Sheets（如果已配置）
                _write_results_to_sheet(simplified)
                self._write_json(200, {"code": 0, "data": simplified})
            except Exception as e:
                elapsed = time.time() - req_start
                log(f"[ERR] ✗ {mail_no_str} 耗时{elapsed:.1f}s → {e}")
                # 全部标记为失败
                err_results = []
                for mn in mail_no_list:
                    err_results.append({
                        "mailNo": mn,
                        "status": "查询失败",
                        "statusCode": "",
                        "origin": "",
                        "dest": "",
                        "latestTime": "",
                        "latestEvent": "",
                        "eventCount": 0,
                        "error": str(e),
                    })
                self._write_json(200, {"code": 0, "data": err_results})

        elif parsed.path == "/":
            self._write_json(200, {
                "server": "菜鸟物流查询本地服务",
                "version": "1.1",
                "usage": "GET /query?mailNo=LP00812637173551",
                "status": "running",
            })

        elif parsed.path == "/health":
            self._write_json(200, {"status": "ok", "time": datetime.now().isoformat()})

        else:
            self._write_json(404, {"error": "未知路径"})

    def do_OPTIONS(self):
        """CORS preflight"""
        try:
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.end_headers()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass

    def _write_json(self, status: int, obj: dict):
        """发送 JSON 响应（容客户端断开）"""
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Content-Length", str(len(body)))
            # 缓存状态
            cache_hit = getattr(_req_ctx, "cache_hit", None)
            if cache_hit is True:
                self.send_header("X-Cache", "HIT")
            elif cache_hit is False:
                self.send_header("X-Cache", "MISS")
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass  # 客户端已断开，无需处理
        finally:
            self.close_connection = True  # 不保持连接，避免残留请求阻塞

    def log_message(self, format, *args):
        """抑制默认日志，使用自定义日志"""
        log(f"{self.client_address[0]} - {format % args}")


def main():
    import argparse
    parser = argparse.ArgumentParser(
        description="菜鸟物流查询 HTTP 服务 — WPS 多维表调用端",
    )
    # Render 云部署会自动设置 PORT 环境变量
    default_port = int(os.environ.get("PORT", 58080))
    default_host = "0.0.0.0" if "PORT" in os.environ else "127.0.0.1"
    parser.add_argument("--port", type=int, default=default_port,
                        help="监听端口（默认 58080，Cloud Run 自动取 PORT 环境变量）")
    parser.add_argument("--host", default=default_host,
                        help="监听地址（Cloud Run 默认 0.0.0.0，本地默认 127.0.0.1）")
    parser.add_argument("--log-file", default=None, help="日志文件路径（可选）")
    args = parser.parse_args()

    global LOG_FILE
    LOG_FILE = args.log_file

    # 启动时检测 Playwright 是否已安装
    import platform as _platform
    _sys = _platform.system()
    _pw_available = False
    try:
        from playwright.sync_api import sync_playwright  # noqa
        _pw_available = True
    except ImportError:
        pass

    server = ThreadingHTTPServer((args.host, args.port), QueryHandler)
    print(f"[CAINIAO] 菜鸟物流查询服务已启动")
    print(f"[INFO] 操作系统: {_sys}")
    print(f"[INFO] Playwright: {'✓ 已安装' if _pw_available else '✗ 未安装（仅使用 curl_cffi 直连，遇见滑块验证会报错）'}")
    if not _pw_available:
        print(f"[HINT] 如需绕过滑块验证，运行: pip install playwright && playwright install chromium")
    print(f"[INFO] http://{args.host}:{args.port}")
    print(f"[INFO] 查询示例: http://localhost:{args.port}/query?mailNo=LP00812637173551")
    if "PORT" in os.environ:
        print(f"[INFO] 云部署模式（Cloud Run），服务 URL 以分配为准")
    print(f"[INFO] 按 Ctrl+C 停止")
    print()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[CAINIAO] 服务已停止")
        server.server_close()


if __name__ == "__main__":
    main()
