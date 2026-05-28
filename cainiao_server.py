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
import ssl
import sys
import os
import time
import random
import threading
import queue
import urllib.request
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, quote
from datetime import datetime
from typing import Optional
from concurrent.futures import ThreadPoolExecutor, as_completed

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

# 全局冷却：当短时间内失败太多时，强制所有查询等待
_GLOBAL_COOLDOWN = 0.0          # 下次查询前需等待的时间戳（time.time）
_GLOBAL_COOLDOWN_LOCK = threading.Lock()
_GLOBAL_FAIL_WINDOW = []        # 最近失败时间戳（用于滑动窗口计数）
_GLOBAL_FAIL_WINDOW_LOCK = threading.Lock()
_GLOBAL_FAIL_THRESHOLD = 2      # 10 秒内超过 2 次失败 → 触发全局冷却
_GLOBAL_COOLDOWN_SECONDS = 30   # 触发后冷却 30 秒

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

# ============================================================
# 全局限流频率追踪（滑动窗口，防止短时间大量限流）
# ============================================================
_GLOBAL_RL_TIMESTAMPS = []      # 限流发生的时间戳（用于统计最近 60s 限流次数）
_GLOBAL_RL_LOCK = threading.Lock()
_GLOBAL_RL_WINDOW_SECONDS = 60  # 统计窗口
_GLOBAL_RL_WINDOW_THRESHOLD = 2 # 60s 内超过 2 次限流 → 额外减速（原3，更早减速）

def _record_rate_limit():
    """记录一次限流事件，返回最近 _GLOBAL_RL_WINDOW_SECONDS 秒内的限流次数。"""
    now = time.time()
    with _GLOBAL_RL_LOCK:
        _GLOBAL_RL_TIMESTAMPS.append(now)
        # 只保留窗口内的
        cutoff = now - _GLOBAL_RL_WINDOW_SECONDS
        _GLOBAL_RL_TIMESTAMPS[:] = [t for t in _GLOBAL_RL_TIMESTAMPS if t > cutoff]
        count = len(_GLOBAL_RL_TIMESTAMPS)
    return count

def _check_global_rl_pressure() -> float:
    """根据最近限流频率计算额外延迟（秒）。"""
    now = time.time()
    with _GLOBAL_RL_LOCK:
        cutoff = now - _GLOBAL_RL_WINDOW_SECONDS
        recent = [t for t in _GLOBAL_RL_TIMESTAMPS if t > cutoff]
        count = len(recent)
    if count >= _GLOBAL_RL_WINDOW_THRESHOLD:
        # 超过阈值 → 每多1次多等1秒，上限 5s
        extra = min(count - _GLOBAL_RL_WINDOW_THRESHOLD + 1, 5.0)
        return extra
    return 0.0


# ============================================================
# 上游 API 调用间隔控制 — 避免并发请求触发流量特征检测
# ============================================================
_UPSTREAM_LAST_CALL_TIME = 0.0
_UPSTREAM_PACE_LOCK = threading.Lock()
_UPSTREAM_MIN_INTERVAL = 2.0    # 同一来源相邻上游调用至少间隔 2.0 秒
# 连续查询计数（超过阈值额外减速）
_CONSECUTIVE_QUERY_COUNT = 0
_CONSECUTIVE_QUERY_COUNT_LOCK = threading.Lock()
_ADAPTIVE_PACING_THRESHOLD = 8  # 连续查 8 单后自动增加额外延迟


def _pace_upstream_call():
    """执行上游 API 调用前等待，确保与上一次调用间隔不低于 _UPSTREAM_MIN_INTERVAL。"""
    global _UPSTREAM_LAST_CALL_TIME
    now = time.time()
    with _UPSTREAM_PACE_LOCK:
        if _UPSTREAM_LAST_CALL_TIME > 0:
            elapsed = now - _UPSTREAM_LAST_CALL_TIME
            if elapsed < _UPSTREAM_MIN_INTERVAL:
                wait = _UPSTREAM_MIN_INTERVAL - elapsed
                time.sleep(wait)
                now = time.time()
        _UPSTREAM_LAST_CALL_TIME = now


def _adaptive_delay():
    """连续查询过多时自动增加额外延迟，避免被上游识别为批量爬虫。"""
    global _CONSECUTIVE_QUERY_COUNT
    with _CONSECUTIVE_QUERY_COUNT_LOCK:
        count = _CONSECUTIVE_QUERY_COUNT
    if count >= _ADAPTIVE_PACING_THRESHOLD:
        extra = min((count - _ADAPTIVE_PACING_THRESHOLD + 1) * 0.5, 3.0)
        log(f"[PACE] 连续查询 {count} 单，自适应延迟 +{extra:.1f}s")
        time.sleep(extra)


def _inc_consecutive_query():
    """递增连续查询计数。"""
    global _CONSECUTIVE_QUERY_COUNT
    with _CONSECUTIVE_QUERY_COUNT_LOCK:
        _CONSECUTIVE_QUERY_COUNT += 1


def _reset_consecutive_query():
    """重置连续查询计数（查询空闲时调用）。"""
    global _CONSECUTIVE_QUERY_COUNT
    with _CONSECUTIVE_QUERY_COUNT_LOCK:
        _CONSECUTIVE_QUERY_COUNT = 0


# ============================================================
# Session 主动轮换配置
# ============================================================
_SESSION_QUERY_COUNT = 0          # 当前 Session 已查询次数（达到上限主动轮换）
_SESSION_QUERY_COUNT_LOCK = threading.Lock()
_SESSION_MAX_QUERIES = int(os.environ.get("SESSION_MAX_QUERIES", "2"))  # 每个 Session 最多查2次后轮换（严防限流）
_PRE_REQUEST_JITTER = (0.3, 1.2)  # 请求前随机延迟（秒），带 jitter 打散请求节奏

# 菜鸟公开 API
PUBLIC_API_URL = "https://global.cainiao.com/global/detail.json"

# 日志输出（WPS 请求时需要静默运行，日志写入文件可选）
# 云端部署默认不写文件，仅 stdout（Render 自动捕获日志）
# 如需本地调试文件日志，设置环境变量 LOG_FILE=1 或在 `--log-file` 参数指定路径
LOG_FILE = os.environ.get("LOG_FILE", "")  # 留空 = 仅 stdout；非空路径 = 写入文件

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
_PLAYWRIGHT_NUM_WORKERS = int(os.environ.get("PLAYWRIGHT_WORKERS", 3))  # 并发 Worker 数量（Render 免费计划 512MB 建议 1）
_PLAYWRIGHT_WORKER_THREADS: list[threading.Thread] = []
_PLAYWRIGHT_READY_EVENT = threading.Event()

# Playwright → curl_cffi Cookie 注入
_PLAYWRIGHT_COOKIES = []
_PLAYWRIGHT_COOKIES_LOCK = threading.Lock()
# Playwright 是否可用（只检测一次，避免重复启动失败）
_PLAYWRIGHT_AVAILABLE: Optional[bool] = None

