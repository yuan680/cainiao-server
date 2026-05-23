#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
菜鸟 LINK 物流轨迹查询工具
调用 cnge.track.get API 查询运单物流轨迹
"""

import argparse
import base64
import hashlib
import json
import sys
import os
from datetime import datetime, timezone

import requests


# ============================================================
# 配置区域 — 从同级 config.json 读取，也可通过命令行参数传入
# ============================================================
DEFAULT_APPKEY = "562861"
DEFAULT_APPSECRET = ""


def sign(body_str: str, secret: str) -> str:
    """
    Cainiao LINK 签名算法:
      1. 拼接 body + secret
      2. 计算 MD5 原始字节
      3. Base64 编码
    """
    raw = (body_str + secret).encode("utf-8")
    md5_bytes = hashlib.md5(raw).digest()
    return base64.b64encode(md5_bytes).decode("utf-8")


def query_track(mail_no: str, app_key: str, app_secret: str,
                api_code: str = "cnge.track.get",
                gateway: str = "https://link.cainiao.com/gateway") -> dict:
    """
    调用菜鸟 LINK 网关查询物流轨迹

    参数:
        mail_no:   运单号
        app_key:   应用 AppKey
        app_secret: 应用 AppSecret
        api_code:  API 编码，默认 cnge.track.get
        gateway:   网关地址
    返回:
        解析后的 JSON 响应 (dict)
    """
    url = f"{gateway}/{api_code}"
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # 请求体 (根据 API 文档调整字段名)
    body_obj = {"mailNo": mail_no}
    body_str = json.dumps(body_obj, separators=(",", ":"))

    # 签名
    signature = sign(body_str, app_secret)

    headers = {
        "Content-Type": "application/json",
        "x-appkey": app_key,
        "x-sign": signature,
        "x-date": now_str,
    }

    resp = requests.post(url, headers=headers, data=body_str, timeout=30)
    resp.raise_for_status()
    return resp.json()


def format_result(data: dict) -> str:
    """将 API 返回的 JSON 格式化为可读文本"""
    lines = []
    lines.append("=" * 60)
    lines.append("物流轨迹查询结果")
    lines.append("=" * 60)

    # 尝试判断是否成功
    if "success" in data:
        if data.get("success"):
            lines.append("状态: ✅ 查询成功")
        else:
            lines.append(f"状态: ❌ 查询失败 - {data.get('errorMsg', '未知错误')}")
            lines.append(json.dumps(data, ensure_ascii=False, indent=2))
            return "\n".join(lines)

    if "result" in data and isinstance(data["result"], list):
        for step in data["result"]:
            time_str = step.get("time", step.get("acceptTime", ""))
            desc = step.get("desc", step.get("remark", ""))
            status = step.get("status", "")
            lines.append(f"  [{time_str}] {desc} ({status})")
    elif "data" in data:
        lines.append(json.dumps(data["data"], ensure_ascii=False, indent=2))
    else:
        lines.append(json.dumps(data, ensure_ascii=False, indent=2))

    lines.append("=" * 60)
    return "\n".join(lines)


def load_config() -> dict:
    """读取同目录下的 config.json（如果存在）"""
    cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
    if os.path.isfile(cfg_path):
        try:
            with open(cfg_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


# ============================================================
# 公开 API（无需鉴权）
# ============================================================
PUBLIC_API_URL = "https://global.cainiao.com/global/detail.json"


def query_public(mail_no: str, lang: str = "zh-CN") -> dict:
    """使用菜鸟公开查询 API（无需任何 AppKey/AppSecret）"""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://global.cainiao.com/",
    }
    params = {"mailNos": mail_no, "lang": lang}
    resp = requests.get(PUBLIC_API_URL, headers=headers, params=params, timeout=15)
    resp.raise_for_status()
    return resp.json()


def check_public_success(data: dict) -> bool:
    """检查公开 API 返回是否包含有效轨迹数据"""
    module = data.get("module", [])
    return len(module) > 0 and module[0].get("mailNo") is not None


# ============================================================
# LINK API（需 AppSecret）
# ============================================================
def query_link(mail_no: str, app_key: str, app_secret: str,
               api_code: str = "cnge.track.get",
               gateway: str = "https://link.cainiao.com/gateway") -> dict:
    """使用菜鸟 LINK 网关查询（别名，与 query_track 相同）"""
    return query_track(mail_no, app_key, app_secret, api_code, gateway)


# ============================================================
# 输出格式化
# ============================================================
def format_human(data: dict) -> str:
    """将公开 API 返回的数据格式化为可读文本"""
    lines = []
    module = data.get("module", [])
    if not module:
        return "未查询到物流信息"

    item = module[0]
    mail_no = item.get("mailNo", "")
    status_desc = item.get("statusDesc", "")
    origin = item.get("originCountry", "")
    dest = item.get("destCountry", "")

    lines.append("=" * 60)
    lines.append(f"运单号: {mail_no}")
    lines.append(f"状态: {status_desc}")
    if origin:
        lines.append(f"发件地: {origin}")
    if dest:
        lines.append(f"目的地: {dest}")
    lines.append("-" * 60)

    detail_list = item.get("detailList", [])
    if detail_list:
        lines.append("轨迹明细:")
        for evt in detail_list:
            ts = evt.get("timeStr", "")
            desc = evt.get("standerdDesc", evt.get("desc", ""))
            carrier_note = evt.get("desc", "")
            desc_title = evt.get("descTitle", "承运商备注")
            lines.append(f"  [{ts}] {desc}")
            if carrier_note and carrier_note != desc:
                lines.append(f"          📎 {desc_title} {carrier_note}")
    else:
        latest = item.get("latestTrace", {})
        if latest:
            ts = latest.get("timeStr", "")
            desc = latest.get("standerdDesc", latest.get("desc", ""))
            lines.append(f"  [{ts}] {desc}")

    lines.append("=" * 60)
    return "\n".join(lines)


def format_link_result(data: dict) -> str:
    """格式化 LINK API 返回结果"""
    return format_result(data)


# ============================================================
# CSV 导出
# ============================================================
def extract_tracking_rows(data: dict) -> list:
    """提取物流摘要行（用于 CSV / WPS 多维表）"""
    rows = []
    module = data.get("module", [])
    for item in module:
        mail_no = item.get("mailNo", "")
        status_desc = item.get("statusDesc", "")
        status = item.get("status", "")
        origin = item.get("originCountry", "")
        dest = item.get("destCountry", "")
        latest = item.get("latestTrace", {})
        last_time = latest.get("timeStr", "") if latest else ""
        last_event = latest.get("standerdDesc", "") if latest else ""
        rows.append({
            "物流单号": mail_no,
            "物流状态": status_desc,
            "状态码": status,
            "发件地": origin,
            "目的地": dest,
            "最后更新时间": last_time,
            "最后事件": last_event,
        })
    return rows


def extract_tracking_events(data: dict) -> list:
    """展开为每行一个轨迹事件（用于 CSV --expand）"""
    rows = []
    module = data.get("module", [])
    for item in module:
        mail_no = item.get("mailNo", "")
        for evt in item.get("detailList", []):
            rows.append({
                "物流单号": mail_no,
                "时间": evt.get("timeStr", ""),
                "事件": evt.get("standerdDesc", evt.get("desc", "")),
                "承运商备注": evt.get("desc", ""),
                "时区": evt.get("timeZone", ""),
            })
    return rows


def write_csv(rows: list, path: str):
    """将字典列表写入 CSV 文件（UTF-8 BOM，Excel 兼容）"""
    import csv
    if not rows:
        print("无数据可导出")
        return
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    print(f"✅ CSV 已导出: {os.path.abspath(path)}")


# ============================================================
# WPS 开放 API 推送（预留）
# ============================================================
def load_wps_config() -> dict:
    """读取 wps_config.json（WPS 开放平台凭据）"""
    cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "wps_config.json")
    if os.path.isfile(cfg_path):
        try:
            with open(cfg_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def push_to_wps(rows: list, cfg: dict):
    """通过 WPS 开放 API 推送数据到多维表（需在 open.wps.cn 申请）"""
    client_id = cfg.get("client_id", "")
    client_secret = cfg.get("client_secret", "")
    table_id = cfg.get("table_id", "")

    if not client_id or not client_secret or not table_id:
        print("❌ wps_config.json 缺少必要参数 (client_id, client_secret, table_id)")
        print("   请在 WPS 开放平台 (https://open.wps.cn) 创建应用后获取")
        print("   配置示例:")
        print("     {")
        print('       "client_id": "your_client_id",')
        print('       "client_secret": "your_secret",')
        print('       "table_id": "your_table_id"')
        print("     }")
        return

    print("⏳ 正在通过 WPS 开放 API 推送数据...")
    # TODO: 实现 OAuth 2.0 鉴权 + 多维表 API 调用
    # 参考: https://open.wps.cn/docs/cloud-api
    print("❌ 暂未实现，请关注 WPS 开放平台文档更新")


# ============================================================


def main():
    parser = argparse.ArgumentParser(
        description="菜鸟全球物流轨迹查询 — WPS 多维表集成版",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
使用示例:
  # 查询单个运单 (默认使用公开 API, 无需配置)
  cainiao_track LP00812637173551

  # 使用 LINK API (需 config.json 配置 appSecret)
  cainiao_track LP00812637173551 --link

  # 输出 JSON
  cainiao_track LP00812637173551 --json

  # 输出 CSV (可直接导入 WPS 多维表)
  cainiao_track LP00812637173551 --csv

  # 导出轨迹明细 (每行一个事件)
  cainiao_track LP00812637173551 --csv --expand

  # 直接推送至 WPS 多维表 (需配置 wps_config.json)
  cainiao_track LP00812637173551 --wps
        """
    )
    parser.add_argument("mail_no", help="运单号（快递单号）")
    parser.add_argument("--link", action="store_true",
                        help="使用菜鸟 LINK API (需配置 AppSecret)")
    parser.add_argument("--appkey", default=None, help="LINK API AppKey")
    parser.add_argument("--appsecret", default=None, help="LINK API AppSecret")
    parser.add_argument("--api-code", default=None, help="LINK API 编码")
    parser.add_argument("--gateway", default=None, help="LINK API 网关")
    parser.add_argument("--json", action="store_true",
                        help="以 JSON 格式输出")
    parser.add_argument("--csv", action="store_true",
                        help="导出 CSV 文件 (可导入 WPS 多维表)")
    parser.add_argument("--expand", action="store_true",
                        help="展开为每行一个轨迹事件 (与 --csv 搭配)")
    parser.add_argument("--csv-path", default=None,
                        help="CSV 输出路径 (默认: ./cainiao_track_{运单号}.csv)")
    parser.add_argument("--wps", action="store_true",
                        help="直接推送数据到 WPS 多维表")
    parser.add_argument("--lang", default="zh-CN",
                        help="语言 (zh-CN/en-US, 默认 zh-CN)")

    args = parser.parse_args()
    cfg = load_config()

    # ========== 查询 ==========
    try:
        if args.link:
            app_key = args.appkey or cfg.get("appKey") or DEFAULT_APPKEY
            app_secret = args.appsecret or cfg.get("appSecret") or DEFAULT_APPSECRET
            api_code = args.api_code or cfg.get("apiCode") or "cnge.track.get"
            gateway = args.gateway or cfg.get("gateway") or "https://link.cainiao.com/gateway"
            if not app_secret:
                print("错误: LINK API 需配置 AppSecret")
                print("       1. --appsecret xxxxx")
                print("       2. 修改 config.json 中的 appSecret")
                sys.exit(1)
            result = query_link(args.mail_no, app_key, app_secret, api_code, gateway)
        else:
            result = query_public(args.mail_no, args.lang)
            if not check_public_success(result):
                print("⚠ 公开 API 未返回数据，尝试 LINK API...")
                cfg = load_config()
                if cfg.get("appSecret"):
                    result = query_link(args.mail_no, cfg.get("appKey", DEFAULT_APPKEY),
                                        cfg["appSecret"])
                else:
                    print("   (未配置 AppSecret，无法使用 LINK API)")
                    sys.exit(1)
    except requests.exceptions.Timeout:
        print("错误: 请求超时")
        sys.exit(1)
    except requests.exceptions.HTTPError as e:
        print(f"错误: HTTP {e.response.status_code}")
        if e.response.text:
            print(e.response.text[:500])
        sys.exit(1)
    except requests.exceptions.RequestException as e:
        print(f"错误: 网络请求失败 - {e}")
        sys.exit(1)
    except json.JSONDecodeError:
        print("错误: 返回数据格式异常")
        sys.exit(1)

    # ========== 输出 ==========
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif args.csv:
        rows = extract_tracking_events(result) if args.expand else extract_tracking_rows(result)
        csv_path = args.csv_path or f"cainiao_track_{args.mail_no}.csv"
        write_csv(rows, csv_path)
    elif args.wps:
        wps_cfg = load_wps_config()
        if not wps_cfg.get("client_id") or not wps_cfg.get("client_secret"):
            print("错误: 推送 WPS 需先配置 wps_config.json")
            print("       参考 WPS集成指南.md 获取配置说明")
            sys.exit(1)
        rows = extract_tracking_rows(result)
        push_to_wps(rows, wps_cfg)
    elif args.link:
        print(format_link_result(result))
    else:
        print(format_human(result))
        print("💡 提示: 用 --csv 导出到 WPS 多维表，或 --wps 直接推送")


if __name__ == "__main__":
    main()
