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
# 每次查询前的随机延时范围（秒），避免触发风控
# 串行查询时 0.5~1.5s/个，真人操作节奏
QUERY_DELAY_MIN = 0.5
QUERY_DELAY_MAX = 1.5

# 菜鸟公开 API
PUBLIC_API_URL = "https://global.cainiao.com/global/detail.json"

# 日志输出（WPS 请求时需要静默运行，日志写入文件可选）
LOG_FILE = None  # 设置路径即可启用日志文件

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
# 方案 B：requests（传统直连，降级用）
# ============================================================
_CAINIAO_SESSION = requests.Session()
_CAINIAO_SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
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
# 首次访问首页获取必要 cookies
try:
    _CAINIAO_SESSION.get(
        "https://global.cainiao.com/",
        proxies={"http": "", "https": ""},
        verify=False,
        timeout=10,
    )
except Exception:
    pass


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


def query_cainiao(mail_no: str, lang: str = "zh-CN") -> dict:
    """查询菜鸟物流轨迹，使用 curl_cffi 直连（TLS 指纹模拟浏览器）。"""
    # 每次查询前随机延时，模仿人类的操作间隔
    delay = random.uniform(QUERY_DELAY_MIN, QUERY_DELAY_MAX)
    log(f"[SLEEP] {mail_no} 等待 {delay:.1f}s…")
    time.sleep(delay)

    # 注入之前 Playwright 提取的 cookies（如果有），降低滑块触发概率
    _inject_playwright_cookies()

    # --- 方案 A：curl_cffi（TLS 指纹模拟浏览器，速度快） ---
    proxies = {"http": "", "https": ""}
    max_retries = 3
    max_rate_limit = 1
    normal_attempt = 0
    rate_limit_attempt = 0
    last_exc = None

    try:
        while normal_attempt < max_retries:
            try:
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
                        log(f"[COOLDOWN] {mail_no} 等待 {rl_delay}s 冷却…")
                        time.sleep(rl_delay)
                        continue  # 不计入普通重试

                    normal_attempt += 1
                    if normal_attempt < max_retries:
                        delay = 2 ** normal_attempt
                        log(f"[WARN] {mail_no} API返回异常 (第{normal_attempt}次)，{delay}s 后重试…")
                        time.sleep(delay)
                        continue
                    raise RuntimeError(f"API返回异常: {err_text[:200]}")

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
    if USE_PLAYWRIGHT:
        log(f"[FALLBACK] 尝试 Playwright 绕过 CAPTCHA 查询 {mail_no}…")
        try:
            raw = _playwright_query(mail_no, lang)
            if raw and raw.get("success") is not False:
                log(f"[OK] Playwright 成功查询 {mail_no}")
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

        if parsed.path == "/query":
            params = parse_qs(parsed.query)
            mail_nos = params.get("mailNo", [])
            lang = params.get("lang", ["zh-CN"])[0]

            if not mail_nos:
                self._write_json(400, {"error": "缺少参数 ?mailNo=xxx"})
                return

            # 支持批量，用逗号分隔
            mail_no_list = [m.strip() for m in mail_nos[0].split(",") if m.strip()]

            # 合并所有单号为逗号分隔字符串，一次 API 调用（官网 detail.json 原生支持）
            mail_no_str = ",".join(mail_no_list)
            try:
                raw = query_cainiao(mail_no_str, lang)
                # parse_simplified 内部遍历 module[]（本身就是 list）
                simplified = parse_simplified(raw)
                log(f"[OK] 批量 {len(mail_no_list)} 单 → {len(simplified)} 结果")
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
                log(f"[ERR] 批量查询失败: {e}")
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