def _check_playwright_available() -> bool:
    """快速检测 Playwright 是否可用（只检测一次并缓存结果）。

    兼容自定义 PLAYWRIGHT_BROWSERS_PATH（Render Dockerfile 设为 /app/pw-browsers）。
    """
    global _PLAYWRIGHT_AVAILABLE
    if _PLAYWRIGHT_AVAILABLE is not None:
        return _PLAYWRIGHT_AVAILABLE
    try:
        import playwright.sync_api  # noqa
        import shutil
        # 查找可能的 Playwright 浏览器目录，优先使用环境变量
        search_paths = set()
        env_path = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "")
        if env_path:
            search_paths.add(env_path)
        search_paths.add(os.path.expanduser("~/.cache/ms-playwright"))
        # Windows: %USERPROFILE%\AppData\Local\ms-playwright
        local_app_data = os.environ.get("LOCALAPPDATA", "")
        if local_app_data:
            search_paths.add(os.path.join(local_app_data, "ms-playwright"))

        for p in search_paths:
            if not os.path.isdir(p):
                continue
            # 检查路径下是否含有 chromium 子目录（如 chromium-XXXX）
            try:
                for entry in os.listdir(p):
                    if "chrom" in entry.lower() and os.path.isdir(os.path.join(p, entry)):
                        _PLAYWRIGHT_AVAILABLE = True
                        return True
            except PermissionError:
                continue

        # 也检查系统命令（备用）
        if shutil.which("chromium") or shutil.which("chromium-browser") or shutil.which("google-chrome"):
            _PLAYWRIGHT_AVAILABLE = True
            return True

        _PLAYWRIGHT_AVAILABLE = False
        log("[WARN] Playwright 浏览器未安装，跳过 Playwright 降级路径")
        return False
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


def _playwright_worker(worker_id: int):
    """Playwright 工作线程 #id：独立浏览器+页面，多 Worker 并发。

    每个 Worker 拥有独立的浏览器实例、上下文、页面和事件监听器，
    因此完全线程安全，共享同一任务队列 _PLAYWRIGHT_TASK_QUEUE。
    """
    try:
        import platform as _platform
        from playwright.sync_api import sync_playwright

        os.environ.pop("PLAYWRIGHT_CHROMIUM_USE_HEADLESS_SHELL", None)

        # --- 根据操作系统调整浏览器参数 ---
        system = _platform.system()
        launch_args = [
            "--disable-blink-features=AutomationControlled",
        ]
        if system == "Linux":
            launch_args.extend(["--no-sandbox", "--disable-dev-shm-usage"])

        _extra_flags = os.environ.get("CHROMIUM_FLAGS", "").strip()
        if _extra_flags:
            launch_args.extend(_extra_flags.split())

        # --- 选择对应的 User-Agent ---
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

        # 获取全局代理管理器，用于 Playwright 浏览器代理
        _pw_proxy_mgr = _get_proxy_manager()
        _pw_current_proxy = _pw_proxy_mgr.get_proxy() if _pw_proxy_mgr.is_active else None
        _pw_proxy_cfg = _pw_proxy_mgr.to_playwright_proxy(_pw_current_proxy) if _pw_current_proxy else None
        _pw_consecutive_failures = 0
        _pw_queries_since_proxy_rotate = 0
        _pw_max_queries_per_proxy = random.randint(8, 15)  # 每个代理最多查询 N 次后轮换
        browser = pw.chromium.launch(
            headless=True,
            args=launch_args,
            proxy=_pw_proxy_cfg,
        )
        _pw_browser_proxy = _pw_current_proxy  # track which proxy browser was launched with
        context = browser.new_context(
            user_agent=ua,
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
            viewport={"width": 1920, "height": 1080},
            device_scale_factor=1,
            java_script_enabled=True,
        )
        page = context.new_page()
        page.set_default_timeout(10_000)

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

        # ── 人类行为模拟 ──────────────────────────────
        def _pw_human_delay(min_ms: int = 200, max_ms: int = 1500):
            """随机等待，模拟人类操作间隔。"""
            time.sleep(random.uniform(min_ms / 1000, max_ms / 1000))

        def _pw_random_mouse_move():
            """在页面上随机移动鼠标，模拟真人浏览。"""
            try:
                vp = page.viewport_size
                if vp:
                    for _ in range(random.randint(1, 3)):
                        x = random.randint(0, vp["width"])
                        y = random.randint(0, vp["height"])
                        page.mouse.move(x, y, steps=random.randint(5, 15))
                        _pw_human_delay(50, 200)
            except Exception:
                pass

        def _pw_random_scroll():
            """模拟随机的页面滚动行为。"""
            try:
                vp = page.viewport_size
                if vp:
                    max_scroll = vp["height"] * random.randint(1, 3)
                    steps = random.randint(3, 8)
                    for _ in range(steps):
                        delta = random.randint(50, 300)
                        page.evaluate(f"window.scrollBy(0, {delta})")
                        _pw_human_delay(100, 400)
                    # 滚动回顶部
                    page.evaluate("window.scrollTo(0, 0)")
                    _pw_human_delay(100, 200)
            except Exception:
                pass

        def _pw_human_simulate():
            """综合人类行为模拟：鼠标移动 + 滚动 + 延迟。"""
            _pw_random_mouse_move()
            if random.random() < 0.4:  # 40% 概率滚动页面
                _pw_random_scroll()
            _pw_human_delay(300, 1000)

        def _pw_rotate_browser_proxy():
            """关闭当前浏览器，用新代理重新启动浏览器。"""
            nonlocal _pw_current_proxy, _pw_proxy_cfg, _pw_browser_proxy
            nonlocal _pw_consecutive_failures, _pw_queries_since_proxy_rotate
            try:
                context.close()
                browser.close()
                pw.stop()
            except Exception:
                pass
            _pw_current_proxy = _pw_proxy_mgr.get_proxy() if _pw_proxy_mgr.is_active else None
            _pw_proxy_cfg = _pw_proxy_mgr.to_playwright_proxy(_pw_current_proxy) if _pw_current_proxy else None
            _pw_browser_proxy = _pw_current_proxy
            _pw_consecutive_failures = 0
            _pw_queries_since_proxy_rotate = 0
            # 重新启动
            pw = sync_playwright().start()
            browser = pw.chromium.launch(
                headless=True,
                args=launch_args,
                proxy=_pw_proxy_cfg,
            )
            # 重新创建 context + page
            context = browser.new_context(
                user_agent=ua,
                locale="zh-CN",
                timezone_id="Asia/Shanghai",
                viewport={"width": 1920, "height": 1080},
                device_scale_factor=1,
                java_script_enabled=True,
            )
            page = context.new_page()
            page.set_default_timeout(10_000)
            _captured.clear()
            page.on('response', _on_response)
            log(f"[PW-{worker_id}] 代理已轮换至 {_pw_current_proxy or '直连'}，浏览器已重启")

        log(f"[PW-{worker_id}] Playwright Worker #{worker_id} 已就绪")

        with _PLAYWRIGHT_LOCK:
            _PLAYWRIGHT_READY_EVENT.set()  # signal at least one worker ready

        def _check_captcha() -> bool:
            try:
                return page.evaluate("""() => {
                    const el = document.querySelector('.nc-container, #nocaptcha, [class*="nc-container"]');
                    if (!el) return false;
                    const style = window.getComputedStyle(el);
                    return style.display !== 'none' && style.visibility !== 'hidden' && el.offsetHeight > 0;
                }""")
            except Exception:
                return False

        def _ensure_cainiao_origin():
            try:
                origin = page.evaluate("window.location.origin")
                if origin and "global.cainiao.com" in origin:
                    _pw_human_simulate()
                    return True
            except Exception:
                pass
            try:
                _pw_human_simulate()
                page.goto("https://global.cainiao.com/", wait_until="domcontentloaded", timeout=15_000)
                _pw_human_delay(500, 1500)
                return True
            except Exception:
                return False

        def _try_fast_api(mail_no: str, lang: str):
            if not _ensure_cainiao_origin():
                return None
            _pw_human_delay(300, 1200)
            try:
                result = page.evaluate(f"""
                    async () => {{
                        const url = 'https://global.cainiao.com/global/detail.json?mailNos={mail_no}&lang={lang}';
                        try {{
                            const resp = await fetch(url, {{
                                credentials: 'include',
                                headers: {{ 'Accept': 'application/json, text/plain, */*' }}
                            }});
                            if (!resp.ok) return null;
                            return await resp.json();
                        }} catch (e) {{
                            return null;
                        }}
                    }}
                """)
                if isinstance(result, dict) and result.get("success") is not False:
                    _pw_human_delay(200, 600)
                    return result
            except Exception as e:
                log(f"[PW-{worker_id}] 快速 API 调用失败: {e}")
            return None

        def _try_navigate(mail_no: str, lang: str, max_attempts: int = 2):
            """导航到菜鸟详情页并等待 API 响应。

            先执行人类行为模拟（鼠标移动、随机延迟），再直接导航到详情页。
            首页搜索框方案虽更像真人但太慢，不适合批量场景；
            此方法更快（5-10s），配合随机延迟和代理轮换已足够绕过基本限流。
            """
            for attempt in range(1, max_attempts + 1):
                _captured.clear()
                # 模拟人类行为后再导航
                _pw_human_simulate()
                _pw_human_delay(500, 2000)

                try:
                    page.goto(
                        f'https://global.cainiao.com/newDetail.htm?mailNoList={mail_no}&lang={lang}',
                        wait_until='domcontentloaded',
                        timeout=20_000,
                    )
                except Exception as nav_e:
                    log(f"[PW-{worker_id}] 导航失败(第{attempt}次): {nav_e}")
                    continue

                data = _captured.get('data')
                if data is None:
                    for _ in range(30):
                        time.sleep(0.2)
                        data = _captured.get('data')
                        if data is not None:
                            break

                if data is not None:
                    return data

                # ── 滑块检测 ──
                if _check_captcha():
                    log(f"[PW-{worker_id}] 第{attempt}次触发了滑块，尝试绕过…")
                    _pw_consecutive_failures += 1
                    try:
                        page.goto("https://global.cainiao.com/", wait_until="domcontentloaded", timeout=12_000)
                        time.sleep(2)
                    except Exception:
                        pass
                    continue

                if attempt < max_attempts:
                    log(f"[PW-{worker_id}] 页面无数据(第{attempt}次)，重试…")
                    time.sleep(1)
            return None

        while True:
            # 定期轮换代理：每个代理查询若干次后自动轮换
            _pw_queries_since_proxy_rotate += 1
            if _pw_proxy_mgr.is_active and _pw_queries_since_proxy_rotate >= _pw_max_queries_per_proxy:
                log(f"[PW-{worker_id}] 达到最大查询次数({_pw_max_queries_per_proxy})，轮换代理…")
                _pw_rotate_browser_proxy()
                _pw_max_queries_per_proxy = random.randint(8, 15)

            # 检查全局冷却
            _check_global_cooldown()

            task_id, mail_no, lang = _PLAYWRIGHT_TASK_QUEUE.get()
            if mail_no is None:
                break
            try:
                # ── 首选：浏览器内 fetch API（快，3-5s） ──
                data = _try_fast_api(mail_no, lang)

                # ── 降级：导航到详情页模拟人类（较慢，5-10s） ──
                if data is None:
                    data = _try_navigate(mail_no, lang, max_attempts=2)

                if data is None:
                    log(f"[PW-{worker_id}] 常规重试耗尽，尝试全新 Page…")
                    _pw_consecutive_failures += 1
                    try:
                        page2 = context.new_page()
                        _captured.clear()
                        page2.on('response', _on_response)
                        page2.goto(
                            f'https://global.cainiao.com/newDetail.htm?mailNoList={mail_no}&lang={lang}',
                            wait_until='domcontentloaded',
                            timeout=20_000,
                        )
                        data2 = _captured.get('data')
                        if data2 is None:
                            for _ in range(30):
                                time.sleep(0.2)
                                data2 = _captured.get('data')
                                if data2 is not None:
                                    break
                        data = data2
                        page2.close()
                    except Exception as p2_e:
                        log(f"[PW-{worker_id}] 全新 Page 也失败: {p2_e}")

                if data is None:
                    has_captcha = _check_captcha()
                    if has_captcha:
                        raise RuntimeError("触发了滑块验证，自动重试策略均未通过")
                    raise RuntimeError("页面加载完成但未捕获到 API 响应")
                if not isinstance(data, dict) or data.get('success') is False:
                    raise RuntimeError(f"API 返回异常: {json.dumps(data, ensure_ascii=False)[:200]}")

                # 成功：重置失败计数、上报成功
                _pw_consecutive_failures = 0
                _record_success()

                # 提取 Cookies 注入 curl_cffi Session
                try:
                    cookies = context.cookies()
                    with _PLAYWRIGHT_COOKIES_LOCK:
                        _PLAYWRIGHT_COOKIES.clear()
                        _PLAYWRIGHT_COOKIES.extend(cookies)
                    _inject_playwright_cookies()
                except Exception as ce:
                    log(f"[PW-{worker_id}] Cookie 提取失败: {ce}")

            except Exception as e:
                _pw_consecutive_failures += 1
                _record_failure()
                log(f"[PW-{worker_id}] 查询失败 ({mail_no}): {e}")
                # 连续失败达到阈值 → 轮换代理 / 重启浏览器
                if _pw_consecutive_failures >= 2 and _pw_proxy_mgr.is_active:
                    if _pw_current_proxy:
                        _pw_proxy_mgr.mark_failed(_pw_current_proxy)
                    _pw_rotate_browser_proxy()
                data = RuntimeError(f"Playwright 查询失败: {e}")

            with _PLAYWRIGHT_LOCK:
                _PLAYWRIGHT_RESULTS[task_id] = data
                evt = _PLAYWRIGHT_EVENTS.pop(task_id, None)
            if evt:
                evt.set()

        context.close()
        browser.close()
        pw.stop()
        log(f"[PW-{worker_id}] Worker #{worker_id} 已退出")
    except Exception as e:
        log(f"[PW-{worker_id}] Worker #{worker_id} 初始化失败: {e}")
        with _PLAYWRIGHT_LOCK:
            _PLAYWRIGHT_READY_EVENT.set()  # prevent deadlock


def _ensure_playwright() -> bool:
    """确保 Playwright 工作线程池已启动（线程安全）。"""
    global _PLAYWRIGHT_WORKER_THREADS
    if _PLAYWRIGHT_READY_EVENT.is_set() and any(
        t.is_alive() for t in _PLAYWRIGHT_WORKER_THREADS
    ):
        return True

    with _PLAYWRIGHT_LOCK:
        if _PLAYWRIGHT_READY_EVENT.is_set() and any(
            t.is_alive() for t in _PLAYWRIGHT_WORKER_THREADS
        ):
            return True

        alive = [t for t in _PLAYWRIGHT_WORKER_THREADS if t.is_alive()]
        needed = _PLAYWRIGHT_NUM_WORKERS - len(alive)
        if needed <= 0:
            return _PLAYWRIGHT_READY_EVENT.wait(timeout=15)

        try:
            from playwright.sync_api import sync_playwright  # noqa: check import
        except ImportError:
            log("[WARN] Playwright 未安装，降级为纯 requests 模式")
            return False

        _PLAYWRIGHT_READY_EVENT.clear()

        for i in range(needed):
            wid = len(alive) + i
            t = threading.Thread(target=_playwright_worker, args=(wid,), daemon=True)
            t.start()
            _PLAYWRIGHT_WORKER_THREADS.append(t)
        log(f"[PLAYWRIGHT] 启动 {needed} 个 Worker（共 {len(_PLAYWRIGHT_WORKER_THREADS)} 个）")

    # 等待至少一个 worker 就绪（最长 20 秒）
    ready = _PLAYWRIGHT_READY_EVENT.wait(timeout=20)
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

    # 等待结果（最长 20 秒）
    if not evt.wait(timeout=20):
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
    # Chrome 125 Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    # Chrome 126 Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    # Chrome 124 Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    # Chrome 125 macOS
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    # Chrome 126 macOS
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    # Edge 125 Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36 Edg/125.0.0.0",
    # Firefox 127 Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:127.0) Gecko/20100101 Firefox/127.0",
    # Chrome 125 Linux
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    # Safari 17 macOS
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
    # Chrome 131 Windows (latest)
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    # Firefox 131 Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:131.0) Gecko/20100101 Firefox/131.0",
    # Edge 131 Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0",
]

# 用于 curl_cffi 的 impersonate 参数（与 User-Agent 一一对应）
_IMPERSONATE_OPTIONS = [
    "chrome120",    # Chrome 125 Win
    "chrome124",    # Chrome 126 Win — 用124模拟126
    "chrome120",    # Chrome 124 Win
    "chrome120",    # Chrome 125 macOS
    "chrome124",    # Chrome 126 macOS
    "chrome120",    # Edge 125 Win
    "firefox120",   # Firefox 127 Win — 指纹差异大
    "chrome120",    # Chrome 125 Linux
    "safari17_0",   # Safari 17 macOS — 完全不同指纹
    "chrome131",    # Chrome 131 Win — 最新指纹
    "firefox131",   # Firefox 131 Win — 最新Firefox
    "chrome120",    # Edge 131 Win
]

# 连续限流计数器（达到阈值后触发 Session 轮换）
_session_rl_count = 0
_SESSION_RL_THRESHOLD = 2        # 连续 2 次限流就换 Session
_session_lock = threading.Lock()

def _build_session() -> requests.Session:
    """创建全新的 curl_cffi Session，带随机 User-Agent + 随机 TLS 指纹。"""
    idx = random.randint(0, len(_USER_AGENTS) - 1)
    ua = _USER_AGENTS[idx]
    imp = _IMPERSONATE_OPTIONS[idx] if idx < len(_IMPERSONATE_OPTIONS) else "chrome120"
    s = requests.Session(impersonate=imp)
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
    return s


def _maybe_rotate_session(force: bool = False) -> bool:
    """当连续限流超过阈值，或当前 Session 查询次数达到上限时，轮换 Session。

    Args:
        force: 若为 True 则强制轮换（用于已确认限流时立即换 Session）。
    """
    global _CAINIAO_SESSION, _session_rl_count, _SESSION_QUERY_COUNT
    with _session_lock:
        should_rotate = force

        # 主动轮换：当前 Session 查询次数已达上限
        if not should_rotate and _SESSION_QUERY_COUNT >= _SESSION_MAX_QUERIES:
            log(f"[SESSION] 当前 Session 已查 {_SESSION_QUERY_COUNT} 次（上限{_SESSION_MAX_QUERIES}），主动轮换")
            should_rotate = True

        # 被动轮换：连续限流
        if not should_rotate and _session_rl_count >= _SESSION_RL_THRESHOLD:
            log(f"[SESSION] 限流累计{_SESSION_RL_THRESHOLD}次，触发轮换")
            should_rotate = True

        if should_rotate:
            old = _CAINIAO_SESSION
            _CAINIAO_SESSION = _build_session()
            _session_rl_count = 0
            _SESSION_QUERY_COUNT = 0
            log(f"[SESSION] 已轮换（User-Agent: {_CAINIAO_SESSION.headers.get('User-Agent','')[:50]}…）")
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
_NON_TERMINAL_CACHE_TTL = 60  # 非终态缓存有效期（秒），运输中单号缓存1分钟避免WPS重复查
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


# ============================================================
# 方案 D：ProxyManager 代理轮询（绕过 IP 限流）
# ============================================================
# 环境变量配置：
#   PROXY_LIST=http://user:pass@ip1:port,http://ip2:port,...    # 逗号分隔
#   PROXY_MODE=rotate|direct              # rotate=轮询, direct=直连(默认)
#   PROXY_CHECK_URL=https://global.cainiao.com/  # 用来测试代理存活
#
# 内置免费代理源（自动获取，仅当 PROXY_LIST 未设且 PROXY_MODE 非 direct 时生效）：
#   proxy list download  |  每 5 分钟后台刷新一次
# ============================================================

_PROXY_MANAGER_READY = False
_PROXY_MANAGER = None
_PROXY_MANAGER_LOCK = threading.Lock()

# 免费代理源（多个源提高获取成功率）
_FREE_PROXY_SOURCES = [
    # ProxyScrape（最稳定，纯 HTTP 列表）
    "https://api.proxyscrape.com/v2/?request=displayproxies&protocol=http&timeout=5000&country=all&ssl=all&anonymity=all",
    # 备用 1
    "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt",
    # 备用 2
    "https://www.proxy-list.download/api/v1/get?type=http",
]


class ProxyManager:
    """代理轮询管理器 — 支持静态列表 + 免费代理自动获取 + 失败熔断"""

    def __init__(self):
        self._proxies: list[str] = []
        self._index = 0
        self._lock = threading.Lock()
        self._failed: set[str] = set()
        self._last_fetch_time = 0
        self._fetch_interval = 300  # 5 分钟刷新免费代理
        self._mode = os.environ.get("PROXY_MODE", "rotate").lower().strip()
        self._proxy_list_raw = os.environ.get("PROXY_LIST", "").strip()
        self._check_url = os.environ.get("PROXY_CHECK_URL", "https://global.cainiao.com/")

    @property
    def is_active(self) -> bool:
        return self._mode == "rotate" and bool(self._proxies)

    def init(self) -> bool:
        """初始化代理列表，返回是否有可用代理。"""
        # 1. 从环境变量解析
        if self._proxy_list_raw:
            parts = [p.strip() for p in self._proxy_list_raw.split(",") if p.strip()]
            for p in parts:
                if p and p not in self._proxies:
                    self._proxies.append(p)
            log(f"[PROXY] 环境变量加载 {len(self._proxies)} 个代理")

        # 2. 有静态代理 或 direct 模式 → 立即返回，不阻塞
        if self._proxies or self._mode == "direct":
            return len(self._proxies) > 0

        # 3. 后台异步获取免费代理（不阻塞当前请求，ProxyScrape 等可能极慢或不可达）
        self._start_background_fetch()
        return False  # 暂时无可用代理，后台获取完成后自动可用

    def _start_background_fetch(self):
        """在后台线程获取免费代理，不影响首次查询速度。"""
        def _task():
            try:
                self._fetch_free_proxies()
                if self._proxies:
                    log(f"[PROXY] 后台获取完成，共 {len(self._proxies)} 个代理可用")
                else:
                    log(f"[PROXY] 后台获取未找到可用免费代理，将使用直连")
            except Exception as e:
                log(f"[PROXY] 后台获取异常: {e}")
        t = threading.Thread(target=_task, daemon=True)
        t.start()

    # ── 免费代理获取 ────────────────────────────────────

    def _fetch_free_proxies(self):
        now = time.time()
        if now - self._last_fetch_time < self._fetch_interval:
            return
        self._last_fetch_time = now

        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

        fetched = []
        raw_text = ""
        for url in _FREE_PROXY_SOURCES:
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "curl/8.0"})
                with urllib.request.urlopen(req, context=ctx, timeout=5) as r:
                    raw_text = r.read().decode("utf-8", errors="replace")
                if raw_text.strip():
                    # 按换行分割，每行格式 ip:port
                    candidates = [line.strip() for line in raw_text.splitlines() if ":" in line]
                    for c in candidates:
                        if c and c not in fetched and c not in self._proxies:
                            fetched.append(c)
                    log(f"[PROXY] {url.split('/')[2]} 获取到 {len(candidates)} 个代理")
                    if fetched:
                        break
            except Exception as e:
                log(f"[PROXY] 免费源 {url.split('/')[2]} 获取失败: {e}")
                continue

        if fetched:
            # 快速验证前 5 个
            alive = []
            for p in fetched[:5]:
                if self._check_proxy(p):
                    alive.append(p)
                    log(f"[PROXY] ✓ {p} 可用")
            self._proxies.extend(alive)
            log(f"[PROXY] 共获取 {len(fetched)} 个，存活 {len(alive)} 个")

    def _check_proxy(self, proxy: str, timeout: int = 4) -> bool:
        """测试单个代理是否可用。"""
        try:
            proxy_handler = urllib.request.ProxyHandler({"http": proxy, "https": proxy})
            opener = urllib.request.build_opener(proxy_handler)
            req = urllib.request.Request(self._check_url)
            with opener.open(req, timeout=timeout) as r:
                return r.status == 200
        except Exception:
            return False

    # ── 轮询接口 ────────────────────────────────────

    def get_proxy(self) -> Optional[str]:
        """返回下一个可用代理（轮询）。"""
        with self._lock:
            if not self._proxies:
                return None
            available = [p for p in self._proxies if p not in self._failed]
            if not available:
                # 全部失败 → 重置（可能临时失效）
                self._failed.clear()
                available = list(self._proxies)
            if self._index >= len(available):
                self._index = 0
            proxy = available[self._index]
            self._index += 1
            return proxy

    def mark_failed(self, proxy: str):
        """标记代理失败，后续轮询跳过。"""
        with self._lock:
            self._failed.add(proxy)
            log(f"[PROXY] ✗ {proxy} 已标记失败（当前剩余 {len(self._proxies) - len(self._failed)} 个）")

    # ── 与请求库集成 ────────────────────────────────────

    def to_curl_proxies(self, proxy: Optional[str] = None) -> dict:
        """转为 curl_cffi Session.get(proxies=...) 格式。"""
        p = proxy or self.get_proxy()
        if not p:
            return {"http": "", "https": ""}
        return {"http": p, "https": p}

    def to_playwright_proxy(self, proxy: Optional[str] = None) -> Optional[dict]:
        """转为 Playwright browser.new_context(proxy=...) 格式。"""
        p = proxy or self.get_proxy()
        if not p:
            return None
        return {"server": p}


def _get_proxy_manager() -> ProxyManager:
    """全局单例 ProxyManager。"""
    global _PROXY_MANAGER, _PROXY_MANAGER_READY
    if _PROXY_MANAGER_READY and _PROXY_MANAGER is not None:
        return _PROXY_MANAGER
    with _PROXY_MANAGER_LOCK:
        if _PROXY_MANAGER_READY and _PROXY_MANAGER is not None:
            return _PROXY_MANAGER
        pm = ProxyManager()
        ok = pm.init()
        if ok:
            log(f"[PROXY] 代理轮询已就绪（共 {len(pm._proxies)} 个代理）")
        else:
            log(f"[PROXY] 未配置代理，使用直连")
        _PROXY_MANAGER = pm
        _PROXY_MANAGER_READY = True
        return pm


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
    可安全缓存满 _CACHE_TTL；非终态（运输中、清关中等）短时缓存避免 WPS
    短时间反复查询上游触发限流，同时不会太久导致状态延误。

    Returns:
        _CACHE_TTL（终态全量缓存）或 _NON_TERMINAL_CACHE_TTL（非终态短时缓存）。
    """
    module = api_response.get("module")
    if not isinstance(module, list):
        return _NON_TERMINAL_CACHE_TTL  # 无有效数据也短暂缓存，避免反复查询
    # 仅在所有包裹均为终态时缓存
    if all(
        isinstance(item, dict) and item.get("status", "") in _TERMINAL_STATUS_CODES
        for item in module
    ):
        return _CACHE_TTL
    return _NON_TERMINAL_CACHE_TTL        # 运输中／清关中 → 短时缓存60s避免重复查


def query_cainiao(mail_no: str, lang: str = "zh-CN") -> dict:
    """查询菜鸟物流轨迹，使用 curl_cffi 直连（TLS 指纹模拟浏览器）。"""
    global _session_rl_count, _SESSION_QUERY_COUNT
    # --- 缓存检查：同一运单号短时间内不重复查上游 ---
    cache_key = f"{mail_no}:{lang}"
    cached = _cache_get(cache_key)
    if cached is not None:
        log(f"[CACHE] {mail_no} 命中缓存，直接返回")
        _req_ctx.cache_hit = True
        return cached
    _req_ctx.cache_hit = False

    _last_errors = []

<<<<<<< HEAD
    # --- 方案 B（首选）：curl_cffi 直连（TLS 指纹），速度远快于 Playwright ---
    # 注入 Playwright Cookie（如有），提升成功率
    _inject_playwright_cookies()

=======
>>>>>>> 6736181 (优化: 替换忙等待循环为 sleep 休眠)
    # 代理管理器（后台初始化，不阻塞首次直连）
    _proxy_mgr = _get_proxy_manager()
    _current_proxy = None
    _proxy_enabled = False          # 默认直连，触发限流后启用代理

    def _rotate_proxy(enable: bool = False):
        """启用/轮换代理，enable=True 在限流后强制启用。"""
        nonlocal _current_proxy, _proxy_enabled
        if enable:
            _proxy_enabled = True
        if _proxy_enabled and _proxy_mgr.is_active:
            _current_proxy = _proxy_mgr.get_proxy()
            if _current_proxy:
                return _proxy_mgr.to_curl_proxies(_current_proxy)
        return {"http": "", "https": ""}

    proxies = {"http": "", "https": ""}   # 首次直连，不走代理
    max_retries = 1
    max_rate_limit = 2          # 限流后最多重试 2 次（递增延迟），防止快速连续触发限流
    normal_attempt = 0
    rate_limit_attempt = 0
    last_exc = None

    try:
        while normal_attempt < max_retries:
            try:
                # 全局限流压力检测 → 如果近期限流太频繁，加额外延迟
                _global_rl_extra = _check_global_rl_pressure()
                if _global_rl_extra > 0:
                    log(f"[RL-PRESSURE] 全局限流压力 {_global_rl_extra:.0f}s 额外延迟")
                    time.sleep(_global_rl_extra)

                # 连续查询检测 → 超过阈值自动加额外延迟，防批量特征
                _adaptive_delay()

                # 请求前随机 jitter，模拟人类操作间隔
                jitter = random.uniform(*_PRE_REQUEST_JITTER)
                if jitter > 0:
                    time.sleep(jitter)

                # 在每次重试前检查是否需要轮换 Session（避免持续限流）
                _maybe_rotate_session()

                # 上游调用间隔控制 — 避免并发请求触发流量特征检测
                _pace_upstream_call()

                # 记录本次查询为连续查询（用于自适应延迟）
                _inc_consecutive_query()

                resp = _CAINIAO_SESSION.get(
                    PUBLIC_API_URL,
                    params={"mailNos": mail_no, "lang": lang},
                    proxies=proxies,
                    verify=False,
                    timeout=8,
                )

                # 检查 Content-Type —— 如果不是 JSON 则大概率被拦截了
                ct = (resp.headers.get("Content-Type", "") or "").lower()
                if "json" not in ct and "javascript" not in ct:
                    normal_attempt += 1
                    if normal_attempt < max_retries:
                        delay = 1
                        time.sleep(delay)
                        continue
                    else:
                        raise RuntimeError(f"上游返回 {ct or '非JSON'}：{resp.text[:300]}")

                body = resp.json()

                # 检查 API 业务状态
                success_flag = body.get("success")
                module_val = body.get("module")
                # 上游限流特征：① success=False/module=None；② success=True 但 module 为空数组/全是 null
                # ③ module 有效但所有条目的 detailList 为空（限流时返回空壳对象）
                is_rate_limited_signal = False
                if success_flag is False or module_val is None:
                    is_rate_limited_signal = True
                elif success_flag is True and isinstance(module_val, list) and len(module_val) == 0:
                    is_rate_limited_signal = True
                elif success_flag is True and isinstance(module_val, list) and all(m is None for m in module_val):
                    is_rate_limited_signal = True
                elif success_flag is True and isinstance(module_val, list) and len(module_val) > 0:
                    # 增强检测：所有条目既无状态描述又无轨迹详情 → 空壳数据，视为限流
                    all_empty = True
                    for _m in module_val:
                        if isinstance(_m, dict):
                            has_status = bool(_m.get("statusDesc") or _m.get("status"))
                            has_detail = bool(_m.get("detailList"))
                            if has_status or has_detail:
                                all_empty = False
                                break
                    if all_empty:
                        is_rate_limited_signal = True

                if is_rate_limited_signal:
                    err_text = json.dumps(body, ensure_ascii=False)
                    # 检测上游限流特征码（"被挤爆啦" / "RGV587_ERROR"）
                    is_rate_limited = "RGV587_ERROR" in err_text or "被挤爆" in err_text or is_rate_limited_signal
                    if is_rate_limited:
                        rate_limit_attempt += 1
                        # 标记当前代理失败并轮换（触发限流后启用代理）
                        if _current_proxy:
                            _proxy_mgr.mark_failed(_current_proxy)
                        proxies = _rotate_proxy(enable=True)
                        # 立即强制轮换 Session（不等累计阈值）
                        _maybe_rotate_session(force=True)
                        with _session_lock:
                            _session_rl_count += 1
                        if rate_limit_attempt > max_rate_limit:
                            raise RuntimeError(f"上游持续限流，已重试{max_rate_limit}次仍失败")
                        log(f"[RLIMIT] {mail_no} 上游限流 {_rl_count_str(rate_limit_attempt)}，已强制轮换 Session+代理")
                        # 记录全局限流频率
                        _rl_count_in_window = _record_rate_limit()
                        if _rl_count_in_window >= _GLOBAL_RL_WINDOW_THRESHOLD:
                            log(f"[RLIMIT] 近期限流频率高（{_rl_count_in_window}次/{_GLOBAL_RL_WINDOW_SECONDS}s），全局限速中")
                        # 提取 punish URL 并访问以解除封锁
                        punish_url = None
                        if isinstance(body.get("data"), dict):
                            punish_url = body["data"].get("url")
                        if punish_url:
                            try:
                                _pace_upstream_call()
                                _CAINIAO_SESSION.get(
                                    punish_url,
                                    proxies=proxies,
                                    verify=False,
                                    timeout=10,
                                )
                                log(f"[UNLOCK] {mail_no} 已访问 punish URL 解除限流")
                            except Exception:
                                pass
                        log(f"[HOLD] {mail_no} 限流冷却，等待后重试")
                        # 递增延迟：第1次1.5s, 第2次3s, 第3次6s...上限8s
                        rl_delay = 1.5 * (2 ** (rate_limit_attempt - 1))
                        rl_delay = min(rl_delay, 8.0)
                        log(f"[COOLDOWN] {mail_no} 等待 {rl_delay:.1f}s 冷却…（第{rate_limit_attempt}次限流）")
                        time.sleep(rl_delay)
                        continue  # 不计入普通重试

                    normal_attempt += 1
                    if normal_attempt < max_retries:
                        delay = 1
                        log(f"[WARN] {mail_no} API返回异常 (第{normal_attempt}次)，{delay}s 后重试…")
                        time.sleep(delay)
                        continue
                    raise RuntimeError(f"API返回异常: {err_text[:200]}")

                # 判断是否所有包裹都已达到终态，决定缓存时长
                _cache_set(cache_key, body, ttl=_resolve_cache_ttl(body))
                # 查询成功 → 重置连续限流计数器，累计查询次数并检查是否需要主动轮换
                with _session_lock:
                    _session_rl_count = 0
                    _SESSION_QUERY_COUNT += 1
                # 查询后检查是否达到主动轮换阈值
                _maybe_rotate_session()
                # 查询成功后加短随机延迟，打散批量查询节奏
                time.sleep(random.uniform(0.1, 0.4))
                return body

            except json.JSONDecodeError as e:
                raw_preview = resp.text[:300].strip().replace("\r", "\\r").replace("\n", "\\n")
                normal_attempt += 1
                if normal_attempt < max_retries:
                    delay = 1
                    log(f"[WARN] {mail_no} JSON解析失败 (第{normal_attempt}次)，{delay}s 后重试…")
                    time.sleep(delay)
                    continue
                raise RuntimeError(f"JSON解析失败，响应前300字符: {raw_preview}") from e

            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
                last_exc = e
                normal_attempt += 1
                if normal_attempt < max_retries:
                    delay = 1
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
        _last_errors.append(f"[B] curl_cffi: {e}")

    # --- 方案 A（次要）：Playwright 真实浏览器，作为 curl_cffi 限流后的降级方案 ---
    if _check_playwright_available():
        log(f"[FALLBACK] 尝试 Playwright 查询 {mail_no}…")
        try:
            _pace_upstream_call()
            raw = _playwright_query(mail_no, lang)
            if raw and raw.get("success") is not False:
                log(f"[OK] Playwright 成功查询 {mail_no}")
                _cache_set(cache_key, raw, ttl=_resolve_cache_ttl(raw))
                return raw
            log(f"[WARN] Playwright 返回异常: {json.dumps(raw, ensure_ascii=False)[:200]}")
        except Exception as pw_e:
            log(f"[FALLBACK] Playwright 失败: {pw_e}")
            _last_errors.append(f"[A] Playwright: {pw_e}")
    else:
        log("[SKIP] Playwright 浏览器未安装，跳过")
        _last_errors.append("[A] Playwright: 未安装")

    # --- 方案 C：标准 urllib（零依赖，完全不同 TLS 指纹，绕过 curl_cffi 特殊特征） ---
    log(f"[FALLBACK] 尝试标准 urllib 查询 {mail_no}…")
    try:
        import ssl
        import urllib.request

        # 复用已有 cookies — 兼容不同版本的 curl_cffi Cookies 对象
        with _session_lock:
            cj = _CAINIAO_SESSION.cookies
            cookie_parts = []
            try:
                # 方法1：遍历标准 CookieJar（兼容 curl_cffi RequestsCookieJar 和标准库）
                for cookie in cj:
                    name = getattr(cookie, 'name', None)
                    value = getattr(cookie, 'value', None)
                    if name and value:
                        cookie_parts.append(f"{name}={value}")
            except Exception:
                try:
                    # 方法2：get_dict() 接口
                    cookie_dict = cj.get_dict()
                    cookie_parts = [f"{k}={v}" for k, v in cookie_dict.items()]
                except Exception:
                    try:
                        # 方法3：纯 dict
                        if isinstance(cj, dict):
                            cookie_parts = [f"{k}={v}" for k, v in cj.items()]
                    except Exception:
                        cookie_parts = []
        cookie_header = "; ".join(cookie_parts) if cookie_parts else ""

        urllib_headers = {
            "User-Agent": random.choice(_USER_AGENTS),
            "Accept": "application/json, text/plain, */*",
            "Referer": f"https://global.cainiao.com/global/detail.json?mailNo={mail_no}",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }
        if cookie_header:
            urllib_headers["Cookie"] = cookie_header

        ssl_ctx = ssl.create_default_context()
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE

        _pace_upstream_call()
        req_url = f"{PUBLIC_API_URL}?mailNo={quote(mail_no)}&lang={lang}"
        req = urllib.request.Request(req_url, headers=urllib_headers)

        # urllib 也使用代理（如果已激活）
        urllib_proxy = _current_proxy or _proxy_mgr.get_proxy()
        if urllib_proxy:
            proxy_handler = urllib.request.ProxyHandler({"http": urllib_proxy, "https": urllib_proxy})
            urllib_opener = urllib.request.build_opener(proxy_handler)
            with urllib_opener.open(req, timeout=15) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        else:
            with urllib.request.urlopen(req, context=ssl_ctx, timeout=15) as resp:
                body = json.loads(resp.read().decode("utf-8"))

        if body.get("success") is not False and body.get("module") is not None:
            log(f"[OK] urllib 成功查询 {mail_no}")
            _cache_set(cache_key, body, ttl=_resolve_cache_ttl(body))
            return body
        log(f"[WARN] urllib 返回异常: {json.dumps(body, ensure_ascii=False)[:200]}")
    except Exception as ue:
        log(f"[FALLBACK] urllib 也失败: {ue}")
        # 代理失败时标记，下次查询将轮换
        if urllib_proxy:
            _proxy_mgr.mark_failed(urllib_proxy)
        _last_errors.append(f"[C] urllib: {ue}")

    # ---- 第4步：Playwright 降级方案（终极兜底） ----
    try:
        from playwright.sync_api import sync_playwright

        log(f"[PLAYWRIGHT] 尝试通过 Playwright 查询 {mail_no} ...")
        pw_proxy = _current_proxy or _proxy_mgr.get_proxy()
        pw_proxy_settings = None
        if pw_proxy:
            pw_proxy_settings = {"server": pw_proxy}

        pw_url = f"https://global.cainiao.com/global/detail.json?mailNo={mail_no}&lang={lang}"
        pw_result = None

        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                proxy=pw_proxy_settings,
                args=["--disable-blink-features=AutomationControlled"],
            )
            context = browser.new_context(
                user_agent=random.choice(_USER_AGENTS),
                locale="zh-CN",
                extra_http_headers={
                    "Accept": "application/json, text/plain, */*",
                    "Referer": f"https://global.cainiao.com/global/detail.json?mailNo={mail_no}",
                },
            )
            page = context.new_page()
            try:
                resp = page.goto(pw_url, wait_until="domcontentloaded", timeout=15000)
                body_text = page.inner_text("pre") or page.inner_text("body")
                if resp:
                    try:
                        pw_result = resp.json()
                    except Exception:
                        pass
                if not pw_result:
                    try:
                        pw_result = json.loads(body_text)
                    except Exception:
                        pass
            except Exception as pw_e:
                log(f"[PLAYWRIGHT] goto 失败: {pw_e}")
            finally:
                browser.close()

        if pw_result and pw_result.get("success") is not False and pw_result.get("module") is not None:
            log(f"[OK] Playwright 成功查询 {mail_no}")
            _cache_set(cache_key, pw_result, ttl=_resolve_cache_ttl(pw_result))
            return pw_result
        log(f"[WARN] Playwright 返回异常或空结果")
    except ImportError:
        log(f"[PLAYWRIGHT] playwright 未安装，跳过")
    except Exception as pw_e:
        log(f"[PLAYWRIGHT] 执行异常: {pw_e}")
        if pw_proxy:
            _proxy_mgr.mark_failed(pw_proxy)

    detail = " | ".join(_last_errors) if _last_errors else "全部查询方案均返回空"
    raise RuntimeError(f"所有查询方案均失败 ({mail_no}): {detail}")


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

            # 检查全局限流冷却：如果冷却中，直接 429 让客户端自己等
            _now = time.time()
            with _GLOBAL_COOLDOWN_LOCK:
                if _now < _GLOBAL_COOLDOWN:
                    wait_sec = int(_GLOBAL_COOLDOWN - _now) + 1
                    log(f"[REQ] 全局限流冷却中，返回 429 Retry-After={wait_sec}s")
                    self._write_json(429, {
                        "code": 429,
                        "error": f"服务器全局限流冷却中，请在 {wait_sec} 秒后重试",
                        "retryAfter": wait_sec,
                    })
                    return
                # 也检查全局限流压力 (60s 内限流次数)
                _recent_rl = len([t for t in _GLOBAL_RL_TIMESTAMPS if t > _now - _GLOBAL_RL_WINDOW_SECONDS])
            if _recent_rl >= _GLOBAL_RL_WINDOW_THRESHOLD:
                log(f"[REQ] 全局限流压力高 ({_recent_rl}/{_GLOBAL_RL_WINDOW_SECONDS}s)，返回 429 减速")
                self._write_json(429, {
                    "code": 429,
                    "error": f"近期限流频繁（{_recent_rl}次/{_GLOBAL_RL_WINDOW_SECONDS}s），请稍后重试",
                    "retryAfter": 5,
                })
                return

            if not mail_nos:
                self._write_json(400, {"error": "缺少参数 ?mailNo=xxx"})
                log(f"[REQ] 400 缺少参数")
                return

            # 支持批量，用逗号分隔
            mail_no_list = [m.strip() for m in mail_nos[0].split(",") if m.strip()]
            mail_no_str = ",".join(mail_no_list)
            log(f"[REQ] → {', '.join(mail_no_list)} (共{len(mail_no_list)}单)")

            # 分批查询：上游 API 对大批量有限制（建议每批 ≤20），拆成多个子批次并发执行
            SUB_BATCH_SIZE = 3  # 单批次最多3单，降低单次请求的限流命中概率

            def _process_sub_batch(sub_batch: list[str]) -> list[dict]:
                """查询单个子批次（20 单），返回 parse_simplified 结果。"""
                sub_batch_local = []
                sub_str = ",".join(sub_batch)
                try:
                    raw = query_cainiao(sub_str, lang)
                    parsed = parse_simplified(raw)
                    sub_batch_local.extend(parsed)
                    returned_mns = {r["mailNo"] for r in parsed}

                    # 对子批次内缺失的单号逐个补查
                    for mn in sub_batch:
                        if mn not in returned_mns:
                            log(f"[RETRY] 批查未返回，单独补查: {mn}")
                            try:
                                ind_raw = query_cainiao(mn, lang)
                                ind_parsed = parse_simplified(ind_raw)
                                if ind_parsed:
                                    sub_batch_local.append(ind_parsed[0])
                                    returned_mns.add(mn)
                                    log(f"[RETRY] ✓ {mn} 补查成功: {ind_parsed[0].get('status','')}")
                            except Exception as ind_e:
                                log(f"[RETRY] ✗ {mn} 补查失败: {ind_e}")

                    # 仍缺失的 → 标记无数据
                    still_missing = [mn for mn in sub_batch
                                     if mn not in {r["mailNo"] for r in sub_batch_local}]
                    for mn in still_missing:
                        sub_batch_local.append({
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
                except Exception as sub_e:
                    err_msg = str(sub_e)
                    log(f"[SUB-ERR] 子批次 {','.join(sub_batch)} 失败: {err_msg}")
                    for mn in sub_batch:
                        sub_batch_local.append({
                            "mailNo": mn,
                            "status": "查询失败",
                            "statusCode": "",
                            "origin": "",
                            "dest": "",
                            "latestTime": "",
                            "latestEvent": "",
                            "eventCount": 0,
                            "error": err_msg,
                        })
                return sub_batch_local

            # 将连续的子批次提交到线程池并发执行（最多 4 个 worker）
            # 独立 mailNo 之间无竞态，且能大幅减少总耗时
            # 加入交错延迟，避免所有子批同时发出引发限流
            all_simplified = []
            batch_errors = {}
            max_parallel = min(4, (len(mail_no_list) + SUB_BATCH_SIZE - 1) // SUB_BATCH_SIZE)
            with ThreadPoolExecutor(max_workers=max_parallel) as executor:
                futures = []
                for b_idx in range(0, len(mail_no_list), SUB_BATCH_SIZE):
                    sub_batch = mail_no_list[b_idx:b_idx + SUB_BATCH_SIZE]
                    future = executor.submit(_process_sub_batch, sub_batch)
                    futures.append(future)
                    # 交错提交，避免所有子批在同一瞬间触发上游请求
                    if len(futures) > 1:
                        time.sleep(random.uniform(0.3, 0.8))

                for future in as_completed(futures):
                    try:
                        all_simplified.extend(future.result())
                    except Exception as e:
                        log(f"[PARALLEL] 子批次异常: {e}")

            # 统计失败 & 检测是否发生限流
            batch_has_rate_limit = False
            for entry in all_simplified:
                if entry.get("error"):
                    batch_errors[entry["mailNo"]] = entry["error"]
                    if "限流" in entry.get("error", "") or "rate" in entry.get("error", "").lower():
                        batch_has_rate_limit = True

            elapsed_total = time.time() - req_start
            success_count = len(all_simplified) - len(batch_errors)
            log(f"[OK] ✓ {len(mail_no_list)}单完成 耗时{elapsed_total:.1f}s (成功{success_count} 失败{len(batch_errors)})")
            # 异步写入 Google Sheets（如果已配置）
            _write_results_to_sheet(all_simplified)

            # 如果批次内发生了限流，在响应中标注，让客户端主动减速
            resp_body = {"code": 0, "data": all_simplified}
            if batch_has_rate_limit:
                resp_body["rateLimited"] = True
                resp_body["retryHint"] = "detected"

            self._write_json(200, resp_body)

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
    print(f"[INFO] Playwright: {'[OK] 已安装' if _pw_available else '[NO] 未安装（仅使用 curl_cffi 直连，遇见滑块验证会报错）'}")
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
